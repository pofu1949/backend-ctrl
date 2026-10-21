"""页面访问上报（规划第 8 部分）。

覆盖：站点令牌限制、event_id 去重、人数与次数的区别、上海时区归日、
不计数路径与预取/爬虫、隐私（只存 HMAC）、路径溢出并入「其他」、
400 天明细清理、上报限速。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import admin_headers
from tests.test_public_api import _site, _token

from ctrlstore.auth import service
from ctrlstore.errors import ApiError
from ctrlstore.models import (
    MAX_PAGEVIEW_PATHS,
    PAGEVIEW_OTHER_PATH,
    PageviewDaily,
    PageviewEvent,
    PageviewPath,
    PageviewVisitor,
)
from ctrlstore.stats import pageviews
from ctrlstore.stats.throttle import PageviewThrottle

PUBLIC = "/api/public/v1"
API = "/api/admin/v1"
SECRET = "pageview-secret-for-tests-only-001"


def _report(
    client: TestClient,
    token: str,
    *,
    event: str = "e1",
    visitor: str = "v1",
    path: str = "/",
    user_agent: str | None = None,
    headers: dict[str, str] | None = None,
):
    payload: dict[str, object] = {"event_id": event, "visitor_id": visitor, "path": path}
    if user_agent is not None:
        payload["user_agent"] = user_agent
    request_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        request_headers.update(headers)
    return client.post(f"{PUBLIC}/pageviews", json=payload, headers=request_headers)


def _site_token(client: TestClient) -> str:
    """建一个站点并签发令牌。每个测试的库都是空的，所以直接建 main。"""
    _site(client, "main")
    return _token(client, "main")


def _daily(db: DbSession, day: str) -> PageviewDaily | None:
    db.expire_all()
    return db.scalar(select(PageviewDaily).where(PageviewDaily.day == day))


def _new_site(db: DbSession, key: str = "s"):
    site = service.create_site(db, key=key, name=key, actor=None)
    db.commit()
    return site


# —— 身份与基本计数 ——


def test_report_needs_site_token(owner_client: TestClient, client: TestClient) -> None:
    anonymous = client.post(
        f"{PUBLIC}/pageviews",
        json={"event_id": "e", "visitor_id": "v", "path": "/"},
    )
    assert anonymous.status_code == 401

    owner_token = owner_client.post(
        f"{API}/tokens",
        json={"subject_type": "user", "subject_key": "owner"},
        headers=admin_headers(owner_client),
    ).json()["token"]
    response = client.post(
        f"{PUBLIC}/pageviews",
        json={"event_id": "e", "visitor_id": "v", "path": "/"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert response.status_code == 403


def test_first_report_counts_one_person_once(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    site = _site(owner_client, "main")
    token = _token(owner_client, "main")

    response = _report(client, token)

    assert response.status_code == 200, response.text
    assert response.json() == {"counted": True, "reason": None}
    day = pageviews.local_day()
    daily = _daily(db, day)
    assert (daily.uv, daily.pv) == (1, 1)
    assert daily.site_id == site["id"]
    assert db.scalar(select(PageviewPath.pv).where(PageviewPath.day == day)) == 1

    # 库里只有 HMAC，不是原始访客标识。
    stored = db.scalar(
        select(PageviewVisitor.visitor_hash).where(PageviewVisitor.day == day)
    )
    assert stored == pageviews.visitor_hash(SECRET, "v1")
    assert stored != "v1"


def test_same_event_retry_does_not_double(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    token = _site_token(owner_client)
    _report(client, token)

    retry = _report(client, token)

    assert retry.json() == {"counted": False, "reason": "duplicate"}
    daily = _daily(db, pageviews.local_day())
    assert (daily.uv, daily.pv) == (1, 1)


def test_same_visitor_new_page_adds_views_not_people(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    token = _site_token(owner_client)
    _report(client, token, event="e1", path="/")
    _report(client, token, event="e2", path="/about")

    daily = _daily(db, pageviews.local_day())
    assert (daily.uv, daily.pv) == (1, 2)


def test_new_visitor_adds_both(
    owner_client: TestClient, client: TestClient, db: DbSession
) -> None:
    token = _site_token(owner_client)
    _report(client, token, event="e1", visitor="v1")
    _report(client, token, event="e2", visitor="v2")

    daily = _daily(db, pageviews.local_day())
    assert (daily.uv, daily.pv) == (2, 2)


# —— 不计数的情况 ——


@pytest.mark.parametrize("path", ["/post?utm_source=x", "/post#top"])
def test_query_string_and_fragment_are_not_counted(
    owner_client: TestClient, client: TestClient, db: DbSession, path: str
) -> None:
    token = _site_token(owner_client)

    response = _report(client, token, path=path)

    assert response.json() == {"counted": False, "reason": "ignored_path"}
    assert _daily(db, pageviews.local_day()) is None


@pytest.mark.parametrize(
    "path", ["/api/public/v1/x", "/p/abc/original.png", "/admin/", "/healthz"]
)
def test_technical_paths_are_not_counted(
    owner_client: TestClient, client: TestClient, db: DbSession, path: str
) -> None:
    token = _site_token(owner_client)

    assert _report(client, token, path=path).json()["reason"] == "ignored_path"
    assert _daily(db, pageviews.local_day()) is None


def test_prefetch_header_is_skipped(owner_client: TestClient, client: TestClient) -> None:
    token = _site_token(owner_client)

    response = _report(client, token, headers={"Sec-Purpose": "prefetch;prerender"})

    assert response.json() == {"counted": False, "reason": "prefetch"}


def test_crawler_user_agent_is_skipped(
    owner_client: TestClient, client: TestClient
) -> None:
    token = _site_token(owner_client)

    response = _report(
        client, token, user_agent="Mozilla/5.0 (compatible; Googlebot/2.1)"
    )

    assert response.json() == {"counted": False, "reason": "bot"}


@pytest.mark.parametrize("path", ["../etc/passwd", "https://evil.example/a", "relative"])
def test_malformed_paths_are_rejected(
    owner_client: TestClient, client: TestClient, path: str
) -> None:
    token = _site_token(owner_client)

    response = _report(client, token, path=path)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid"


def test_response_is_not_cached(owner_client: TestClient, client: TestClient) -> None:
    token = _site_token(owner_client)

    response = _report(client, token)

    assert response.headers["Cache-Control"] == "no-store"


# —— 时区 ——


def test_shanghai_day_boundary() -> None:
    """UTC 15:59 与 16:00 落在相邻的两个上海日历日。"""
    assert pageviews.local_day(datetime(2026, 10, 21, 15, 59)) == "2026-10-21"
    assert pageviews.local_day(datetime(2026, 10, 21, 16, 0)) == "2026-10-22"


def test_record_uses_shanghai_day(db: DbSession) -> None:
    site = _new_site(db)

    pageviews.record(
        db,
        site_id=site.id,
        secret=SECRET,
        event_id="e",
        visitor_id="v",
        path="/",
        now=datetime(2026, 10, 21, 16, 0, tzinfo=UTC),
    )
    db.commit()

    assert _daily(db, "2026-10-22") is not None
    assert _daily(db, "2026-10-21") is None


# —— 隐私与清理 ——


def test_detail_is_purged_but_daily_stays(db: DbSession) -> None:
    site = _new_site(db)
    old_moment = datetime.now(UTC) - timedelta(days=500)
    pageviews.record(
        db,
        site_id=site.id,
        secret=SECRET,
        event_id="old",
        visitor_id="v",
        path="/old",
        now=old_moment,
    )
    db.commit()
    old_day = pageviews.local_day(old_moment)

    pageviews.purge_old(db)
    db.commit()

    assert _daily(db, old_day) is not None
    assert db.scalar(select(PageviewVisitor).where(PageviewVisitor.day == old_day)) is None
    assert db.scalar(select(PageviewPath).where(PageviewPath.day == old_day)) is None


def test_event_dedup_window_is_24_hours(db: DbSession) -> None:
    site = _new_site(db)
    pageviews.record(
        db, site_id=site.id, secret=SECRET, event_id="e", visitor_id="v", path="/"
    )
    db.commit()
    event = db.scalar(select(PageviewEvent))
    event.received_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=25)
    db.commit()

    pageviews.purge_old(db)
    db.commit()
    assert db.scalar(select(PageviewEvent)) is None

    again = pageviews.record(
        db, site_id=site.id, secret=SECRET, event_id="e", visitor_id="v", path="/"
    )
    db.commit()
    assert again.counted is True


# —— 路径上限 ——


def test_paths_overflow_into_other(db: DbSession) -> None:
    site = _new_site(db)
    day = pageviews.local_day()
    moment = datetime.now(UTC)

    for index in range(MAX_PAGEVIEW_PATHS + 5):
        pageviews.record(
            db,
            site_id=site.id,
            secret=SECRET,
            event_id=f"e{index}",
            visitor_id=f"v{index}",
            path=f"/post/{index}",
            now=moment,
        )
    db.commit()

    rows = db.execute(
        select(PageviewPath.path, PageviewPath.pv).where(PageviewPath.day == day)
    ).all()
    assert len(rows) <= MAX_PAGEVIEW_PATHS
    assert dict(rows)[PAGEVIEW_OTHER_PATH] >= 5
    assert sum(count for _, count in rows) == MAX_PAGEVIEW_PATHS + 5


# —— 限速 ——


def test_throttle_raises_after_limit() -> None:
    moment = [0.0]
    throttle = PageviewThrottle(max_per_window=2, clock=lambda: moment[0])

    throttle.check("site")
    throttle.check("site")
    with pytest.raises(ApiError) as error:
        throttle.check("site")
    assert error.value.status_code == 429

    # 窗口滑过去之后重新放行。
    moment[0] = 61.0
    throttle.check("site")


def test_report_endpoint_throttles(
    owner_client: TestClient, client: TestClient
) -> None:
    token = _site_token(owner_client)
    client.app.state.pageview_throttle = PageviewThrottle(max_per_window=1)

    assert _report(client, token, event="e1").status_code == 200
    second = _report(client, token, event="e2")
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


def test_daily_totals_use_sums(db: DbSession) -> None:
    """两个站点各自一行，读数是相加——第一期只有一个站点，但模型允许两个。"""
    first = _new_site(db, "a")
    second = _new_site(db, "b")
    day = pageviews.local_day()

    for site in (first, second):
        pageviews.record(
            db, site_id=site.id, secret=SECRET, event_id="e", visitor_id="v", path="/"
        )
    db.commit()

    total = db.scalar(select(func.sum(PageviewDaily.uv)).where(PageviewDaily.day == day))
    assert total == 2
