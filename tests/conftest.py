"""测试公共夹具。

环境变量会跨测试泄漏，因此每条测试前清掉 ``CTRLSTORE_*``；
需要构造配置的测试再显式写入。

数据库夹具分三层，便于按需使用：

* ``settings``  只有配置，数据目录在临时目录里；
* ``database``  已迁移到最新版本的 session 工厂，用来断言库内状态；
* ``client``    FastAPI 测试客户端，``owner_client`` 则是登录后的客户端。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker
from tests.auth_helpers import OWNER_PASSWORD

from ctrlstore.auth import service
from ctrlstore.config import Settings
from ctrlstore.db import create_engine, run_migrations
from ctrlstore.main import create_app
from ctrlstore.storage import LocalStorage

#: 测试用的假密钥，只在进程内使用。
TEST_SECRETS = {
    "session_secret": "session-secret-for-tests-only-0001",
    "pageview_hmac_secret": "pageview-secret-for-tests-only-001",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("CTRLSTORE_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """数据目录落在临时目录里的完整配置。"""
    return Settings(data_dir=tmp_path / "data", **TEST_SECRETS)


@pytest.fixture
def database(settings: Settings) -> Iterator[sessionmaker]:
    """临时库：建表并返回 session 工厂，用来断言库内状态。"""
    engine = create_engine(settings)
    run_migrations(settings, engine=engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def db(settings: Settings, database) -> Iterator[DbSession]:
    """准备数据用的会话，离开时提交。

    单独建一个引擎：生产上每个进程只有一条连接，测试里“准备数据”“断言状态”
    “应用处理请求”会同时要连接，各自一条才不会互相等待。这也正是部署形态
    （API 两进程 + worker）的连接关系。
    """
    engine = create_engine(settings)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
        session.commit()
    engine.dispose()


@pytest.fixture
def owner(db: DbSession):
    """唯一所有者。立即提交，应用侧的会话才查得到。"""
    user = service.create_owner(db, username="owner", password=OWNER_PASSWORD)
    db.commit()
    return user


@pytest.fixture
def storage(settings: Settings) -> LocalStorage:
    """与配置一致的存储实现（原件目录、临时目录）。"""
    return LocalStorage.from_settings(settings)


@pytest.fixture
def client(settings: Settings, database) -> Iterator[TestClient]:
    """已启动的应用（启动时自动执行迁移）。"""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def owner_client(client: TestClient, owner) -> TestClient:
    """已登录的客户端：Cookie 存在 client 里，后续请求自动带上。"""
    response = client.post(
        "/api/admin/v1/session",
        json={"username": "owner", "password": OWNER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client
