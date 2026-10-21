"""站点与访问令牌。

令牌明文只在创建响应里出现一次（企划第 8.1 节 A3）；之后再查只能看到元数据。
站点令牌只用于读取已发布内容与上报页面访问，签发接口不授予写权限。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import errors
from ..auth import deps, service
from ..models import ApiToken, Site
from .schemas import (
    CreateSiteRequest,
    CreateTokenRequest,
    IssuedTokenOut,
    SiteOut,
    TokenOut,
)

router = APIRouter(tags=["sites"])


@router.get("/sites", response_model=list[SiteOut])
def list_sites(
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> list[SiteOut]:
    sites = db.scalars(select(Site).order_by(Site.key)).all()
    return [SiteOut.model_validate(site) for site in sites]


@router.post("/sites", response_model=SiteOut, status_code=201)
def create_site(
    payload: CreateSiteRequest,
    actor: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> SiteOut:
    site = service.create_site(db, key=payload.key, name=payload.name, actor=actor.user)
    return SiteOut.model_validate(site)


@router.get("/tokens", response_model=list[TokenOut])
def list_tokens(
    _: deps.Principal = Depends(deps.owner),
    db: DbSession = Depends(deps.get_db),
) -> list[TokenOut]:
    tokens = db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all()
    return [TokenOut.of(token) for token in tokens]


@router.post("/tokens", response_model=IssuedTokenOut, status_code=201)
def create_token(
    payload: CreateTokenRequest,
    actor: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> IssuedTokenOut:
    """签发令牌。响应里的 ``token`` 是唯一一次能看到明文的地方。"""
    if payload.subject_type == "site":
        site = db.scalar(select(Site).where(Site.key == payload.subject_key))
        if site is None:
            raise errors.ApiError.not_found(f"站点不存在：{payload.subject_key}")
        plaintext, token = service.issue_site_token(
            db,
            site=site,
            name=payload.name,
            expires_at=payload.expires_at,
            actor=actor.user,
            allowed_ip=payload.allowed_ip,
        )
    else:
        user = service.find_user(db, payload.subject_key)
        if user is None:
            raise errors.ApiError.not_found(f"账号不存在：{payload.subject_key}")
        plaintext, token = service.issue_user_token(
            db,
            user=user,
            name=payload.name,
            expires_at=payload.expires_at,
            actor=actor.user,
            allowed_ip=payload.allowed_ip,
        )
    return IssuedTokenOut(**TokenOut.of(token).model_dump(), token=plaintext)


@router.delete("/tokens/{token_id}", status_code=204, response_model=None)
def revoke_token(
    token_id: str,
    actor: deps.Principal = Depends(deps.owner_writer),
    db: DbSession = Depends(deps.get_db),
) -> None:
    token = db.get(ApiToken, token_id)
    if token is None:
        raise errors.ApiError.not_found("令牌不存在")
    service.revoke_token(db, token, actor=actor.user)
