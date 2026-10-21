"""派生文件的登记与状态（企划第 10.2、8.5 节）。

一张图片入库后立刻有一条 ``pending`` 的缩略图记录与一个后台任务；生成成功改
``ready``，失败改 ``failed``。公开区只挂 ``ready`` 的那一份，所以「派生没做完」
与「原图已发布」是两件互不牵连的事。

记录在登记任务的同时建立，而不是等生成完再建：这样管理端和测试都能看到
``pending``，也能看出失败原因（``jobs.last_error``）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..jobs import service as jobs
from ..models import (
    DERIVATIVE_PENDING,
    DERIVATIVE_READY,
    PURPOSE_THUMB,
    Asset,
    AssetVersion,
    Derivative,
    utcnow,
)
from ..storage import LocalStorage, keys

#: 派生文件的 MIME。缩略图与展示图都是 WebP，视频封面是 JPEG。
MIME_BY_PURPOSE = {
    "thumb": "image/webp",
    "display": "image/webp",
    "poster": "image/jpeg",
}


def for_version(db: DbSession, version_id: str, purpose: str) -> Derivative | None:
    """某个版本的某项派生文件。"""
    return db.scalar(
        select(Derivative).where(
            Derivative.version_id == version_id, Derivative.purpose == purpose
        )
    )


def ready(version: AssetVersion) -> list[Derivative]:
    """该版本已经生成好的派生文件。公开区与响应都只认这一份。"""
    return [item for item in version.derivatives if item.status == DERIVATIVE_READY]


def schedule_thumbnail(db: DbSession, *, asset: Asset, version: AssetVersion) -> Derivative:
    """登记缩略图并入队。只有图片版本会调用这里。

    同版本重复调用不会排第二个任务；已经 ``ready`` 的直接返回，不重做。
    """
    derivative = ensure(
        db,
        asset=asset,
        version=version,
        purpose=PURPOSE_THUMB,
        mime=MIME_BY_PURPOSE[PURPOSE_THUMB],
    )
    if derivative.status != DERIVATIVE_READY:
        derivative.status = DERIVATIVE_PENDING
        derivative.updated_at = utcnow()
        jobs.enqueue(db, jobs.KIND_THUMB, {"derivative_id": derivative.id})
    return derivative


def ensure(
    db: DbSession, *, asset: Asset, version: AssetVersion, purpose: str, mime: str
) -> Derivative:
    """取或建一条派生记录。键由服务端按版本生成，不接受外部传入。"""
    existing = for_version(db, version.id, purpose)
    if existing is not None:
        return existing

    derivative = Derivative(
        version_id=version.id,
        purpose=purpose,
        mime=mime,
        storage_key=keys.build_derivative(asset.id, version.version_no, purpose),
        status=DERIVATIVE_PENDING,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    # 追加到关系而不是只 db.add：同一事务里紧接着同步公开区时，
    # 判断「哪些派生文件已就绪」要看得见这一行。
    version.derivatives.append(derivative)
    db.flush()
    return derivative


def discard_for_version(
    db: DbSession, *, storage: LocalStorage, version: AssetVersion
) -> None:
    """删掉一个版本的全部派生文件与记录。

    版本被保留规则淘汰、或内容被彻底删除时调用。先删文件再删行，与原件一致。
    """
    storage.discard_derivatives(version.asset_id, version.version_no)
    # 从关系里移除而不是直接 db.delete：随后版本行被删除时，级联不会再删一次。
    for derivative in list(version.derivatives):
        version.derivatives.remove(derivative)
    db.flush()
