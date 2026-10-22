"""主面板数据（规划第 8 部分验收项）。

覆盖：没有上报时的明确提示、今日人数/次数与昨日差、月份每日人数、
近 12 个月相加、七类库存与磁盘剩余、按日详情、编辑被拒绝。
"""

from __future__ import annotations

import calendar
import shutil
from collections import namedtuple
from datetime import UTC, date, datetime, timedelta

import psutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession
from tests.auth_helpers import EDITOR_PASSWORD, add_editor, admin_headers, login
from tests.test_public_api import _site
from tests.test_publish import _image, _publish, _text

from ctrlstore.stats import pageviews

API = "/api/admin/v1"
SECRET = "pageview-secret-for-tests-only-001"

_DiskUsage = namedtuple("_DiskUsage", "total used free")
_Memory = namedtuple("_Memory", "available")


@pytest.fixture(autouse=True)
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: _DiskUsage(1000 * 1024**3, 500 * 1024**3, 500 * 1024**3)
    )
    monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(4 * 1024**3))


def _record(
    db: DbSession,
    site_id: str,
    *,
    event: str,
    visitor: str,
    path: str = "/",
    now: datetime | None = None,
) -> None:
    pageviews.record(
        db,
        site_id=site_id,
        secret=SECRET,
        event_id=event,
        visitor_id=visitor,
        path=path,
        now=now,
    )
    db.commit()


