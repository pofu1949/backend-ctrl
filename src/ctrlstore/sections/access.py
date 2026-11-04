"""区块写权限。

判断只读模板上的 ``access``。新增页面版式保持默认 ``section``，
这里不用跟着加模板名。文案权限只能碰 ``access == "copy"`` 的模板。
"""

from __future__ import annotations

from collections.abc import Iterable

from .template import TEMPLATES
from .validation import get_template


def allows(template_id: str, granted: Iterable[str], verb: str) -> bool:
    """``verb`` 是 ``update`` 或 ``publish``。"""
    held = set(granted)
    if f"section.{verb}" in held:
        return True
    template = get_template(template_id)
    return template.access == "copy" and f"entry.{verb}" in held


def can_open(template_id: str, granted: Iterable[str]) -> bool:
    """管理端能不能打开这条区块。修改或发布权限都算。"""
    return allows(template_id, granted, "update") or allows(template_id, granted, "publish")


def ids_for(access: str) -> list[str]:
    return [item.id for item in TEMPLATES.values() if item.access == access]
