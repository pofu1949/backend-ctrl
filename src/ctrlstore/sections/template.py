"""区块模板。

网站版式已经定了，这里只声明它要填的槽位。编辑不能在运行时再加一层表，
否则前端没有对应的位置可渲染。新增版式时在这里加一份模板，并和网站一起改。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    """模板里的一个槽位。``repeat`` 的子字段只再嵌一层。"""

    key: str
    type: str
    label: str
    max_length: int = 0
    max_items: int = 0
    required: bool = False
    fields: tuple[FieldSpec, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "type": self.type,
            "label": self.label,
            "required": self.required,
        }
        if self.max_length:
            payload["max_length"] = self.max_length
        if self.max_items:
            payload["max_items"] = self.max_items
        if self.type == "assets":
            payload["kinds"] = ["image", "video"]
        if self.fields:
            payload["fields"] = [field.public_dict() for field in self.fields]
        return payload


@dataclass(frozen=True)
class Template:
    id: str
    label: str
    description: str
    fields: tuple[FieldSpec, ...]
    example: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "fields": [field.public_dict() for field in self.fields],
            "example": self.example,
        }


_POINT_FIELDS = (
    FieldSpec("number", "text", "编号", max_length=8),
    FieldSpec("title", "text", "标题", max_length=80, required=True),
    FieldSpec("body", "longtext", "正文", max_length=2000),
    FieldSpec("media", "assets", "配图", max_items=6),
    FieldSpec("action_label", "text", "按钮文字", max_length=40),
    FieldSpec("action_href", "link", "按钮链接", max_length=200),
)

#: 对应「主标题 + 导语 + 配图 + 若干下级卡片」。卡片内部不再套卡片。
STACK = Template(
    id="stack",
    label="分层说明",
    description="一个主标题、一段导语、一组配图，以及可排序的下级卡片。",
    fields=(
        FieldSpec("eyebrow", "text", "眉题", max_length=80),
        FieldSpec("title", "text", "主标题", max_length=120, required=True),
        FieldSpec("lead", "longtext", "导语", max_length=2000),
        FieldSpec("gallery", "assets", "配图", max_items=8),
        FieldSpec("points", "repeat", "下级卡片", max_items=12, fields=_POINT_FIELDS),
    ),
    example={
        "eyebrow": "A CLEARER WAY TO PLAN",
        "title": "全屋定制费用如何确定？",
        "lead": (
            "全屋定制费用由尺寸、材料、柜内结构、五金、造型工艺和交付范围共同决定。"
            "云栖木作先确认空间需求与配置，再核算报价。"
            "比较方案时，应先对齐配置和服务范围，再比较总价。"
        ),
        "gallery": [],
        "points": [
            {
                "number": "01",
                "title": "计价方式",
                "body": (
                    "沟通时明确采用投影面积、展开面积还是分项计价。"
                    "投影面积通常按柜深与柜高计算；展开面积按实际板件面积核算。"
                    "具体方式和计价单位以正式报价单为准。"
                ),
                "media": [],
                "action_label": "",
                "action_href": "",
            },
            {
                "number": "02",
                "title": "包含与排除",
                "body": (
                    "柜体、门板、背板、五金、灯带、运输和安装属于明确包含项。"
                    "非标造型、材料升级、尺寸或颜色变更，以及拆除、水电等现场配套事项，"
                    "也要确认费用与承接方。"
                ),
                "media": [],
                "action_label": "带户型图咨询预算",
                "action_href": "/consult",
            },
            {
                "number": "03",
                "title": "最终核价",
                "body": (
                    "准备户型图、定制空间、收纳需求、电器尺寸和预算范围。"
                    "最终费用以实际测量、确认方案、报价明细及合同为准。"
                    "变更应记录原因、数量、价格和工期影响。"
                ),
                "media": [],
                "action_label": "",
                "action_href": "",
            },
        ],
    },
)

TEMPLATES: dict[str, Template] = {STACK.id: STACK}
