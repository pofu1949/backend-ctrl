"""幂等键的规则（企划第 10.3 节）。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session as DbSession

from ctrlstore import idempotency
from ctrlstore.errors import ApiError

SCOPE = {
    "actor_id": "user-1",
    "method": "POST",
    "path": "/api/admin/v1/uploads/x/complete",
    "key": "k1",
}


def test_missing_key_returns_none(db: DbSession) -> None:
    assert idempotency.lookup(db, **SCOPE, request_hash="h1") is None


def test_same_key_and_body_replays(db: DbSession) -> None:
    idempotency.remember(db, **SCOPE, request_hash="h1", status=200, body={"ok": True})
    db.commit()

    stored = idempotency.lookup(db, **SCOPE, request_hash="h1")

    assert stored is not None
    assert stored.status == 200
    assert stored.body == {"ok": True}


def test_same_key_with_other_body_conflicts(db: DbSession) -> None:
    idempotency.remember(db, **SCOPE, request_hash="h1", status=200, body={})
    db.commit()

    with pytest.raises(ApiError) as excinfo:
        idempotency.lookup(db, **SCOPE, request_hash="h2")

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "conflict"


def test_key_is_scoped_per_actor_and_path(db: DbSession) -> None:
    idempotency.remember(db, **SCOPE, request_hash="h1", status=200, body={})
    db.commit()

    assert idempotency.lookup(db, **{**SCOPE, "actor_id": "user-2"}, request_hash="h1") is None
    assert idempotency.lookup(db, **{**SCOPE, "path": "/other"}, request_hash="h1") is None


def test_expired_key_is_forgotten(db: DbSession) -> None:
    from datetime import timedelta

    from ctrlstore.models import IdempotencyKey, utcnow

    idempotency.remember(db, **SCOPE, request_hash="h1", status=200, body={})
    db.commit()
    row = db.query(IdempotencyKey).one()
    row.created_at = utcnow() - timedelta(hours=idempotency.RETENTION_HOURS + 1)
    db.commit()

    assert idempotency.lookup(db, **SCOPE, request_hash="h1") is None


def test_purge_removes_expired_rows(db: DbSession) -> None:
    from datetime import timedelta

    from ctrlstore.models import IdempotencyKey, utcnow

    idempotency.remember(db, **SCOPE, request_hash="h1", status=200, body={})
    db.commit()
    row = db.query(IdempotencyKey).one()
    row.created_at = utcnow() - timedelta(hours=idempotency.RETENTION_HOURS + 1)
    db.commit()

    assert idempotency.purge_expired(db) == 1
