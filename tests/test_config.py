"""配置加载与校验（企划第 17.4 节、规划第 1 部分）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import TEST_SECRETS

from ctrlstore.config import (
    ConfigError,
    Settings,
    load_settings,
)

CONFIG_TEMPLATE = """
data_dir = {data_dir!r}
public_base_url = "http://192.168.1.10:8000"
min_free_bytes = 1048576
"""


def _write_config(path: Path, data_dir: Path, extra: str = "") -> Path:
    path.write_text(CONFIG_TEMPLATE.format(data_dir=str(data_dir)) + extra, encoding="utf-8")
    return path


def test_directories_default_to_data_dir(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", **TEST_SECRETS)

    assert settings.db_path == tmp_path / "data" / "ctrlstore.db"
    assert settings.originals_dir == tmp_path / "data" / "originals"
    assert settings.public_dir == tmp_path / "data" / "public"
    assert settings.tmp_dir == tmp_path / "data" / "tmp"


def test_explicit_directories_win(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        public_dir=tmp_path / "www",
        **TEST_SECRETS,
    )

    assert settings.public_dir == tmp_path / "www"


def test_database_url_uses_forward_slashes(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", **TEST_SECRETS)

    assert settings.db_url == f"sqlite:///{(tmp_path / 'data' / 'ctrlstore.db').as_posix()}"
    assert "\\" not in settings.db_url


def test_relative_data_dir_is_rejected() -> None:
    with pytest.raises(ValueError, match="绝对路径"):
        Settings(data_dir=Path("data"), **TEST_SECRETS)


@pytest.mark.parametrize(
    "url", ["ftp://example.com", "example.com", "http:///path", "http://a/?x=1"]
)
def test_public_base_url_must_be_clean_absolute_url(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, public_base_url=url, **TEST_SECRETS)


def test_public_base_url_loses_trailing_slash(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path, public_base_url="https://media.example.com/", **TEST_SECRETS
    )

    assert settings.public_base_url == "https://media.example.com"


@pytest.mark.parametrize("secret", ["short", "x" * 31])
def test_short_secret_is_rejected(secret: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="至少"):
        Settings(data_dir=tmp_path, session_secret=secret, pageview_hmac_secret=secret)


def test_missing_secrets_refuse_to_start(tmp_path: Path) -> None:
    config = _write_config(tmp_path / "config.toml", tmp_path / "data")

    with pytest.raises(ConfigError) as excinfo:
        load_settings(config)

    message = str(excinfo.value)
    assert "session_secret" in message
    assert "CTRLSTORE_SESSION_SECRET" in message


def test_toml_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(tmp_path / "config.toml", tmp_path / "data")
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])

    settings = load_settings(config)

    assert settings.data_dir == tmp_path / "data"
    assert settings.public_base_url == "http://192.168.1.10:8000"
    assert settings.min_free_bytes == 1048576
    assert settings.config_path == config


def test_environment_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(tmp_path / "config.toml", tmp_path / "data")
    monkeypatch.setenv("CTRLSTORE_DATA_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])

    settings = load_settings(config)

    assert settings.data_dir == tmp_path / "elsewhere"
    assert settings.db_path == tmp_path / "elsewhere" / "ctrlstore.db"


def test_secrets_in_main_config_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])
    config = _write_config(
        tmp_path / "config.toml",
        tmp_path / "data",
        extra='session_secret = "{}"'.format("x" * 40),
    )

    with pytest.raises(ConfigError, match="session_secret"):
        load_settings(config)


def test_secrets_file_is_used(tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(
        "[secrets]\n"
        f'session_secret = "{TEST_SECRETS["session_secret"]}"\n'
        f'pageview_hmac_secret = "{TEST_SECRETS["pageview_hmac_secret"]}"\n',
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path / "config.toml",
        tmp_path / "data",
        extra='secrets_file = "secrets.toml"\n',
    )

    settings = load_settings(config)

    assert settings.session_secret == TEST_SECRETS["session_secret"]


def test_unreadable_secrets_file_is_reported(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path / "config.toml",
        tmp_path / "data",
        extra='secrets_file = "missing.toml"\n',
    )

    with pytest.raises(ConfigError, match="不存在"):
        load_settings(config)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o602])
def test_secrets_file_must_be_private(mode: int, tmp_path: Path) -> None:
    from ctrlstore.config import require_private_mode

    with pytest.raises(ConfigError, match="权限过宽"):
        require_private_mode(mode, tmp_path / "secrets.toml")


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_private_mode_is_accepted(mode: int, tmp_path: Path) -> None:
    from ctrlstore.config import require_private_mode

    require_private_mode(mode, tmp_path / "secrets.toml")


def test_utf8_bom_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 编辑器常给配置文件加 BOM，tomllib 本身不接受。"""
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])
    config = tmp_path / "config.toml"
    config.write_bytes(
        b"\xef\xbb\xbf" + f"data_dir = '{(tmp_path / 'data').as_posix()}'\n".encode()
    )

    settings = load_settings(config)

    assert settings.data_dir == tmp_path / "data"


def test_explicit_config_path_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])
    missing = tmp_path / "no-such-config.toml"

    with pytest.raises(ConfigError, match="不存在"):
        load_settings(missing)


def test_default_config_path_may_be_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没给路径时，默认配置文件缺失只表示“全部用默认值”。"""
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])
    monkeypatch.setenv("CTRLSTORE_DATA_DIR", str(tmp_path / "data"))

    settings = load_settings()

    assert settings.data_dir == tmp_path / "data"
