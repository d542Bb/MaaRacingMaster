# -*- coding: utf-8 -*-
"""MoGe 原生 FOV 随车速爬坡探针（stdout-only，2026-10-08 用户提案）。

背景：游戏疑似动态 FOV（FOV=f(speed)）。若成立，MoGe 官方 infer 从点图
恢复的归一化焦距（fx/fy → fov=2·atan(0.5/f)）就是免费的自车速度锚。
此前用超车段中段帧做 fx-速度相关被场景噪声淹没（速度动态范围太小）。
本探针按用户提案改用起步爬坡段：0→全速连续爬坡、同场景连续镜头、
右上角得分速度此时就是 m/s 真值（尚未吃金币，1s 窗滞后）。

方法：对一局 debug raw 帧逐帧跑产线同款 MoGe（q4f16 静态 336×598），
recover_focal_shift 恢复焦距 → fov_x/fov_y（度）。地面真值由 hud.jsonl
rate_b 与帧号映射另行对齐（帧 9 ↔ rate 6 的爬坡起点，Δ≈0.38s/帧）。

用法：.venv python probe_moge_fov_ramp.py <dir_or_frames...>
  目录时取其中 *_raw.jpg 全部帧；文件时逐个处理。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

import moge2_post as mp

WEIGHTS = Path(r"D:\maaracing_assistant\maaracing_master\plugins\speedrush"
               r"\resources\onnx\depth\moge2_vits_static_336x598_t1032_q4f16.onnx")
IN_W, IN_H = 598, 336


def load_session() -> ort.InferenceSession:
    avail = [p for p in ("DmlExecutionProvider", "CPUExecutionProvider")
             if p in ort.get_available_providers()]
    so = ort.SessionOptions()
    for name, dim in zip(("batch_size", "height", "width"), (1, IN_H, IN_W)):
        so.add_free_dimension_override_by_name(name, int(dim))
    return ort.InferenceSession(str(WEIGHTS), sess_options=so, providers=avail)


def fov_deg(f: float) -> float:
    return 2.0 * math.degrees(math.atan(0.5 / f))


def main() -> None:
    frames: list[Path] = []
    for a in sys.argv[1:]:
        p = Path(a)
        if p.is_dir():
            frames.extend(sorted(p.glob("*_raw.jpg")))
        else:
            frames.append(p)
    if not frames:
        print("无输入帧")
        return
    sess = load_session()
    print(f"模型 {WEIGHTS.name}  帧数 {len(frames)}  providers={sess.get_providers()}")
    print(f"{'frame':<14} {'fov_x':>7} {'fov_y':>7} {'fx':>7} {'fy':>7} {'valid%':>7}")
    for f in frames:
        bgr = cv2.imread(str(f))
        if bgr is None:
            print(f"{f.name:<14} 读取失败")
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        blob = mp.preprocess(rgb, IN_W, IN_H)
        points, mask, metric_scale = mp.forward(sess, blob, 1032)
        res = mp.reconstruct(points, mask, metric_scale)
        fx, fy = res["fx"], res["fy"]
        valid = float(res["valid"].mean()) * 100.0
        print(f"{f.name:<14} {fov_deg(fx):7.2f} {fov_deg(fy):7.2f} "
              f"{fx:7.4f} {fy:7.4f} {valid:6.1f}")


if __name__ == "__main__":
    main()
