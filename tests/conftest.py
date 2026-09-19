# -*- coding: utf-8 -*-
"""pytest 全局配置。

把项目根加入 sys.path，使 `tools.navkit...` 等仓库内导入在 CI（`pytest` 不带
`python -m`）下也可导入——cwd 非 project root 时不自动在 sys.path。

插件代码的测试一律走绝对包路径导入（maaracing_master.plugins.<id>.…）：
包 __init__ 与插件 __init__ 均为轻依赖（实测不拉 maa/opencv），且这是与生产
代码一致的唯一导入口径——不再为个别测试保留 sys.path 平铺导入的旁门。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 测试进程不走 maaracing_master 包入口，无法继承 __init__.py 的禁用；
# 此处同样关闭字节码写入，避免 tests/ 与直导目录散落 __pycache__。
sys.dont_write_bytecode = True

_PROJ = Path(__file__).resolve().parent.parent

# 把项目根加入 sys.path，使 `from tools.navkit...`（P3）在 CI（`pytest` 不带
# `python -m`）下也可导入——cwd 非 project root 时不自动在 sys.path。
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))