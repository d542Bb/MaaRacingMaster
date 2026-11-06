# -*- coding: utf-8 -*-
"""归档插件测试的本地配置。

归档测试不参与默认收集（`pyproject.toml` 的 `testpaths = ["tests"]`），考古时显式运行：

    .venv/Scripts/python.exe -m pytest maaracing_master/plugins/_archive/treasure/tests -m retired
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

_REPO = Path(__file__).resolve().parents[5]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
