# -*- coding: utf-8 -*-
"""P2a-Q1 实验：帧注入控制器 + v4 真图加载冒烟 + Custom 桥执行通路。

目的（对应 v4-p2a-engine.md AC-1/AC-3）：
1. 证明 `CustomController.screencap()` 可以把 wgcap 缓存帧注入 MaaFW 执行通路；
2. 实测 `post_pipeline` 加载 v4 真源目录的行为——含 policy.json 是否污染节点表
   （plan §3 布局的地雷检测），以及 `$` 开头根字段是否被官方忽略；
3. 最小图跑通 Custom(MRA_*) + v2 嵌套 Or 的执行（stub 桥，真桥归 Q2/Q3）。

用法：.venv\\Scripts\\python.exe tools\\experiments\\v4-p2a-wiring\\wiring_smoke.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from maa.controller import CustomController  # noqa: E402
from maa.custom_action import CustomAction  # noqa: E402
from maa.custom_recognition import CustomRecognition  # noqa: E402
from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402

NAV_DIR = REPO / "maaracing_assistant" / "core" / "resources" / "nav"
TREASURE_NAV = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "nav"
TREASURE_POLICY = REPO / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "policy" / "treasure.policy.json"


class InjectController(CustomController):
    """WgcapController 的胚胎：screencap 返回固定合成帧并计数。"""

    def __init__(self):
        super().__init__()
        self.frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.screencap_count = 0
        self.click_count = 0

    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "maawiring-smoke"

    def start_app(self, intent):
        return True

    def stop_app(self, intent):
        return True

    def screencap(self):
        self.screencap_count += 1
        return self.frame

    def click(self, x, y):
        self.click_count += 1
        return True

    def swipe(self, x1, y1, x2, y2, duration):
        return True

    def touch_down(self, contact, x, y, pressure):
        return True

    def touch_move(self, contact, x, y, pressure):
        return True

    def touch_up(self, contact):
        return True

    def click_key(self, keycode):
        return True

    def input_text(self, text):
        return True

    def key_down(self, keycode):
        return True

    def key_up(self, keycode):
        return True


class StubTemplate(CustomRecognition):
    hits = 0
    errors: list[str] = []

    def analyze(self, context, argv):
        # 固定返回画面中央小框，验证 Custom 参数以字符串透传且执行链可达
        StubTemplate.hits += 1
        try:
            param = json.loads(argv.custom_recognition_param or "{}")
            result = CustomRecognition.AnalyzeResult(box=(600, 300, 80, 40),
                                                     detail={"echo": param.get("mode", "?")})
            return result
        except Exception as exc:  # 框架会吞掉桥内异常，这里显影
            StubTemplate.errors.append(f"{type(exc).__name__}: {exc}")
            raise


class StubClick(CustomAction):
    def run(self, context, argv):
        return True


SMOKE = {
    "$smoke_ver": 4,
    "smoke.entry": {"recognition": "DirectHit", "action": "DoNothing", "next": ["smoke.or"]},
    "smoke.or": {
        "recognition": {"type": "Or", "param": {"any_of": [
            {"recognition": "Custom", "custom_recognition": "MRA_Template",
             "custom_recognition_param": {"mode": "template",
                                          "rect": [0.4, 0.3, 0.5, 0.4],
                                          "templates": ["no_such.png"]}},
            {"recognition": "TemplateMatch", "template": "no_such.png", "threshold": 0.1},
        ]}},
        "action": "Custom", "custom_action": "MRA_Click",
        "custom_action_param": {"wait_after_ms": 0},
        "next": ["smoke.end"], "timeout": 3000, "rate_limit": 50,
    },
    "smoke.end": {"recognition": "DirectHit", "action": "DoNothing"},
}


def make_dir(with_policy_contamination: bool) -> Path:
    d = Path(tempfile.mkdtemp(prefix="v4wiring_"))
    shutil.copy(NAV_DIR / "global.json", d / "global.json")
    shutil.copy(TREASURE_NAV / "treasure.json", d / "treasure.json")
    if with_policy_contamination:
        shutil.copy(TREASURE_POLICY, d / "treasure.policy.json")
    (d / "smoke.json").write_text(json.dumps(SMOKE, ensure_ascii=False), encoding="utf-8")
    return d


def try_load(tag: str, d: Path) -> tuple[bool, list[str]]:
    res = Resource()
    res.register_custom_recognition("MRA_Template", StubTemplate())
    res.register_custom_action("MRA_Click", StubClick())
    job = res.post_pipeline(str(d)).wait()
    ok = bool(job.succeeded)
    nodes: list[str] = []
    if ok:
        getter = getattr(res, "get_node_list", None)
        if callable(getter):
            nodes = list(getter() or [])
    print(f"[{tag}] post_pipeline succeeded={ok} nodes={len(nodes)}")
    return ok, nodes


def main() -> int:
    # 1) 污染试验：policy.json 与节点图同目录（plan §3 现状布局）
    d_bad = make_dir(with_policy_contamination=True)
    ok_bad, nodes_bad = try_load("含 policy.json 目录", d_bad)
    policy_leak = [n for n in nodes_bad if n in ("perception", "actuators", "policy", "_v3_schema_ver")]
    print(f"    策略表键泄漏为节点: {policy_leak or '无'}")

    # 2) 隔离试验：policy.json 移出加载根
    d_good = make_dir(with_policy_contamination=False)
    ok_good, nodes_good = try_load("policy 隔离后目录", d_good)
    dollar = [n for n in nodes_good if n.startswith("$")]
    print(f"    $ 前缀根字段成为节点: {dollar or '无（官方豁免成立）'}")
    treasure = [n for n in nodes_good if n.startswith("treasure.")]
    glob = [n for n in nodes_good if n.startswith("global.")]
    smoke = [n for n in nodes_good if n.startswith("smoke.")]
    print(f"    节点统计 treasure={len(treasure)} global={len(glob)} smoke={len(smoke)}")

    # 3) 执行冒烟：注入帧 + Custom 桥 + v2 嵌套 Or
    ctrl = InjectController()
    res2 = Resource()
    res2.register_custom_recognition("MRA_Template", StubTemplate())
    res2.register_custom_action("MRA_Click", StubClick())
    res2.post_pipeline(str(d_good)).wait()
    tasker = Tasker()
    tasker.bind(res2, ctrl)
    inited = tasker.inited
    job = tasker.post_task("smoke.entry")
    job.wait()
    detail = job.get()
    print(f"[执行冒烟] tasker.inited={inited} job.succeeded={job.succeeded} job.done={job.done} "
          f"screencap 次数={ctrl.screencap_count} StubTemplate 命中尝试={StubTemplate.hits} "
          f"桥内异常={StubTemplate.errors or '无'}")
    ok_exec = bool(job.succeeded) and ctrl.screencap_count > 0

    verdict = {
        "AC1_加载": ok_good,
        "AC1_美元豁免": not dollar,
        "AC1_policy地雷复现": (not ok_bad) or bool(policy_leak),
        "AC3_注入执行": ok_exec,
    }
    print(json.dumps({k: v for k, v in verdict.items() if k != "expect注记"}, ensure_ascii=False))
    shutil.rmtree(d_bad, ignore_errors=True)
    shutil.rmtree(d_good, ignore_errors=True)
    return 0 if all(v for k, v in verdict.items() if k != "expect注记") else 1


if __name__ == "__main__":
    sys.exit(main())
