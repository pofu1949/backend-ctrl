"""页面访问上报 ``POST /api/public/v1/pageviews``（企划第 8.9 节）。

只接受站点令牌：访客浏览器不持有令牌，事件由网站服务器转发。响应里的
``counted`` 与 ``reason`` 只为排错方便；网站仍然按「统计失败不影响页面」处理。

四种请求不计：预取/预渲染、爬虫与社交预览、接口与公开文件路径、带查询串的路径。
重复的 ``event_id`` 同样返回 ``counted=false``，只是原因不同。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as DbSession

from ..auth import deps
from ..errors import ApiError
from ..stats import pageviews
from . import deps as public_deps

router = APIRouter(tags=["public"])


class PageviewRequest(BaseModel):
    """一次页面打开。字段名与企划第 8.9 节一致。

    ``extra="ignore"``：网站多送字段（例如路由名）不该导致整条上报失败。
    """

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=1, max_length=128, description="事件 ID，24 小时内去重")
    visitor_id: str = Field(min_length=1, max_length=128, description="访客标识，只存 HMAC")
    path: str = Field(min_length=1, max_length=512, description="以 / 开头、不含查询串")
    user_agent: str | None = Field(
        default=None, max_length=512, description="访客 User-Agent，可选；缺省用请求头"
    )


@router.post("/pageviews")
def record_pageview(
    payload: PageviewRequest,
    request: Request,
    site=Depends(public_deps.site_from_request),
    db: DbSession = Depends(deps.get_db),
) -> JSONResponse:
    """记录一次页面打开。同一事件重试不会增加次数。"""
    if site is None:
        raise ApiError.unauthorized("页面访问上报只接受站点令牌")

    settings = request.app.state.settings
    # 限速按站点计：多发一把令牌不该换来更多额度。
    request.app.state.pageview_throttle.check(site.id)

    path = pageviews.validate_path(payload.path)
    user_agent = payload.user_agent or request.headers.get("User-Agent")
    reason = pageviews.ignored_reason(
        path=path, user_agent=user_agent, headers=request.headers
    )
    if reason is None:
        recorded = pageviews.record(
            db,
            site_id=site.id,
            secret=settings.pageview_hmac_secret,
            event_id=payload.event_id,
            visitor_id=payload.visitor_id,
            path=path,
        )
    else:
        recorded = pageviews.Recorded(counted=False, reason=reason)

    return JSONResponse(
        status_code=200,
        content={"counted": recorded.counted, "reason": recorded.reason},
        headers={"Cache-Control": "no-store"},
    )
