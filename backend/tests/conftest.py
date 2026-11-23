import os

os.environ.setdefault('AUTH_KEY', 'test-handover-key')
os.environ.setdefault('SEED_DEMO', 'false')
# 指向不可达地址即可：enqueue 对唤醒信号失败做了容忍，测试不依赖 Redis。
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6390/0')

import pytest
from sqlalchemy import create_engine

from src.models.tables import schema, users
from src.services.security import hash_password


@pytest.fixture(scope='session')
def engine(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('db')}/handover.db"
    test_engine = create_engine(url)
    import src.controllers.workflow as controllers
    import src.main as main
    import src.routes.workflow as routes
    import src.services.handover as handover
    import src.services.security as security
    import src.services.workflow as workflow
    for module in (workflow, handover, security, controllers, routes, main):
        module.engine = test_engine
    schema.create_all(test_engine)
    with test_engine.begin() as conn:
        for role in ('operator', 'reviewer', 'viewer'):
            conn.execute(users.insert().values(username=role, role=role,
                        password_hash=hash_password('test-pass')))
    return test_engine


@pytest.fixture()
def client(engine):
    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as test_client:
        yield test_client
