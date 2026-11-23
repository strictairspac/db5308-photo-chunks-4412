import hashlib
from concurrent.futures import ThreadPoolExecutor

from src.constants.workflow import Status
from src.models.tables import runs
from src.services import domain, workflow
from src.utils.encoding import canonical
from src.utils.time import utc_time


def auth(client, username):
    resp = client.post('/login', json={'username': username, 'password': 'test-pass'})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


def submit_case(client, headers, asset, snapshot, content='SCAN-OK!', faults=None):
    """经办员建立档案并送检（创建执行）。"""
    payload = {'chunk_size': 4, 'restore_snapshot': snapshot,
               'snapshots': [{'snapshot_id': snapshot, 'asset': asset, 'encoding': 'utf8', 'content': content}],
               'retained_snapshots': [{'snapshot_id': snapshot}], 'faults': faults or []}
    case = client.post('/cases', json={'title': f'{asset} 查验', 'payload': payload}, headers=headers)
    assert case.status_code == 201, case.text
    run = client.post(f"/cases/{case.json()['id']}/runs", headers=headers)
    assert run.status_code == 202, run.text
    return case.json(), run.json()


def execute(engine, run, payload):
    """模拟 worker：认领该执行并跑到完成。"""
    token = 'test-attempt'
    with engine.begin() as conn:
        conn.execute(runs.update().where(runs.c.id == run['id'])
                     .values(status=Status.RUNNING, started_at=utc_time(), attempt_token=token))
    workflow.finish(run['id'], token, result=domain.solve(payload, None))


