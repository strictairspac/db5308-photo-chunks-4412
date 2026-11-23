import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from src.config.settings import engine
from src.constants.workflow import Status
from src.models.tables import (batch_items, batches, evidence, runs,
                               voucher_items, vouchers)
from src.services.workflow import lock_assets, log
from src.utils.encoding import canonical
from src.utils.time import utc_time

MAX_BATCH_ASSETS = 100


def _for_update(conn, query):
    """行锁读取（当前读），不会提前固定事务的一致性读视图。SQLite 仅用于演示单测，无行锁。"""
    return query.with_for_update() if conn.dialect.name != 'sqlite' else query


def _batch_or_404(conn, batch_id, lock=False):
    query = select(batches).where(batches.c.id == batch_id)
    if lock:
        query = _for_update(conn, query)
    row = conn.execute(query).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该批次')
    return dict(row)


def _readiness(conn, asset):
    """批内一份底片当前的交接就绪状态：以最新一次送检（执行编号最大者）为准。"""
    run = conn.execute(select(runs).where(runs.c.asset == asset)
                       .order_by(runs.c.id.desc()).limit(1)).mappings().first()
    if run is None:
        return {'asset': asset, 'run_id': None, 'snapshot': None, 'status': None,
                'passed': False, 'reason': '尚未送检'}
    row = {'asset': asset, 'run_id': run['id'], 'snapshot': run['snapshot'], 'status': run['status'], 'passed': False}
    if run['status'] != Status.COMPLETE:
        row['reason'] = ('最新送检版正在查验中' if run['status'] in (Status.QUEUED, Status.RUNNING)
                         else '最新送检版执行失败')
        return row
    report = conn.execute(select(evidence).where(evidence.c.run_id == run['id'])).mappings().first()
    if report and report['report']['metrics'].get('还原校验通过') == 1:
        row.update(passed=True, evidence_id=report['id'], evidence_digest=report['digest'])
    else:
        row['reason'] = '最新送检版未通过还原校验'
    return row


def _voucher_view(conn, voucher):
    items = conn.execute(select(voucher_items).where(voucher_items.c.voucher_id == voucher['id'])
                         .order_by(voucher_items.c.id)).mappings().all()
    return {'id': voucher['id'], 'code': voucher['code'], 'batch_id': voucher['batch_id'],
            'digest': voucher['digest'], 'issued_by': voucher['issued_by'], 'issued_at': voucher['issued_at'],
            'items': [{'asset': item['asset'], 'snapshot': item['snapshot'], 'run_id': item['run_id'],
                       'evidence_id': item['evidence_id'], 'evidence_digest': item['evidence_digest']}
                      for item in items]}


def _detail(conn, batch_id):
    batch = _batch_or_404(conn, batch_id)
    items = conn.execute(select(batch_items).where(batch_items.c.batch_id == batch_id)
                         .order_by(batch_items.c.id)).mappings().all()
    voucher = conn.execute(select(vouchers).where(vouchers.c.batch_id == batch_id)).mappings().first()
    return {**batch, 'items': [_readiness(conn, item['asset']) for item in items],
            'voucher': _voucher_view(conn, voucher) if voucher else None}


def make_batch(title, assets, actor):
    title = (title or '').strip()
    if not title:
        raise HTTPException(422, '批次名称不能为空')
    if not isinstance(assets, list) or not 1 <= len(assets) <= MAX_BATCH_ASSETS:
        raise HTTPException(422, f'批内底片须为 1 至 {MAX_BATCH_ASSETS} 份')
    names = []
    for asset in assets:
        if not isinstance(asset, str) or not 1 <= len(asset) <= 120 or asset != asset.strip():
            raise HTTPException(422, '底片文件名须为 1 至 120 个字符，且首尾不能有空白')
        if asset in names:
            raise HTTPException(422, f'批内底片重复：{asset}')
        names.append(asset)
    with engine.begin() as conn:
        batch_id = conn.execute(batches.insert().values(slug=uuid.uuid4().hex, title=title,
                                owner=actor, created_at=utc_time())).inserted_primary_key[0]
        for asset in names:
            conn.execute(batch_items.insert().values(batch_id=batch_id, asset=asset))
        log(conn, actor, 'create_batch', batch_id)
        return _detail(conn, batch_id)


def list_batches():
    with engine.connect() as conn:
        rows = conn.execute(select(batches).order_by(batches.c.id.desc()).limit(100)).mappings().all()
        return [_detail(conn, row['id']) for row in rows]


def detail(batch_id):
    with engine.connect() as conn:
        return _detail(conn, batch_id)


def handover(batch_id, actor):
    """核发领用凭证。整批一个凭证，每份底片钉在交接当时的报告上。

    重试安全：凭证按批次唯一且编号确定，交接中断后重试返回原凭证，不重复开凭证。
    """
    with engine.begin() as conn:
        _batch_or_404(conn, batch_id, lock=True)
        items = conn.execute(_for_update(conn, select(batch_items)
                             .where(batch_items.c.batch_id == batch_id))).mappings().all()
        lock_assets(conn, [item['asset'] for item in items])
        # 一致读从所有行锁就位之后开始：并发的新送检要么已可见，要么被锁挡到交接提交后。
        voucher = conn.execute(select(vouchers).where(vouchers.c.batch_id == batch_id)).mappings().first()
        if voucher is not None:
            return _voucher_view(conn, voucher), False
        checks = [_readiness(conn, item['asset']) for item in items]
        blocked = [f"底片 {row['asset']}{row['reason']}" for row in checks if not row['passed']]
        if blocked:
            raise HTTPException(409, '批次尚未就绪：' + '；'.join(blocked))
        code = f'VCH-{batch_id:06d}'
        pinned = [{'asset': row['asset'], 'snapshot': row['snapshot'], 'run_id': row['run_id'],
                   'evidence_id': row['evidence_id'], 'evidence_digest': row['evidence_digest']} for row in checks]
        digest = hashlib.sha256(canonical({'batch_id': batch_id, 'code': code, 'items': pinned})).hexdigest()
        voucher_id = conn.execute(vouchers.insert().values(batch_id=batch_id, code=code, digest=digest,
                                  issued_by=actor, issued_at=utc_time())).inserted_primary_key[0]
        for item in pinned:
            conn.execute(voucher_items.insert().values(voucher_id=voucher_id, **item))
        log(conn, actor, 'handover', code)
        row = conn.execute(select(vouchers).where(vouchers.c.id == voucher_id)).mappings().first()
        return _voucher_view(conn, row), True


def voucher_check(code):
    """按原凭证查验：每份底片仍对应交接当时的报告，并核对证据校验码未变。"""
    with engine.connect() as conn:
        voucher = conn.execute(select(vouchers).where(vouchers.c.code == code)).mappings().first()
        if voucher is None:
            raise HTTPException(404, '找不到该凭证')
        batch = _batch_or_404(conn, voucher['batch_id'])
        view = _voucher_view(conn, voucher)
        for item in view['items']:
            report = conn.execute(select(evidence).where(evidence.c.id == item['evidence_id'])).mappings().first()
            item['consistent'] = bool(report and report['digest'] == item['evidence_digest'])
            item['summary'] = report['report']['summary'] if report else None
        return {**view, 'batch_title': batch['title']}
