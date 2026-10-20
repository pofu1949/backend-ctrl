"""命令行全链路：建所有者、建编辑、授权、签发令牌、重置密码。

这些用例直接调用 ``main``，走的是与生产同一个入口。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from tests.conftest import TEST_SECRETS

from ctrlstore.auth import passwords, service
from ctrlstore.cli import main
from ctrlstore.db import create_engine, run_migrations
from ctrlstore.models import AuditLog, User


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> Path:
    """一份可用配置，并跑完 init（所有者 owner / owner-password-01）。"""
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])
    config = tmp_path / "config.toml"
    config.write_text(f"data_dir = '{(tmp_path / 'data').as_posix()}'\n", encoding="utf-8")
    _feed(monkeypatch, "owner-password-01")
    assert main(["--config", str(config), "init", "--password-stdin"]) == 0
    capsys.readouterr()
    return config


def _feed(monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{password}\n"))


def _db(config: Path, tmp_path: Path):
    from ctrlstore.config import load_settings

    settings = load_settings(config)
    engine = create_engine(settings)
    run_migrations(settings, engine=engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def test_init_creates_hashed_owner(workspace: Path) -> None:
    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        owner = service.get_owner(db)
        assert owner is not None
        assert owner.username == "owner"
        assert owner.password_hash.startswith("$argon2id$")
        assert passwords.verify_password(owner.password_hash, "owner-password-01")
    engine.dispose()


def test_user_create_with_preset_grants_all_eight(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _feed(monkeypatch, "editor-password-01")

    code = main(
        [
            "--config",
            str(workspace),
            "user",
            "create",
            "editor",
            "--preset",
            "editor",
            "--password-stdin",
        ]
    )

    assert code == 0
    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        editor = service.find_user(db, "editor")
        assert editor is not None
        assert len(editor.permissions) == 8
    engine.dispose()


def test_user_create_without_permissions(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, "editor-password-01")

    assert main(["--config", str(workspace), "user", "create", "editor", "--password-stdin"]) == 0

    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        assert service.find_user(db, "editor").permissions == []
    engine.dispose()


def test_grant_then_revoke_permission(workspace: Path) -> None:
    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        service.create_editor(db, username="editor", password="editor-password-01")
        db.commit()
    engine.dispose()

    assert (
        main(
            [
                "--config",
                str(workspace),
                "user",
                "grant",
                "editor",
                "--permission",
                "asset.read",
            ]
        )
        == 0
    )
    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        assert [p.permission for p in service.find_user(db, "editor").permissions] == ["asset.read"]
    engine.dispose()

    assert (
        main(
            [
                "--config",
                str(workspace),
                "user",
                "revoke",
                "editor",
                "--permission",
                "asset.read",
            ]
        )
        == 0
    )
    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        assert service.find_user(db, "editor").permissions == []
    engine.dispose()


def test_disable_user_writes_audit(workspace: Path) -> None:
    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        service.create_editor(db, username="editor", password="editor-password-01")
        db.commit()
    engine.dispose()

    assert main(["--config", str(workspace), "user", "disable", "editor"]) == 0

    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        assert service.find_user(db, "editor").disabled_at is not None
        actions = [row.action for row in db.scalars(select(AuditLog)).all()]
        assert "user_disabled" in actions
    engine.dispose()


def test_owner_reset_password_only_resets_existing_owner(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _feed(monkeypatch, "new-owner-password-01")

    code = main(["--config", str(workspace), "owner", "reset-password", "--password-stdin"])

    assert code == 0
    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        owner = service.get_owner(db)
        assert passwords.verify_password(owner.password_hash, "new-owner-password-01")
        assert len(db.scalars(select(User)).all()) == 1
    engine.dispose()


def test_owner_reset_password_fails_when_no_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("CTRLSTORE_SESSION_SECRET", TEST_SECRETS["session_secret"])
    monkeypatch.setenv("CTRLSTORE_PAGEVIEW_HMAC_SECRET", TEST_SECRETS["pageview_hmac_secret"])
    config = tmp_path / "config.toml"
    config.write_text(f"data_dir = '{(tmp_path / 'data').as_posix()}'\n", encoding="utf-8")
    factory, engine = _db(config, tmp_path)
    engine.dispose()
    _feed(monkeypatch, "whatever-password-01")

    code = main(["--config", str(config), "owner", "reset-password", "--password-stdin"])

    assert code == 1
    assert "所有者不存在" in capsys.readouterr().err


def test_site_and_token_commands(workspace: Path, capsys) -> None:
    assert main(["--config", str(workspace), "site", "create", "main-site", "--name", "主站"]) == 0
    capsys.readouterr()

    assert main(["--config", str(workspace), "token", "create", "--site", "main-site"]) == 0
    output = capsys.readouterr().out
    plaintext = output.strip().splitlines()[-1]
    assert "." in plaintext

    assert main(["--config", str(workspace), "token", "list"]) == 0
    assert "site" in capsys.readouterr().out

    factory, engine = _db(workspace, workspace.parent)
    with factory() as db:
        stored = db.scalars(select(User)).all()
        assert stored  # 只是确认库可查；令牌明文不入库
        from ctrlstore.models import ApiToken

        row = db.scalars(select(ApiToken)).one()
        assert row.token_hash != plaintext
    engine.dispose()


def test_duplicate_username_is_reported(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _feed(monkeypatch, "editor-password-01")
    main(["--config", str(workspace), "user", "create", "editor", "--password-stdin"])
    capsys.readouterr()
    _feed(monkeypatch, "editor-password-02")

    code = main(["--config", str(workspace), "user", "create", "editor", "--password-stdin"])

    assert code == 1
    assert "已存在" in capsys.readouterr().err
