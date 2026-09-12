#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包后 runtime 行为冒烟（由 assemble.ps1 §5.7 用包内 python 执行）。

为什么 import 级自检不够（2026-09-11 真机事故）：EXP-5A 误删 numpy.ctypeslib
后，`import numpy` / `import maa` 全部正常——只有 MaaFW C++ 线程回调 Python
读帧时（maa ImageBuffer.get → numpy.ctypeslib.as_array）才炸，且异常被
ctypes 吞掉只进 stderr，主日志无痕，症状是全部 custom recognition 静默失效、
点击盲落屏幕左上角。裁剪破坏的往往是这类惰性运行时路径，静态检查（grep
找使用者、文件存在性、import 冒烟）对它们全体失明。

覆盖面只有第三方 runtime（不 import maaracing_master——验的是依赖自身，
与被裁的白名单一一对应）：
  1. maa_buffers        ImageBuffer/RectBuffer <-> numpy 的 C 层往返
                        （事故第一现场：ImageBuffer.get 用 numpy.ctypeslib）
  2. maa_pipeline_probe 最小单节点图：custom recognition 收注入帧回固定框 →
                        custom action 断言 argv.box 一致（完整 C++ 回调链）
  3. cv2_template_chain imencode/imdecode 往返 + matchTemplate
                        （EXP-5A/9 的 cv2/numpy 裁剪面）
  4. rapidocr_construct RapidOCR() 构造（3.9.2 构造即加载 det+cls+rec 三个
                        onnx）+ 一次小图推理（B4'-0 模型完整性同类）
  INFO dml_providers    onnxruntime 可用 provider 列表只打印不断言
                        （DML 需要显卡驱动，同 vgamepad/ViGEmBus 降级先例）

退出码 0 = 全绿；1 = 至少一项红（assemble 据此报构建失败）。
"""
import json
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np

BOX = (999, 591, 104, 42)          # 与真机事故同形的固定框
FRAME_SHAPE = (720, 1280, 3)


def check_maa_buffers():
    from maa.buffer import ImageBuffer, RectBuffer
    img = np.zeros(FRAME_SHAPE, dtype=np.uint8)
    img[100, 200] = (11, 22, 33)
    buf = ImageBuffer()
    assert buf.set(img), "ImageBuffer.set 返回 False"
    out = buf.get()                 # 事故点：内部走 numpy.ctypeslib.as_array
    assert out.shape == FRAME_SHAPE, f"帧形状不符: {out.shape}"
    assert tuple(int(v) for v in out[100, 200]) == (11, 22, 33), "像素往返不一致"
    rb = RectBuffer()
    assert rb.set(BOX), "RectBuffer.set 返回 False"
    r = rb.get()
    assert (r.x, r.y, r.w, r.h) == BOX, f"Rect 往返不一致: {(r.x, r.y, r.w, r.h)}"


def check_maa_pipeline_probe():
    from maa.custom_action import CustomAction
    from maa.custom_recognition import CustomRecognition
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker

    frame = np.zeros(FRAME_SHAPE, dtype=np.uint8)

    class ProbeController(CustomController):
        def connect(self):
            return True

        def connected(self):
            return True

        def request_uuid(self):
            return "release-smoke"

        def screencap(self):
            return frame.copy()

        def start_app(self, intent):
            return True

        def stop_app(self, intent):
            return True

        def click(self, x, y):
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

    class ProbeReco(CustomRecognition):
        def analyze(self, context, argv):
            # 走到这里说明 binding 已把 C++ 帧成功转成 numpy（ImageBuffer.get）
            if argv.image is None or argv.image.shape[0] != FRAME_SHAPE[0]:
                raise AssertionError(f"注入帧异常: {None if argv.image is None else argv.image.shape}")
            return self.AnalyzeResult(box=BOX, detail={"smoke": 1})

    received = []

    class ProbeAct(CustomAction):
        def run(self, context, argv):
            received.append(tuple(argv.box))
            return True

    resource, tasker = Resource(), Tasker()
    resource.register_custom_recognition("SMOKE_Reco", ProbeReco())
    resource.register_custom_action("SMOKE_Click", ProbeAct())
    graph = {"smoke": {
        "recognition": "Custom", "custom_recognition": "SMOKE_Reco",
        "action": "Custom", "custom_action": "SMOKE_Click",
        "timeout": 5000, "rate_limit": 0,
    }}
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "smoke.json"
        f.write_text(json.dumps(graph), encoding="utf-8")
        assert not resource.post_pipeline(str(f)).wait().failed, "post_pipeline 失败"
        tasker.bind(resource, ProbeController())
        job = tasker.post_task("smoke")
        job.wait()
    assert received and received[0] == BOX, \
        f"custom recognition→action box 贯通断裂: {received or '动作未执行'}"


def check_cv2_template_chain():
    import cv2
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 40, FRAME_SHAPE, dtype=np.uint8)  # 噪声底
    # 带纹理模板（纯色块在 TM_CCOEFF_NORMED 下零方差区域相关未定义，处处满分）
    tex = rng.integers(120, 255, (42, 104, 3), dtype=np.uint8)
    frame[100:142, 200:304] = tex
    ok, enc = cv2.imencode(".png", tex)
    assert ok, "imencode 失败"
    dec = cv2.imdecode(enc, cv2.IMREAD_COLOR)   # 生产读图路径（utf8_patch 同源）
    assert dec is not None and dec.shape == tex.shape, "imdecode 往返失败"
    res = cv2.matchTemplate(frame, dec, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    assert max_val > 0.99 and max_loc == (200, 100), \
        f"matchTemplate 定位异常: val={max_val:.3f} loc={max_loc}"


def check_rapidocr_construct():
    from rapidocr import RapidOCR
    eng = RapidOCR()               # 3.9.2：构造即加载 det+cls+rec 三个 onnx
    res = eng(np.full((64, 64, 3), 255, dtype=np.uint8))
    assert res is not None, "RapidOCR 推理返回 None"


CHECKS = (
    ("maa_buffers", check_maa_buffers),
    ("maa_pipeline_probe", check_maa_pipeline_probe),
    ("cv2_template_chain", check_cv2_template_chain),
    ("rapidocr_construct", check_rapidocr_construct),
)


def main():
    try:
        import onnxruntime as ort
        print(f"[smoke][info] onnxruntime providers: {ort.get_available_providers()}")
    except Exception as exc:  # noqa: BLE001 —— 仅信息行，不判负
        print(f"[smoke][info] onnxruntime providers 查询失败（不判负）: {exc!r}")

    failed = []
    for name, fn in CHECKS:
        try:
            fn()
            print(f"[smoke] {name}: PASS")
        except BaseException as exc:  # noqa: BLE001 —— 构建脚本要求任何异常都收口成红
            failed.append(name)
            print(f"[smoke] {name}: FAIL -> {exc!r}")
            traceback.print_exc(limit=6)
    print(f"[smoke] {'ALL PASS' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
