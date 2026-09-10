# -*- coding: utf-8 -*-
"""生态审计 B4 红线实证：policy 表混进 pipeline 目录 → 递归加载是否整目录失败。

复现 P2a-Q1 定案的布局红线：Resource.post_pipeline 递归读取目录内全部 json，
policy.json 的段名（perception/policy/actuators…）会被当节点解析。
临时目录实验，不触碰真源目录。
"""
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.resource import Resource  # noqa: E402

CORE = REPO / "maaracing_assistant" / "core" / "resources" / "pipeline"
TR = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "pipeline"
POLICY = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"

tmp = Path(tempfile.mkdtemp(prefix="navkit-audit-redline-"))
try:
    for f in CORE.glob("*.json"):
        shutil.copy2(f, tmp / f.name)
    for f in TR.glob("*.json"):
        shutil.copy2(f, tmp / f.name)
    shutil.copy2(POLICY, tmp / POLICY.name)  # 红线动作：policy 进 pipeline 目录
    res = Resource()
    job = res.post_pipeline(str(tmp)).wait()
    status = ("succeeded" if job.succeeded else
              "failed" if job.failed else "other")
    print(f"[redline] policy 混入 pipeline 目录: job={status} loaded={res.loaded}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    print("[redline] 临时目录已清理:", not tmp.exists())
