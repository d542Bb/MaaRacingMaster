# -*- coding: utf-8 -*-
"""MaaFw 生态审计：policy 表混进 pipeline 目录 → 递归加载是否整目录失败。

复现布局红线：Resource.post_pipeline 递归读取目录内全部 json，policy 数据的段名
（perception/policy/actuators…）会被当节点解析。临时目录实验，不触碰真源目录。
"""
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from maa.resource import Resource  # noqa: E402

PACK = REPO / "maaracing_master"
CORE = PACK / "core" / "resources" / "startup"
PLUGIN = PACK / "plugins" / "speedrush" / "resources" / "pipeline"
POLICY = PACK / "plugins" / "speedrush" / "resources" / "policy" / "decision.json"

tmp = Path(tempfile.mkdtemp(prefix="pipeline-audit-redline-"))
try:
    for f in CORE.glob("*.json"):
        shutil.copy2(f, tmp / f.name)
    for f in PLUGIN.glob("*.json"):
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