def _dashboard(client: TestClient, **params) -> dict:
    response = client.get(f"{API}/dashboard", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# —— 没有上报 ——


def test_no_reports_says_so_instead_of_zero(owner_client: TestClient) -> None:
    body = _dashboard(owner_client)

    assert body["reported"] is False
    assert body["message"] == "尚未收到网站上报"
    # 历史与今日都给空，不把「没上报」填成 0。
    assert body["today"] is None
    assert body["month"]["days"] == []
    assert body["months"] == []


def test_inventory_is_available_before_any_report(owner_client: TestClient) -> None:
    image = _image(owner_client)
    assert image["asset_id"]

    body = _dashboard(owner_client)

    assert body["reported"] is False
    inventory = body["inventory"]
    assert [item["kind"] for item in inventory["kinds"]] == [
        "image",
        "video",
        "markdown",
        "pdf",
        "docx",
        "xlsx",
        "text",
    ]
    assert inventory["kinds"][0]["count"] == 1
    assert inventory["total_count"] == 1
    assert inventory["disk_free_bytes"] == 500 * 1024**3


# —— 今日与昨日 ——


def test_today_numbers_and_deltas(
    owner_client: TestClient, db: DbSession, client: TestClient
) -> None:
    site = _site(owner_client, "main")
    today_moment = datetime.now(UTC)
    yesterday_moment = today_moment - timedelta(days=1)

    # 今天：2 人 3 次；昨天：1 人 1 次。
    _record(db, site["id"], event="t1", visitor="a", now=today_moment)
    _record(db, site["id"], event="t2", visitor="a", path="/about", now=today_moment)
    _record(db, site["id"], event="t3", visitor="b", now=today_moment)
    _record(db, site["id"], event="y1", visitor="c", now=yesterday_moment)

    body = _dashboard(client)

    assert body["reported"] is True
    assert body["message"] is None
    assert body["today"]["day"] == pageviews.local_day(today_moment)
    assert body["today"]["uv"] == 2
    assert body["today"]["pv"] == 3
    assert body["today"]["uv_delta"] == 1
    assert body["today"]["pv_delta"] == 2
    assert body["month"]["month"] == pageviews.local_day(today_moment)[:7]
    assert len(body["month"]["days"]) == calendar.monthrange(
        date.today().year, date.today().month
    )[1]
    assert len(body["months"]) == 12
    assert body["months"][-1]["month"] == body["month"]["month"]
    # 近 12 个月必须按时间正序，且首项早于当前月；不能把未来月份当成历史。
    assert [row["month"] for row in body["months"]] == sorted(
        row["month"] for row in body["months"]
    )
    assert body["months"][0]["month"] < body["months"][-1]["month"]


def test_requested_month_and_invalid_month(
    owner_client: TestClient, db: DbSession, client: TestClient
) -> None:
    site = _site(owner_client, "main")
    _record(db, site["id"], event="e", visitor="a", now=datetime.now(UTC))
    today = date.fromisoformat(pageviews.local_day())
    previous = today.replace(day=1) - timedelta(days=1)

    # 上个月 15 号有一条记录。
    _record(
        db,
        site["id"],
        event="old",
        visitor="old",
        now=datetime(previous.year, previous.month, 15, 4, tzinfo=UTC),
    )

    body = _dashboard(client, month=f"{previous.year}-{previous.month:02d}")

    assert body["month"]["month"] == f"{previous.year}-{previous.month:02d}"
    days = {item["day"]: item for item in body["month"]["days"]}
    assert days[f"{previous.year}-{previous.month:02d}-15"]["uv"] == 1
    assert days[f"{previous.year}-{previous.month:02d}-16"]["uv"] == 0

    invalid = client.get(f"{API}/dashboard", params={"month": "2026-13"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid"


# —— 库存 ——


def test_inventory_counts_bytes_and_trash(
    owner_client: TestClient, client: TestClient
) -> None:
    image = _image(owner_client, "hero.png")
    image = _publish(owner_client, image["asset_id"])
    article = _text(owner_client)
    article = _publish(owner_client, article["id"])
    trashed = _image(owner_client, "draft.png")
    detail = owner_client.get(f"{API}/assets/{trashed['asset_id']}").json()
    owner_client.post(
        f"{API}/assets/{trashed['asset_id']}/trash",
        json={"revision": detail["revision"]},
        headers=admin_headers(owner_client),
    )

    inventory = _dashboard(client)["inventory"]
    kinds = {item["kind"]: item for item in inventory["kinds"]}

    assert kinds["image"]["count"] == 1
    assert kinds["image"]["published"] == 1
    assert kinds["image"]["current_bytes"] == image["current_version"]["byte_size"]
    assert kinds["markdown"]["count"] == 1
    assert kinds["markdown"]["published"] == 1
    assert kinds["markdown"]["current_bytes"] == article["current_version"]["byte_size"]
    # 回收站不计入条数，但要单独计数。
    assert inventory["trash_count"] == 1
    assert inventory["total_count"] == 2
    assert inventory["total_bytes"] == (
        kinds["image"]["total_bytes"] + kinds["markdown"]["total_bytes"]
    )
    assert kinds["video"]["count"] == 0


# —— 日详情与权限 ——


def test_day_detail_lists_top_paths(
    owner_client: TestClient, db: DbSession, client: TestClient
) -> None:
    site = _site(owner_client, "main")
    now = datetime.now(UTC)
    # 12 条路径，第 i 条被打开 i+1 次。
    for index in range(12):
        for count in range(index + 1):
            _record(
                db,
                site["id"],
                event=f"e{index}-{count}",
                visitor=f"v{index}-{count}",
                path=f"/post/{index}",
                now=now,
            )

    response = client.get(
        f"{API}/dashboard/day", params={"date": pageviews.local_day(now)}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["day"] == pageviews.local_day(now)
    assert body["uv"] == sum(range(1, 13))
    assert body["pv"] == sum(range(1, 13))
    assert len(body["paths"]) == 10
    counts = [item["pv"] for item in body["paths"]]
    assert counts == sorted(counts, reverse=True)
    assert body["paths"][0] == {"path": "/post/11", "pv": 12}

    invalid = client.get(f"{API}/dashboard/day", params={"date": "2026-13-01"})
    assert invalid.status_code == 422


def test_editor_cannot_read_dashboard(
    owner_client: TestClient, db: DbSession, client: TestClient
) -> None:
    add_editor(db, username="editor", permissions=("asset.read",))
    login(client, "editor", EDITOR_PASSWORD)

    response = client.get(f"{API}/dashboard")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
