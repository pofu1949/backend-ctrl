"""内容与文案的可授予能力。

所有者不读权限表，代码里直接视为拥有全部；编辑只拥有表里出现的键。
能力清单集中在这里，新增能力只需要改这一处。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 可授予编辑的能力，顺序即界面上的展示顺序。
CONTENT_PERMISSIONS: tuple[str, ...] = (
    "asset.read",
    "asset.upload",
    "asset.update",
    "asset.publish",
    "asset.trash",
    "folder.manage",
    "collection.manage",
    "tag.manage",
)

#: 命令行预设：一次授予全部八项。
ENTRY_PERMISSIONS = ("entry.update", "entry.publish")
#: 区块与文案一样，修改和发布分开，避免没审核的草稿进网站。
SECTION_PERMISSIONS = ("section.update", "section.publish")
PERMISSIONS = CONTENT_PERMISSIONS + ENTRY_PERMISSIONS + SECTION_PERMISSIONS
PRESET_EDITOR = frozenset(CONTENT_PERMISSIONS)
PRESET_COPYWRITER = frozenset(ENTRY_PERMISSIONS)

#: 预设名到能力集合的映射。
PRESETS: dict[str, frozenset[str]] = {
    "editor": PRESET_EDITOR,
    "copywriter": PRESET_COPYWRITER,
}


class UnknownPermissionError(ValueError):
    """出现了清单之外的能力名。"""


def normalize(names: Iterable[str]) -> set[str]:
    """校验并去重。未知能力名直接拒绝，避免写进库里却没人认。"""
    selected = set(names)
    unknown = sorted(selected - set(PERMISSIONS))
    if unknown:
        raise UnknownPermissionError(f"未知权限：{', '.join(unknown)}")
    return selected


def from_preset(name: str) -> set[str]:
    """按预设名取能力集合。"""
    if name not in PRESETS:
        raise UnknownPermissionError(f"未知预设：{name}")
    return set(PRESETS[name])


def effective(role: str, granted: Iterable[str]) -> frozenset[str]:
    """某个身份实际拥有的能力。所有者总是全部。"""
    if role == "owner":
        return frozenset(PERMISSIONS)
    return frozenset(name for name in granted if name in PERMISSIONS)


def allows(granted: Iterable[str], permission: str) -> bool:
    """单项判断。``granted`` 应是已算好的有效集合。"""
    return permission in granted
