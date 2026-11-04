"""区块管理 API。认证、CSRF、幂等键沿用已有入口。"""

import json
from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DbSession

from .. import idempotency
from ..auth import deps
from ..errors import ApiError
from ..models import Section
from . import access, readers, service
from .schemas import CreateSection, SectionAction, SectionList, SectionOut, UpdateSection

router = APIRouter(tags=["sections"])


_READ_PERMISSIONS = ("section.update", "section.publish", "entry.update", "entry.publish")


def reader(principal: deps.Principal = Depends(deps.writer)) -> deps.Principal:
    """发布者要能核对草稿，但读取本身不授予修改。文案权限只能看文案区块。"""
    if not any(principal.can(name) for name in _READ_PERMISSIONS):
        raise ApiError.forbidden("缺少区块管理权限")
    return principal


def _require(principal: deps.Principal, template_id: str, verb: str) -> None:
    if not access.allows(template_id, principal.permissions, verb):
        raise ApiError.forbidden("缺少该区块的权限")


def present(db: DbSession, section: Section) -> SectionOut:
    return SectionOut(
        id=section.id,
        slug=section.slug,
        language=section.language,
        site_id=section.site_id,
        title=section.title,
        template=section.template,
        visibility=section.visibility,
        status=section.status,
        body=json.loads(section.body),
        reader_ids=readers.ids(db, section.id, published=False),
        published_slug=section.published_slug,
        published_language=section.published_language,
        published_site_id=section.published_site_id,
        published_title=section.published_title,
        published_template=section.published_template,
        published_visibility=section.published_visibility,
        published_body=json.loads(section.published_body) if section.published_body else None,
        published_reader_ids=readers.ids(db, section.id, published=True),
        revision=section.revision,
        published_at=section.published_at,
        created_at=section.created_at,
        updated_at=section.updated_at,
        assets=service.asset_briefs(db, section),
    )


@router.get("/section-templates")
def section_templates(
    response: Response,
    principal: deps.Principal = Depends(reader),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    items = service.templates()
    if not (principal.can("section.update") or principal.can("section.publish")):
        items = [item for item in items if item.get("access") == "copy"]
    return {"templates": items}


@router.get("/section-sites")
def section_sites(
    _: deps.Principal = Depends(reader),
    db: DbSession = Depends(deps.get_db),
) -> list[dict]:
    """只提供区块归属选择所需的站点标识。"""
    from ..auth.service import list_sites

    return [{"id": site.id, "key": site.key, "name": site.name} for site in list_sites(db)]


@router.get("/sections", response_model=SectionList)
def list_sections(
    response: Response,
    language: str | None = Query(default=None, max_length=32),
    prefix: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, pattern="^(draft|published)$"),
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=200),
    principal: deps.Principal = Depends(reader),
    db: DbSession = Depends(deps.get_db),
) -> SectionList:
    response.headers["Cache-Control"] = "no-store"
    template_ids = None
    if not (principal.can("section.update") or principal.can("section.publish")):
        template_ids = access.ids_for("copy")
    rows, next_cursor = service.list_sections(
        db,
        language=language,
        prefix=prefix,
        status=status,
        template_ids=template_ids,
        cursor=cursor,
        limit=limit,
    )
    return SectionList(
        items=[present(db, row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/sections/{section_id}", response_model=SectionOut)
def read_section(
    section_id: str,
    response: Response,
    principal: deps.Principal = Depends(reader),
    db: DbSession = Depends(deps.get_db),
) -> SectionOut:
    response.headers["Cache-Control"] = "no-store"
    section = service.get(db, section_id)
    if not access.can_open(section.template, principal.permissions):
        raise ApiError.forbidden("缺少该区块的权限")
    return present(db, section)


@router.post("/sections", response_model=SectionOut, status_code=201)
def create_section(
    payload: CreateSection,
    request: Request,
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> SectionOut:
    _require(principal, payload.template, "update")
    section = service.create(
        db,
        settings=request.app.state.settings,
        fields=payload.model_dump(),
        actor=principal.user,
    )
    return present(db, section)


@router.patch("/sections/{section_id}", response_model=SectionOut)
def update_section(
    section_id: str,
    payload: UpdateSection,
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> SectionOut:
    current = service.get(db, section_id)
    _require(principal, current.template, "update")
    section = service.update(
        db,
        section_id,
        revision=payload.revision,
        actor=principal.user,
        fields=payload.model_dump(exclude_unset=True, exclude={"revision"}),
    )
    return present(db, section)


def _action(
    request: Request,
    db: DbSession,
    section_id: str,
    payload: SectionAction,
    principal: deps.Principal,
    action: Callable[..., Section],
    *,
    verb: str,
) -> SectionOut:
    current = service.get(db, section_id)
    _require(principal, current.template, verb)
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
            return SectionOut(**stored.body)
    result = present(db, action(db, section_id, revision=payload.revision, actor=principal.user))
    if key:
        idempotency.remember(db, **scope, status=200, body=result.model_dump(mode="json"))
    return result


@router.post("/sections/{section_id}/publish", response_model=SectionOut)
def publish_section(
    section_id: str,
    payload: SectionAction,
    request: Request,
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> SectionOut:
    raw = payload.model_dump(exclude_unset=True)
    return _action(
        request,
        db,
        section_id,
        payload,
        principal,
        lambda database, ident, revision, actor: service.publish(
            database,
            ident,
            revision=revision,
            actor=actor,
            reader_ids=raw.get("reader_ids"),
            replace_readers="reader_ids" in raw,
        ),
        verb="publish",
    )


@router.post("/sections/{section_id}/unpublish", response_model=SectionOut)
def unpublish_section(
    section_id: str,
    payload: SectionAction,
    request: Request,
    principal: deps.Principal = Depends(deps.writer),
    db: DbSession = Depends(deps.get_db),
) -> SectionOut:
    return _action(
        request, db, section_id, payload, principal, service.unpublish, verb="publish"
    )


@router.delete("/sections/{section_id}", status_code=204)
def delete_section(
    section_id: str,
    revision: int | None = Query(default=None, ge=1),
    principal: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> Response:
    service.delete(db, section_id, actor=principal.user, revision=revision)
    return Response(status_code=204)
