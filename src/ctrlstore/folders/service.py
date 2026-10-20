"""目录树。

目录是给人浏览的树，不等于磁盘路径（企划原则 1）。内部一律用 ``id``，
``path`` 只是物化出来的展示路径，例如 ``/资料/2026``，根目录是 ``/``。

移动或改名时，同一条事务里把子孙的 ``path`` 前缀一起换掉：这样祖先判断、
列表排序都不需要递归查询，代价是每次移动要写若干行——目录数量本来就少。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from .. import audit
from ..errors import ApiError
from ..models import ROOT_PATH, Folder, User, utcnow

#: 目录名长度上限，与列宽一致。
MAX_NAME_LENGTH = 128

#: 物化路径的整体长度上限。
MAX_PATH_LENGTH = 1024


def list_folders(db: DbSession) -> list[Folder]:
    """按物化路径排序，父子相邻，便于前端直接渲染成树。"""
    return list(db.scalars(select(Folder).order_by(Folder.path)).all())


def get_folder(db: DbSession, folder_id: str) -> Folder:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise ApiError.not_found("目录不存在")
    return folder


def create_folder(
    db: DbSession, *, name: str, parent_id: str | None, actor: User | None = None
) -> Folder:
    clean = _validate_name(name)
    parent = get_folder(db, parent_id) if parent_id else None
    path = _child_path(parent, clean)

    folder = Folder(
        parent_id=parent.id if parent else None,
        name=clean,
        path=path,
        created_at=utcnow(),
    )
    db.add(folder)
    _flush_or_conflict(db, clean)
    audit.record(
        db,
        action=audit.FOLDER_CREATED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_FOLDER,
        target_id=folder.id,
        detail={"name": clean, "path": path},
    )
    return folder


def rename_folder(db: DbSession, folder: Folder, *, name: str, actor: User | None = None) -> Folder:
    clean = _validate_name(name)
    if clean == folder.name:
        return folder
    old_path = folder.path
    folder.name = clean
    folder.path = _child_path(folder.parent, clean)
    _rename_descendants(db, old_path, folder.path)
    _flush_or_conflict(db, clean)
    audit.record(
        db,
        action=audit.FOLDER_RENAMED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_FOLDER,
        target_id=folder.id,
        detail={"before": old_path, "after": folder.path},
    )
    return folder


def move_folder(
    db: DbSession, folder: Folder, *, parent_id: str | None, actor: User | None = None
) -> Folder:
    """移动目录。不能移到自己或自己的子孙下面，否则树会断开。"""
    parent = get_folder(db, parent_id) if parent_id else None
    if parent is not None:
        _reject_cycle(folder, parent)
    target_parent_id = parent.id if parent else None
    if target_parent_id == folder.parent_id:
        return folder

    old_path = folder.path
    folder.parent_id = target_parent_id
    folder.path = _child_path(parent, folder.name)
    if folder.path == old_path:
        return folder
    _rename_descendants(db, old_path, folder.path)
    _flush_or_conflict(db, folder.name)
    audit.record(
        db,
        action=audit.FOLDER_MOVED,
        actor_type=audit.ACTOR_USER if actor else audit.ACTOR_SYSTEM,
        actor_id=actor.id if actor else "",
        target_type=audit.TARGET_FOLDER,
        target_id=folder.id,
        detail={"before": old_path, "after": folder.path},
    )
    return folder


# —— 内部 ——


def _validate_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        raise ApiError.invalid("目录名不能为空")
    if len(clean) > MAX_NAME_LENGTH:
        raise ApiError.invalid(f"目录名不能超过 {MAX_NAME_LENGTH} 个字符")
    if "/" in clean or "\\" in clean:
        raise ApiError.invalid("目录名不能含斜杠")
    if clean in {".", ".."}:
        raise ApiError.invalid("目录名不能是 . 或 ..")
    return clean


def _child_path(parent: Folder | None, name: str) -> str:
    # 根下直接 `/名字`；其余接在父路径后面（父路径不以 / 结尾）。
    path = f"/{name}" if parent is None else f"{parent.path.rstrip('/')}/{name}"
    if len(path) > MAX_PATH_LENGTH:
        raise ApiError.invalid(f"目录层级过深：路径超过 {MAX_PATH_LENGTH} 个字符")
    return path


def _reject_cycle(folder: Folder, parent: Folder) -> None:
    """目标父目录不能是自己，也不能在自己的子孙里。"""
    if parent.id == folder.id:
        raise ApiError.invalid("不能把目录移动到它自己下面")
    if parent.path == folder.path or parent.path.startswith(f"{folder.path}/"):
        raise ApiError.invalid("不能把目录移动到自己的子目录下面")


def _rename_descendants(db: DbSession, old_path: str, new_path: str) -> None:
    """把子孙的路径前缀整体换掉。同一事务内完成，中途失败一起回滚。"""
    if old_path in {ROOT_PATH, ""}:
        return
    prefix = f"{old_path}/"
    descendants: Iterable[Folder] = db.scalars(
        select(Folder).where(Folder.path.startswith(prefix))
    ).all()
    for child in descendants:
        child.path = f"{new_path}/{child.path[len(prefix) :]}"


def _flush_or_conflict(db: DbSession, name: str) -> None:
    """同名冲突交给库层的唯一索引判定，这里只翻译成 409。"""
    try:
        db.flush()
    except IntegrityError as exc:
        raise ApiError.conflict(f"同一目录下已有同名目录：{name}") from exc
