"""文案管理 API。认证、CSRF、幂等键沿用已有入口。"""

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DbSession

from .. import idempotency
from ..auth import deps
from ..errors import ApiError
from ..models import Entry
from . import service
from .schemas import CreateEntry, EntryAction, EntryList, EntryOut, UpdateEntry

router = APIRouter(tags=["entries"])


def reader(principal: deps.Principal = Depends(deps.writer)) -> deps.Principal:
    # 发布者必须能审核草稿，但读取权限不会授予修改能力。
    if not (principal.can("entry.update") or principal.can("entry.publish")):
        raise ApiError.forbidden("缺少文案管理权限")
    return principal


@router.get("/entries", response_model=EntryList)
def list_entries(
    response: Response,
    language: str | None = Query(default=None, max_length=32),
    prefix: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, pattern="^(draft|published)$"),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=200),
    _: deps.Principal = Depends(reader),
    db: DbSession = Depends(deps.get_db),
) -> EntryList:
    response.headers["Cache-Control"] = "no-store"
    rows, next_cursor = service.list_entries(
        db, language=language, prefix=prefix, status=status, cursor=cursor, limit=limit
    )
    return EntryList(items=[EntryOut.model_validate(row) for row in rows], next_cursor=next_cursor)


@router.get("/entries/{entry_id}", response_model=EntryOut)
def read_entry(
    entry_id: str,
    response: Response,
    _: deps.Principal = Depends(reader),
    db: DbSession = Depends(deps.get_db),
) -> EntryOut:
    response.headers["Cache-Control"] = "no-store"
    return EntryOut.model_validate(service.get(db, entry_id))


@router.post("/entries", response_model=EntryOut, status_code=201)
def create_entry(
    payload: CreateEntry,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("entry.update")),
    db: DbSession = Depends(deps.get_db),
) -> EntryOut:
    return EntryOut.model_validate(
        service.create(
            db,
            settings=request.app.state.settings,
            fields=payload.model_dump(),
            actor=principal.user,
        )
    )


@router.patch("/entries/{entry_id}", response_model=EntryOut)
def update_entry(
    entry_id: str,
    payload: UpdateEntry,
    principal: deps.Principal = Depends(deps.require_permission("entry.update")),
    db: DbSession = Depends(deps.get_db),
) -> EntryOut:
    return EntryOut.model_validate(
        service.update(
            db,
            entry_id,
            revision=payload.revision,
            actor=principal.user,
            fields=payload.model_dump(exclude_unset=True, exclude={"revision"}),
        )
    )


def _action(
    request: Request,
    db: DbSession,
    entry_id: str,
    payload: EntryAction,
    principal: deps.Principal,
    action: Callable[..., Entry],
) -> EntryOut:
    # 锁覆盖 lookup、状态变更与 remember；并发重试不会在提交前重复执行。
    service.begin_write(db)
    key = request.headers.get(idempotency.HEADER)
    scope = dict(
        actor_id=principal.user.id,
        method="POST",
        path=request.url.path,
        key=key,
        request_hash=idempotency.hash_request(payload.model_dump_json()),
    )
    if key:
        stored = idempotency.lookup(db, **scope)
        if stored is not None:
            return EntryOut(**stored.body)
    result = EntryOut.model_validate(
        action(db, entry_id, revision=payload.revision, actor=principal.user)
    )
    if key:
        idempotency.remember(db, **scope, status=200, body=result.model_dump(mode="json"))
    return result


@router.post("/entries/{entry_id}/publish", response_model=EntryOut)
def publish_entry(
    entry_id: str,
    payload: EntryAction,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("entry.publish")),
    db: DbSession = Depends(deps.get_db),
) -> EntryOut:
    return _action(request, db, entry_id, payload, principal, service.publish)


@router.post("/entries/{entry_id}/unpublish", response_model=EntryOut)
def unpublish_entry(
    entry_id: str,
    payload: EntryAction,
    request: Request,
    principal: deps.Principal = Depends(deps.require_permission("entry.publish")),
    db: DbSession = Depends(deps.get_db),
) -> EntryOut:
    return _action(request, db, entry_id, payload, principal, service.unpublish)


@router.delete("/entries/{entry_id}", status_code=204)
def delete_entry(
    entry_id: str,
    revision: int | None = Query(default=None, ge=1),
    principal: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> Response:
    service.delete(db, entry_id, actor=principal.user, revision=revision)
    return Response(status_code=204)


@router.get("/entry-sites")
def entry_sites(
    _: deps.Principal = Depends(reader),
    db: DbSession = Depends(deps.get_db),
) -> list[dict]:
    """只提供文案归属选择所需的站点标识，不授予账号/令牌管理能力。"""
    from ..auth.service import list_sites

    return [{"id": site.id, "key": site.key, "name": site.name} for site in list_sites(db)]
