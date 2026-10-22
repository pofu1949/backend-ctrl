"""管理端静态资源。

界面是同一进程里的轻量单页应用：HTML、CSS、JS 都由 FastAPI 提供，
数据只走 ``/api/admin/v1``。这样不额外引入 Node 构建链或常驻开发服务器，
也符合企划第 14 章“服务端渲染或一个轻量前端”的约束。
"""

from __future__ import annotations

from pathlib import Path

#: 管理端目录。安装成 wheel 后这些文件仍在 ``ctrlstore/admin_ui`` 下。
ADMIN_UI_DIR = Path(__file__).resolve().parent
INDEX_HTML = ADMIN_UI_DIR / "index.html"
ASSETS_DIR = ADMIN_UI_DIR / "assets"
