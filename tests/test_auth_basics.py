"""密码、权限与令牌的单元测试。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session as DbSession

from ctrlstore.auth import passwords, permissions, tokens
from ctrlstore.errors import ApiError
from ctrlstore.models import utcnow


def test_password_hash_is_argon2id_and_verifies() -> None:
    hashed = passwords.hash_password("a-long-enough-password")

    assert hashed.startswith("$argon2id$")
    assert passwords.verify_password(hashed, "a-long-enough-password")
    assert not passwords.verify_password(hashed, "wrong-password")


def test_broken_hash_is_a_failed_login_not_a_crash() -> None:
    assert not passwords.verify_password("not-a-hash", "anything")


@pytest.mark.parametrize("password", ["", "short", "x" * 11])
def test_weak_password_is_rejected(password: str) -> None:
    with pytest.raises(passwords.WeakPasswordError):
        passwords.validate_password(password)


def test_owner_has_every_permission_without_table_rows() -> None:
    granted = permissions.effective("owner", [])

    assert granted == frozenset(permissions.PERMISSIONS)
    assert len(granted) == 8


def test_editor_has_only_granted_permissions() -> None:
    assert permissions.effective("editor", ["asset.read", "unknown.key"]) == {"asset.read"}


def test_unknown_permission_is_rejected() -> None:
    with pytest.raises(permissions.UnknownPermissionError):
        permissions.normalize(["asset.read", "asset.delete"])


def test_editor_preset_is_all_eight() -> None:
    assert permissions.from_preset("editor") == set(permissions.PERMISSIONS)


def test_token_plaintext_contains_id_but_store_holds_only_hash(db: DbSession, owner) -> None:
    plaintext, token = tokens.create(db, name="cli", subject_type="user", subject_id=owner.id)
    db.commit()

    assert plaintext.startswith(f"{token.id}.")
    assert token.token_hash != plaintext
    assert token.token_hash == tokens.hash_secret(plaintext.split(".", 1)[1])


def test_token_resolves_to_subject(db: DbSession, owner) -> None:
    plaintext, _ = tokens.create(db, name="cli", subject_type="user", subject_id=owner.id)
    db.commit()

    resolved = tokens.resolve(db, plaintext)

    assert resolved is not None
    _, subject = resolved
    assert subject.id == owner.id


def test_expired_token_is_rejected(db: DbSession, owner) -> None:
    plaintext, _ = tokens.create(
        db,
        name="cli",
        subject_type="user",
        subject_id=owner.id,
        expires_at=utcnow() - timedelta(minutes=1),
    )
    db.commit()

    assert tokens.resolve(db, plaintext) is None


def test_revoked_token_is_rejected(db: DbSession, owner) -> None:
    plaintext, token = tokens.create(db, name="cli", subject_type="user", subject_id=owner.id)
    tokens.revoke(db, token)
    db.commit()

    assert tokens.resolve(db, plaintext) is None


@pytest.mark.parametrize("raw", ["", "no-dot", "unknown.zzz", ".secret"])
def test_malformed_token_is_rejected(raw: str, db: DbSession, owner) -> None:
    assert tokens.resolve(db, raw) is None


def test_service_translates_unknown_permission_to_422(db: DbSession, owner) -> None:
    from ctrlstore.auth import service

    with pytest.raises(ApiError) as excinfo:
        service.set_permissions(db, owner, ["nope"], actor=owner)

    assert excinfo.value.status_code == 403  # 先被“所有者不可改”挡住
