from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('asset', String(120), index=True),
    Column('snapshot', String(64)),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
batches = Table('handover_batches', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('owner', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
batch_items = Table('handover_batch_items', schema,
    Column('id', Integer, primary_key=True),
    Column('batch_id', ForeignKey('handover_batches.id'), nullable=False, index=True),
    Column('asset', String(120), nullable=False),
    UniqueConstraint('batch_id', 'asset'))
# 每份底片一行的互斥锁表：送检（新建执行）与交接（核发凭证）都先锁对应行，
# 保证“重新送检要等新版验完、迟到的旧结果不能顶上”在并发下成立。
asset_locks = Table('handover_asset_locks', schema,
    Column('asset', String(120), primary_key=True))
vouchers = Table('handover_vouchers', schema,
    Column('id', Integer, primary_key=True),
    Column('batch_id', ForeignKey('handover_batches.id'), nullable=False, unique=True),
    Column('code', String(40), nullable=False, unique=True),
    Column('digest', String(64), nullable=False),
    Column('issued_by', String(40), nullable=False),
    Column('issued_at', String(32), nullable=False))
voucher_items = Table('handover_voucher_items', schema,
    Column('id', Integer, primary_key=True),
    Column('voucher_id', ForeignKey('handover_vouchers.id'), nullable=False, index=True),
    Column('asset', String(120), nullable=False),
    Column('snapshot', String(64), nullable=False),
    Column('run_id', Integer, nullable=False),
    Column('evidence_id', Integer, nullable=False),
    Column('evidence_digest', String(64), nullable=False),
    UniqueConstraint('voucher_id', 'asset'))
