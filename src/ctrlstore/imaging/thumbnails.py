"""图片派生：缩略图与展示图（企划第 8.5、11.3 节）。

只做「把原图缩小成 WebP」这一件事，不认识数据库、也不决定公不公开。
这样解码失败、内存超限都能在这里单独测，队列与公开区各管一段。

两条实现约定：

* **先写临时文件再改名**。生成中途失败不会在派生目录里留下半个文件。
* **能缩小读取就缩小读取**。JPEG 用 ``draft`` 让解码器直接按目标尺寸解，
  避免一张大图在 worker 里解出完整像素；其他格式只能整图解码，上限由
  systemd 的 ``MemoryMax`` 兜底。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

#: WebP 质量。480 像素的列表图用 82 已经看不出差别，文件却小很多。
WEBP_QUALITY = 82

#: 保存时压缩尝试次数。4 是 Pillow 的默认档，这里写明以免以后误改。
WEBP_METHOD = 4


def render_webp(source: Path, destination: Path, *, max_side: int) -> tuple[int, int]:
    """把原图缩到最长边 ``max_side`` 并写成 WebP，返回输出尺寸。

    图片本身的宽高都不超过上限时保持原尺寸（不放大）：列表要的是大小，不是分辨率。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f"{destination.name}.tmp")
    staging.unlink(missing_ok=True)
    try:
        with Image.open(source) as image:
            # GIF 缩略图只取第一帧：动图预览不做。
            image.seek(0)
            _shrink_on_decode(image, max_side)
            image = ImageOps.exif_transpose(image)
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            prepared = _to_webp_mode(image)
            prepared.save(staging, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
            size = prepared.size
        staging.replace(destination)
        return size
    except Exception:
        staging.unlink(missing_ok=True)
        raise


def _shrink_on_decode(image: Image.Image, max_side: int) -> None:
    """JPEG 让解码器直接按目标尺寸解，省掉一整张原图的解码峰值。"""
    if image.format == "JPEG":
        image.draft("RGB", (max_side, max_side))


def _to_webp_mode(image: Image.Image) -> Image.Image:
    """WebP 支持 RGB 与 RGBA；调色板图和 CMYK 先转过去。"""
    if image.mode in ("RGB", "RGBA"):
        return image
    if "A" in image.getbands():
        return image.convert("RGBA")
    return image.convert("RGB")
