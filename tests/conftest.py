"""测试公共夹具。

环境变量会跨测试泄漏，因此每条测试前清掉 ``CTRLSTORE_*``；
需要构造配置的测试再显式写入。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ctrlstore.config import Settings

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
