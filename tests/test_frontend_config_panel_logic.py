# -*- coding: utf-8 -*-
"""配置面板时序纯逻辑（js/config-panel-state.js）的 Node 测试入口。

规则本身不碰 DOM，单独用 node --test 覆盖：旧轮询晚到、保存串行与 pending 合并、
卡片代际作废。CI 不装 node 时自动跳过（本地开发机有 node 即真实执行）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
TEST_FILE = REPO / "tests" / "js" / "config_panel_state.test.mjs"


@pytest.mark.skipif(NODE is None, reason="node 不可用，跳过前端时序纯逻辑测试")
def test_config_panel_state_logic():
    proc = subprocess.run(
        [NODE, "--test", str(TEST_FILE)],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
