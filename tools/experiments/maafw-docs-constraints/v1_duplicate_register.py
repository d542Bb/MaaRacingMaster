#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V-1 最小实验：maafw 5.12.3 下 Custom 注册命名空间的实际行为。

判据来源：官方文档 2.2「MaaResourceRegisterCustomRecognition」——
「名称不得为空。自定义识别器和自定义动作共享一个区分大小写的命名空间。
若名称为空或已被任一类型注册，则返回 false 且保留已有注册；如需替换，应先移除原注册。」
（该表述为 v5.13 文档化；5.12.3 行为是否一致即本实验问题。）

实验点：
  A1 同名识别器二次注册            → 返回值？
  A2 跨类型同名（识别占用后注册动作）→ 返回值？（共享命名空间验证）
  A3 空名注册                      → 返回值？
  A4 仅大小写不同的名字            → 第二次应成功
  B1 重复注册失败后，任务实际命中哪个实现（期望：旧实现，证明"保留已有注册"）
  B2 binding 在调 C 前先把新实例写入 _custom_recognition_holder：
     二次注册 + 释放旧实例全部 Python 引用 + gc 后跑任务
     → C 侧指向的旧实现是否悬垂（崩 / 未定义行为）？
  C  unregister 后同名再 register   → 成功且新实现生效
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from maa.controller import CustomController  # noqa: E402
from maa.custom_action import CustomAction  # noqa: E402
from maa.custom_recognition import CustomRecognition  # noqa: E402
from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402

HITS: list[str] = []


class TaggedReco(CustomRecognition):
    """命中时记录自身 tag，用于判定 C 侧实际调用了哪个实现。"""

    def __init__(self, tag: str):
        super().__init__()
        self.tag = tag

    def analyze(self, context, argv):
        HITS.append(self.tag)
        return CustomRecognition.AnalyzeResult(box=(10, 10, 20, 20), detail={"tag": self.tag})


class TaggedAct(CustomAction):
    """供跨类型命名空间测试使用的真实 CustomAction 实例。"""

    def run(self, context, argv) -> bool:
        return True


def build_fake_controller_cls():
    """自动覆盖全部 raise NotImplementedError 钩子的最小 CustomController（黑帧、无副作用）。

    5.12.3 binding 的 CustomController 非 ABCMeta，抽象性靠方法体 raise，
    故按源码文本探测需打桩的方法名。
    """
    import inspect

    ns = {}
    for base in CustomController.__mro__:
        for name, fn in vars(base).items():
            if not callable(fn):
                continue
            try:
                src = inspect.getsource(fn)
            except (TypeError, OSError):
                continue
            if "NotImplementedError" in src:
                ns[name] = lambda self, *a, **k: True
    ns["request_uuid"] = lambda self: "exp-v1-uuid"
    ns["screencap"] = lambda self: np.zeros((720, 1280, 3), dtype=np.uint8)
    ns["is_running"] = lambda self: True
    return type("FakeController", (CustomController,), ns)


PIPE = {
    "v1_entry": {"recognition": "DirectHit", "action": "DoNothing", "next": ["v1_reco"]},
    "v1_reco": {
        "recognition": "Custom",
        "custom_recognition": "Dup",
        "action": "DoNothing",
        "timeout": 3000,
        "rate_limit": 200,
    },
}


def run_task(res: Resource) -> tuple[bool, str]:
    ctrl_cls = build_fake_controller_cls()
    ctrl = ctrl_cls()
    tasker = Tasker()
    tasker.bind(res, ctrl)
    ctrl.post_connection().wait()
    job = tasker.post_task("v1_entry", pipeline_override=PIPE)
    job.wait()
    return (not job.failed), f"status={job.status}"


def scenario(tag: str, register_fn) -> None:
    print(f"\n===== {tag} =====")
    register_fn()


def a_return_values() -> None:
    """A1~A4 + C：注册返回值语义（无需执行）。"""
    res = Resource()
    r1, r2 = TaggedReco("R1"), TaggedReco("R2")
    print(f"A1 首次 register_reco('Dup', R1) -> {res.register_custom_recognition('Dup', r1)}")
    print(f"A1 同名 register_reco('Dup', R2) -> {res.register_custom_recognition('Dup', r2)}")
    print(f"A2 跨类型 register_action('Dup') -> {res.register_custom_action('Dup', TaggedAct())}")
    print(f"A3 空名 register_reco('')        -> {res.register_custom_recognition('', r2)}")
    print(f"A4 大小写 register_reco('dup')   -> {res.register_custom_recognition('dup', r2)}")
    print(f"C  unregister_reco('Dup')        -> {res.unregister_custom_recognition('Dup')}")
    print(f"C  再 register_reco('Dup', R3)   -> {res.register_custom_recognition('Dup', TaggedReco('R3'))}")
    print(f"C  空转 unregister 不存在的名字  -> {res.unregister_custom_recognition('NoSuchName')}")


def b1_hit_target() -> None:
    """B1：同名二次注册（A1 实测返回 True）后，任务实际命中哪个实现。"""
    print("\n===== B1 重复注册后实际命中的实现 =====")
    res = Resource()
    r1 = TaggedReco("R1")
    print(f"register R1 -> {res.register_custom_recognition('Dup', r1)}")
    print(f"register R2 -> {res.register_custom_recognition('Dup', TaggedReco('R2'))}")
    HITS.clear()
    ok, detail = run_task(res)
    print(f"任务完成 ok={ok} {detail} 命中序列={HITS}")
    print("判定：R2 → 5.12.3 同名再注册=覆盖生效（热重载安全）；R1 → 保留旧注册（文档口径）")


def b2_dangling_probe() -> None:
    """B2：旧实例 Python 引用全释放后再跑任务（若覆盖生效则应稳定命中 R2 且无崩溃）。"""
    print("\n===== B2 旧实例被 GC 后跑任务（悬垂探针）=====")
    res = Resource()
    r1 = TaggedReco("R1")
    print(f"register R1 -> {res.register_custom_recognition('Dup', r1)}")
    print(f"register R2 -> {res.register_custom_recognition('Dup', TaggedReco('R2'))}")
    del r1  # R1 已失去 holder 引用，此处释放最后一个 Python 引用
    gc.collect()
    time.sleep(0.1)
    HITS.clear()
    try:
        ok, detail = run_task(res)
        print(f"未崩溃：ok={ok} {detail} 命中序列={HITS}")
    except Exception as exc:  # noqa: BLE001
        print(f"崩溃/异常：{type(exc).__name__}: {exc}")


def c_new_impl_active() -> None:
    """C：unregister 后重注册，新实现应生效。"""
    print("\n===== C unregister 后重注册生效验证 =====")
    res = Resource()
    r1 = TaggedReco("R1")
    r2 = TaggedReco("R2")
    print(f"register R1 -> {res.register_custom_recognition('Dup', r1)}")
    print(f"unregister  -> {res.unregister_custom_recognition('Dup')}")
    print(f"register R2 -> {res.register_custom_recognition('Dup', r2)}")
    HITS.clear()
    ok, detail = run_task(res)
    print(f"任务完成 ok={ok} {detail} 命中序列={HITS}")
    print("期望：命中 R2")


def main() -> int:
    a_return_values()
    b1_hit_target()
    b2_dangling_probe()
    c_new_impl_active()
    return 0


if __name__ == "__main__":
    sys.exit(main())
