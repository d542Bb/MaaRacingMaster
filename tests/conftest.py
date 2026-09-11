# -*- coding: utf-8 -*-
"""pytest 全局配置。

为 `strategy`（原 bid_strategy）提供可导入路径：策略模块只依赖标准库，直接以「模块目录」导入，
**不经过** maaracing_master 包的 __init__（会触发 registry → plugins/* 插件扫描，
拉入 maa/vgamepad/opencv 等重依赖，拖慢单测并污染 CI）。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 测试进程不走 maaracing_master 包入口，无法继承 __init__.py 的禁用；
# 此处同样关闭字节码写入，避免 tests/ 与直导目录散落 __pycache__。
sys.dont_write_bytecode = True

_PROJ = Path(__file__).resolve().parent.parent

# 把 treasure 插件目录加入 sys.path，使 `from strategy import ...` 生效
_STRATEGY_DIR = _PROJ / "maaracing_master" / "plugins" / "treasure"
if str(_STRATEGY_DIR) not in sys.path:
    sys.path.insert(0, str(_STRATEGY_DIR))

# 把项目根加入 sys.path，使 `from tools.navkit...`（P3）在 CI（`pytest` 不带
# `python -m`）下也可导入——cwd 非 project root 时不自动在 sys.path。
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))