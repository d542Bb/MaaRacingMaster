#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""global 资产 NavKit adapter（G4：在调试台暴露 core 侧 global 资产段）。

global 与 treasure 的性质差异（决定本 adapter 的取舍）：
- 纯 v3：真源是 `core/resources/config/global_assets.json`（页名 + 大厅骨架锚点），
  **无 v2 rois 文件、无 debug 会话帧、无决策流水**——它是跨模块共用的导航骨架，
  不是某个玩法的局内识别。
- 因此本 adapter 只服务 v3 资产查看/编辑（`/api/assets`、`/api/graph`），
  v2 校准半区（ROI 拖动/匹配）对 global 无意义。

避开写盘陷阱（关键）：
- `main()` 无条件调用 `ensure_rois(state)`，它按 v2 类目读写 `rois_path()`。
- 若把 `rois_path()` 直接指回 v3 的 `global_assets.json`，`ensure_rois` 会用 v2 视图
  JSON 往返**重排该 git 跟踪文件的格式**并落盘——每次开台都无谓改写真源，不可接受。
- 故 `rois_path()` 指向用户数据目录（gitignored）里的一个 v2 缓存文件，让 ensure_rois
  有处安放；真正的 v3 编辑走 `assets_path_for("global")`（server 已加 global 分支），
  与 rois 缓存互不相干。treasure 的两文件分离（v2 rois + v3 assets）与此同构。
"""
from __future__ import annotations

from pathlib import Path

from tools.navkit.core.categories import CategoryDefs
from tools.navkit.core.session import SessionBrowser

from maaracing_assistant.core.navkit import CORE_IMAGE_DIR
from maaracing_assistant.core.paths import debug_dir

PROJ = Path(__file__).resolve().parent.parent.parent.parent

# v2 校准半区对 global 无内容：声明单一 "anchors" 段（结构上兼容 v3 anchors 形态），
# 只为满足 CategoryDefs 非空约束与 ensure_rois 幂等建缓存，不承载可校准语义。
CATEGORIES: tuple[str, ...] = ("anchors",)


def make_category_defs() -> CategoryDefs:
    return CategoryDefs(CATEGORIES, name="global")


def session_dir() -> Path:
    # global 无 debug 会话；指向 debug_dir()/global 使会话/流水浏览自然为空。
    return debug_dir() / "global"


def make_session_browser() -> SessionBrowser:
    return SessionBrowser(session_dir())


def template_dir() -> Path:
    # global 模板图住在 core 段，与运行时检测/路由解析同源。
    return CORE_IMAGE_DIR


def rois_path() -> Path:
    """v2 校准缓存（用户数据目录，gitignored），刻意与 v3 真源分离。

    见模块 docstring「避开写盘陷阱」——绝不能指回 global_assets.json。
    """
    return debug_dir() / "global" / "_studio_v2_rois_cache.json"


def register_endpoints(state) -> None:
    """global 无领域端点（无 OCR/彩蛋）。

    存在即告知 `build_state` 走 adapter 装配、跳过对缓存文件的缺省填充写盘；
    真正的 v3 查看/编辑能力由 server 通用端点（/api/assets、/api/graph）承载。
    """
    return None