def make_batch(client, headers, title, assets):
    resp = client.post('/batches', json={'title': title, 'assets': assets}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_handover_issues_one_voucher_after_all_pass(client, engine):
    op = auth(client, 'operator')
    case, run = submit_case(client, op, 'T10-frame01.dpx', 'T10-1')
    execute(engine, run, case['payload'])
    batch = make_batch(client, op, 'T10 卷', ['T10-frame01.dpx'])

    resp = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert resp.status_code == 201, resp.text
    voucher = resp.json()
    assert voucher['code'] == f"VCH-{batch['id']:06d}"
    (item,) = voucher['items']
    assert (item['asset'], item['snapshot'], item['run_id']) == ('T10-frame01.dpx', 'T10-1', run['id'])
    evidence = client.get('/evidence', headers=op).json()
    report = next(row for row in evidence if row['run_id'] == run['id'])
    assert (item['evidence_id'], item['evidence_digest']) == (report['id'], report['digest'])
    expect = hashlib.sha256(canonical({'batch_id': batch['id'], 'code': voucher['code'],
                                       'items': voucher['items']})).hexdigest()
    assert voucher['digest'] == expect


def test_handover_retry_never_duplicates_voucher(client, engine):
    op = auth(client, 'operator')
    case, run = submit_case(client, op, 'T20-frame01.dpx', 'T20-1')
    execute(engine, run, case['payload'])
    batch = make_batch(client, op, 'T20 卷', ['T20-frame01.dpx'])

    first = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert first.status_code == 201
    for _ in range(3):  # 交接中断后重试：返回原凭证，不重复开凭证
        retry = client.post(f"/batches/{batch['id']}/handover", headers=op)
        assert retry.status_code == 200
        assert retry.json()['id'] == first.json()['id']
        assert retry.json()['code'] == first.json()['code']
    detail = client.get(f"/batches/{batch['id']}", headers=op).json()
    assert detail['voucher']['code'] == first.json()['code']


def test_handover_waits_for_every_negative(client, engine):
    op = auth(client, 'operator')
    batch = make_batch(client, op, 'T30 卷', ['T30-a.dpx', 'T30-b.dpx'])

    resp = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert resp.status_code == 409
    assert '尚未送检' in resp.json()['detail']

    case_a, run_a = submit_case(client, op, 'T30-a.dpx', 'T30-1')
    execute(engine, run_a, case_a['payload'])
    resp = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert resp.status_code == 409 and 'T30-b.dpx' in resp.json()['detail']

    case_b, run_b = submit_case(client, op, 'T30-b.dpx', 'T30-2')
    execute(engine, run_b, case_b['payload'])
    resp = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert resp.status_code == 201, resp.text
    assert {item['asset'] for item in resp.json()['items']} == {'T30-a.dpx', 'T30-b.dpx'}


def test_resubmitted_negative_waits_for_new_version(client, engine):
    """重新送检后，迟到的旧结果不能顶上：交接要等新版验完，并钉在新版报告上。"""
    op = auth(client, 'operator')
    case1, run1 = submit_case(client, op, 'T40.dpx', 'T40-1')
    execute(engine, run1, case1['payload'])  # 旧送检版已通过
    batch = make_batch(client, op, 'T40 卷', ['T40.dpx'])

    case2, run2 = submit_case(client, op, 'T40.dpx', 'T40-2')  # 重新送检，仍在排队
    resp = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert resp.status_code == 409
    assert '正在查验中' in resp.json()['detail']

    execute(engine, run2, case2['payload'])
    resp = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert resp.status_code == 201, resp.text
    (item,) = resp.json()['items']
    assert (item['run_id'], item['snapshot']) == (run2['id'], 'T40-2')


def test_failed_latest_version_blocks_handover(client, engine):
    """最新送检版未通过还原校验时，旧版通过记录不能顶上。"""
    op = auth(client, 'operator')
    case1, run1 = submit_case(client, op, 'T50.dpx', 'T50-1')
    execute(engine, run1, case1['payload'])
    faults = [{'snapshot_id': 'T50-2', 'chunk_index': 1, 'fault': 'corrupt'}]
    case2, run2 = submit_case(client, op, 'T50.dpx', 'T50-2', faults=faults)
    execute(engine, run2, case2['payload'])
    batch = make_batch(client, op, 'T50 卷', ['T50.dpx'])

    resp = client.post(f"/batches/{batch['id']}/handover", headers=op)
    assert resp.status_code == 409
    assert '未通过还原校验' in resp.json()['detail']


def test_handed_over_batch_checked_against_original_voucher(client, engine):
    """已交接的批次按原凭证查验：之后再送检新版，凭证仍对应当时的报告。"""
    op = auth(client, 'operator')
    case1, run1 = submit_case(client, op, 'T60.dpx', 'T60-1')
    execute(engine, run1, case1['payload'])
    batch = make_batch(client, op, 'T60 卷', ['T60.dpx'])
    voucher = client.post(f"/batches/{batch['id']}/handover", headers=op).json()

    case2, run2 = submit_case(client, op, 'T60.dpx', 'T60-2')
    execute(engine, run2, case2['payload'])  # 交接后又送检并通过新版

    check = client.get(f"/vouchers/{voucher['code']}", headers=op)
    assert check.status_code == 200, check.text
    (item,) = check.json()['items']
    assert item['run_id'] == run1['id'] and item['consistent'] is True
    assert check.json()['batch_title'] == 'T60 卷'

    detail = client.get(f"/batches/{batch['id']}", headers=op).json()
    assert detail['voucher']['code'] == voucher['code']  # 凭证不变
    assert detail['items'][0]['run_id'] == run2['id']    # 当前进度以最新送检版为准


def test_concurrent_handover_issues_single_voucher(client, engine):
    op = auth(client, 'operator')
    case, run = submit_case(client, op, 'T70.dpx', 'T70-1')
    execute(engine, run, case['payload'])
    batch = make_batch(client, op, 'T70 卷', ['T70.dpx'])

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: client.post(f"/batches/{batch['id']}/handover", headers=op), range(5)))
    assert {resp.status_code for resp in responses} <= {200, 201}
    codes = {resp.json()['code'] for resp in responses}
    assert len(codes) == 1
    detail = client.get(f"/batches/{batch['id']}", headers=op).json()
    assert detail['voucher']['code'] == codes.pop()


def test_batch_form_validation(client, engine):
    op = auth(client, 'operator')
    assert client.post('/batches', json={'title': 'T80', 'assets': []}, headers=op).status_code == 422
    assert client.post('/batches', json={'title': 'T80', 'assets': ['a.dpx', 'a.dpx']}, headers=op).status_code == 422
    assert client.post('/batches', json={'title': '   ', 'assets': ['a.dpx']}, headers=op).status_code == 422
    assert client.post('/batches', json={'title': 'T80', 'assets': [' a.dpx ']}, headers=op).status_code == 422
    assert client.post('/batches/999/handover', headers=op).status_code == 404


def test_handover_roles(client, engine):
    op = auth(client, 'operator')
    reviewer = auth(client, 'reviewer')
    viewer = auth(client, 'viewer')
    batch = make_batch(client, op, 'T90 卷', ['T90.dpx'])

    assert client.post('/batches', json={'title': 'x', 'assets': ['x.dpx']}, headers=viewer).status_code == 403
    assert client.post(f"/batches/{batch['id']}/handover", headers=reviewer).status_code == 403
    assert client.get('/batches', headers=viewer).status_code == 200
    assert client.get(f"/batches/{batch['id']}", headers=viewer).status_code == 200
    assert client.get('/vouchers/VCH-999999', headers=viewer).status_code == 404
