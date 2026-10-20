"""命令行入口（企划第 13.4 节）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import TEST_SECRETS

from ctrlstore.cli import main


def _config_file(tmp_path: Path) -> Path:
    """写一份最小配置。TOML 用单引号字面量，Windows 路径才不会触发转义。"""
    config = tmp_path / "config.toml"
    config.write_text(f"data_dir = '{(tmp_path / 'data').as_posix()}'\n", encoding="utf-8")
    return config


def _provide_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])


def test_init_creates_directories_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _provide_secrets(monkeypatch)
    config = _config_file(tmp_path)

    exit_code = main(["--config", str(config), "init"])

    assert exit_code == 0
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "data" / "originals").is_dir()
    assert (tmp_path / "data" / "public").is_dir()
    assert (tmp_path / "data" / "ctrlstore.db").is_file()
    assert "已迁移" in capsys.readouterr().out


def test_init_without_secrets_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config_file(tmp_path)

    exit_code = main(["--config", str(config), "init"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "配置错误" in captured.err
    assert "session_secret" in captured.err


def test_check_config_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config_file(tmp_path)
    _provide_secrets(monkeypatch)

    exit_code = main(["--config", str(config), "check-config"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "配置有效" in output
    assert str(tmp_path / "data" / "ctrlstore.db") in output


def test_serve_uses_the_given_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：Uvicorn 曾按默认路径重新加载配置，丢掉 ``--config``。"""
    import uvicorn

    captured: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    _provide_secrets(monkeypatch)
    config = _config_file(tmp_path)

    exit_code = main(["--config", str(config), "serve", "--port", "8123"])

    assert exit_code == 0
    app_settings = captured["app"].state.settings
    assert app_settings.data_dir == tmp_path / "data"
    assert captured["kwargs"] == {
        "host": "127.0.0.1",
        "port": 8123,
        "log_config": None,
        "access_log": False,
    }
