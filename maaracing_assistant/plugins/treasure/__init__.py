# -*- coding: utf-8 -*-
"""巅峰鉴宝插件包。

插件自包含契约：代码、模板图、感知/决策真源全部位于本目录 resources/ 内，
registry 按 manifest.py 自动发现；整个目录拷走即卸载、放入即安装。
"""

from functools import lru_cache
from pathlib import Path

# 插件根目录（plugins/treasure/）
PLUGIN_DIR = Path(__file__).resolve().parent
# 资源根与分类目录（模板图 image/、感知/决策真源 policy/、图节点 pipeline/）
RES_DIR = PLUGIN_DIR / "resources"
IMAGE_DIR = RES_DIR / "image"
PIPELINE_DIR = RES_DIR / "pipeline"
POLICY_PATH = RES_DIR / "policy" / "treasure.policy.json"


@lru_cache(maxsize=1)
def nav_source():
    """policy.json 数据面（NavSource）的进程内缓存加载，供鉴宝运行时共用。

    契约（承接 v3_assets 的降级与冷生效语义，P4b 换数据源）：
    - 文件缺失或加载异常 → 返回 ``None``；调用方以 None 触发既有降级路径
      （detector 检测跳过 / 模板装载器回代码常量）。
    - N-3 冷生效：改真源下次启动生效，故缓存不失效。
    - `core.navkit.v4_source` 为纯标准库底座，函数内延迟导入，
      不在包导入期引入任何重依赖。
    """
    if not POLICY_PATH.exists():
        return None
    try:
        from maaracing_assistant.core.navkit.v4_source import load_nav_source
        return load_nav_source(POLICY_PATH)
    except Exception:
        return None
