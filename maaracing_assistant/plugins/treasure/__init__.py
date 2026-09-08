# -*- coding: utf-8 -*-
"""巅峰鉴宝插件包。

插件自包含契约：代码、模板图、ROI 配置全部位于本目录 resources/ 内，
registry 按 manifest.py 自动发现；整个目录拷走即卸载、放入即安装。
"""

import os
from functools import lru_cache
from pathlib import Path

# 插件根目录（plugins/treasure/）
PLUGIN_DIR = Path(__file__).resolve().parent
# 资源根与分类目录（模板图 image/、ROI 配置 config/）
RES_DIR = PLUGIN_DIR / "resources"
IMAGE_DIR = RES_DIR / "image"
CONFIG_DIR = RES_DIR / "config"

V3_ASSETS_PATH = CONFIG_DIR / "treasure_assets.json"


@lru_cache(maxsize=1)
def v3_assets():
    """`treasure_assets.json`（schema v3）的进程内缓存加载，供鉴宝各运行时读取点共用。

    契约（TREASURE_V2_V3_SINGLE_SOURCE_PLAN / N-6 降级回退）：
    - `NAVKIT_SOURCE=v2`、文件缺失、或加载异常 → 返回 ``None``；调用方以 None 触发 v2 回退
      （保持 AGENTS.md「v3 加载失败回退 v2」约定，四常量/v2 JSON 兜底继续可用）。
    - N-3 冷生效：改资产下次启动生效，故缓存不失效。
    - `navkit.Assets` 为纯标准库底座，函数内延迟导入，不在包导入期引入任何重依赖。
    """
    if os.environ.get("NAVKIT_SOURCE", "v3").lower() == "v2":
        return None
    if not V3_ASSETS_PATH.exists():
        return None
    try:
        from maaracing_assistant.core.navkit import Assets
        return Assets.load(V3_ASSETS_PATH, module="treasure")
    except Exception:
        return None
