"""登录、退出与当前身份。

登录本身不需要 CSRF（此时还没有会话可绑定），但要过限速。
其余写操作走 :func:`writer` 依赖，由它统一校验 CSRF。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session as DbSession

from .. import audit, errors
from ..auth import deps, passwords, service, sessions
from .schemas import LoginRequest, MeOut

router = APIRouter(tags=["session"])


@router.post("/session", response_model=MeOut)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession = Depends(deps.get_db),
) -> MeOut:
    """用户名密码登录，成功后下发会话 Cookie。"""
    settings = request.app.state.settings
    throttle = request.app.state.login_throttle
    ip = deps.client_ip(request)
    throttle.check(payload.username, ip)

    user = service.find_user(db, payload.username)
    if user is None or not passwords.verify_password(user.password_hash, payload.password):
        throttle.record_failure(payload.username, ip)
        audit.record(
            db,
            action=audit.LOGIN_FAILED,
            actor_type=audit.ACTOR_ANONYMOUS,
            target_type=audit.TARGET_USER,
            target_id=user.id if user else None,
            detail={"username": payload.username.strip(), "ip": ip},
        )
        # 审计要留下，但凭据错误本身是失败：先提交，再交给依赖回滚空事务。
        db.commit()
        raise errors.ApiError.unauthorized("用户名或密码不正确")

    if not user.is_active:
        raise errors.ApiError.forbidden("账号已停用")

    throttle.reset(payload.username, ip)
    issued = sessions.start(db, user, absolute_hours=settings.session_absolute_hours)
    _set_cookie(response, settings, issued.raw_token)
    audit.record_for_user(db, user, action=audit.LOGIN, detail={"ip": ip})
    return MeOut.of(deps.Principal(kind="session", user=user), issued.csrf_token)


@router.delete("/session", status_code=204, response_model=None)
def logout(
    response: Response,
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> None:
    """退出登录。撤销当前会话并清掉 Cookie。"""
    if principal.session_record is not None:
        sessions.revoke(db, principal.session_record)
        audit.record_for_user(db, principal.user, action=audit.LOGOUT)
    response.delete_cookie(sessions.COOKIE_NAME, path="/")


@router.get("/me", response_model=MeOut)
def me(
    principal: deps.Principal = Depends(deps.current_principal),
) -> MeOut:
    """当前身份与 CSRF 令牌，供前端在一开始取一次。"""
    return MeOut.of(principal, principal.csrf_token)


def _set_cookie(response: Response, settings, raw_token: str) -> None:
    response.set_cookie(
        key=sessions.COOKIE_NAME,
        value=raw_token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
