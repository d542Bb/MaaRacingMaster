#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小实验：出价面板的「数字键」按下 A 后是否被游戏接受（C 类事实，不靠猜）。

背景（真机 MaaRM_20260915_071523，2026-09-15）
------------------------------------------------
07:17:34 程序提交 `bid_numpad_2`（导航到位 + 按 A），此后到 07:17:58 的 24 秒里：
  - 游戏输入框始终为空（抽 raw 帧 0713/0764 确认，光标规规矩矩停在 '2' 键上）；
  - `_bid_input_progress` 不推进 → 意图与指纹不变 → 指纹锁按设计不再重发；
  - 同局 07:17:28 的「智能出价」与 07:17:31 的「✖ 清空」按下去都生效。
所以「A 键通道」本身没坏，分型只剩三种可能，且必须真机区分：
  (甲) 数字键需要额外前置（先把输入框点活/聚焦），否则游戏忽略数字键；
  (乙) 导航落点或时序问题（光标没真停在键中心 / A 按得太早）；
  (丙) 输入框 OCR 读区（bid_result_amount_box）读不到已输入的 '2'。

本脚本只做一件事：**在光标已经停好的前提下按一次 A**，并给出可客观判定的前后帧差异。
导航不由本脚本负责（用生产链路的 GUI「仅意图」模式，或你自己的手柄把光标摆好）。
自包含、不 import maaracing_master 业务代码（见 tools/experiments/README.md）。

跑法（仓库根，务必用 .venv 的 Python）
--------------------------------------
    # 0) 基线：不按任何键，量出游戏自身动画造成的「自然差异」
    .venv\\Scripts\\python.exe tools/experiments/bid-numpad-keypress/probe_keypress.py \\
        --title 巅峰极速 --dry-run --label baseline

    # 1) 目标键：光标停在数字键「2」上
    .venv\\Scripts\\python.exe tools/experiments/bid-numpad-keypress/probe_keypress.py \\
        --title 巅峰极速 --label numpad2

    # 2) 对照键：光标停在「智能出价」上（已知生效的键）
    .venv\\Scripts\\python.exe tools/experiments/bid-numpad-keypress/probe_keypress.py \\
        --title 巅峰极速 --label smart

判定矩阵（把三组输出的「变化像素占比 / 包围盒」填进 README）
------------------------------------------------------------
    numpad2 ≈ baseline 且 smart ≫ baseline  → (甲)：数字键需先激活输入框
    numpad2 ≫ baseline 且 smart ≫ baseline  → (乙)：A 与落点都没问题，回头查导航/时序或 (丙) OCR 读区
    numpad2 ≈ smart ≈ baseline              → A 没送达该面板（输入通道/面板焦点）
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

import cv2
import numpy as np

_OUT_DIR = Path(__file__).resolve().parent / "out"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]


def find_window(title_substr: str) -> int:
    """按标题子串找可见顶层窗口，返回 hwnd（找不到抛 RuntimeError）。"""
    hits: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_substr in buf.value:
            hits.append((int(hwnd), buf.value))
        return True

    _user32.EnumWindows(_cb, 0)
    if not hits:
        raise RuntimeError(f"未找到标题含 {title_substr!r} 的可见窗口")
    for hwnd, title in hits:
        print(f"[窗口] hwnd={hwnd} title={title!r}")
    return hits[0][0]


class _Grabber:
    """最小 WGC 抓帧器：只保留最新一帧，按需取（自包含，不引业务代码）。"""

    def __init__(self, hwnd: int):
        from windows_capture import WindowsCapture

        self._latest: np.ndarray | None = None
        self._fid = 0
        cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=False,
                             draw_border=False, minimum_update_interval=0,
                             dirty_region=False)

        @cap.event
        def on_frame_arrived(frame, _control):
            self._latest = np.asarray(frame.frame_buffer).copy()  # BGRA
            self._fid += 1

        @cap.event
        def on_closed():
            print("[WGC] 捕获窗口已关闭")

        self._cap = cap
        cap.start_free_threaded()

    def grab(self, timeout: float = 3.0) -> np.ndarray:
        """等一帧新的，返回 BGR ndarray。"""
        fid0 = self._fid
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self._fid > fid0 and self._latest is not None:
                return cv2.cvtColor(self._latest, cv2.COLOR_BGRA2BGR)
            time.sleep(0.01)
        if self._latest is None:
            raise RuntimeError("WGC 未收到任何帧（窗口被最小化？）")
        return cv2.cvtColor(self._latest, cv2.COLOR_BGRA2BGR)

    def close(self):
        try:
            self._cap.stop()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)  # 原生线程收摊宽限（与生产 WgcCapture.stop 同口径）
        self._latest = None


class _VirtualPad:
    """最小虚拟手柄：只按一个键，press → update → 保持 → release → update。"""

    def __init__(self, button_name: str):
        import vgamepad as vg

        self._vg = vg
        self._pad = vg.VX360Gamepad()
        self._btn = getattr(vg.XUSB_BUTTON, f"XUSB_GAMEPAD_{button_name.upper()}")

    def tap(self, hold_ms: int) -> None:
        self._pad.press_button(self._btn)
        self._pad.update()
        time.sleep(hold_ms / 1000.0)
        self._pad.release_button(self._btn)
        self._pad.update()


def _diff_report(before: np.ndarray, after: np.ndarray, out_png: Path,
                 thresh: int = 12) -> tuple[int, float, tuple[int, int, int, int]]:
    """打印/保存两帧差异：变化像素数、占比、包围盒（帧像素坐标）。"""
    d = cv2.absdiff(before, after)
    mask = (d.max(axis=2) > thresh).astype(np.uint8)
    changed = int(mask.sum())
    ratio = changed / float(mask.size)
    if changed:
        x, y, w, h = cv2.boundingRect(mask)
    else:
        x = y = w = h = 0
    cv2.imwrite(str(out_png), cv2.convertScaleAbs(d, alpha=4))  # 差异放大 4 倍便于肉眼看
    return changed, ratio, (x, y, w, h)


def main() -> int:
    ap = argparse.ArgumentParser(description="出价面板单键按下最小实验")
    ap.add_argument("--title", default="巅峰极速", help="游戏窗口标题子串")
    ap.add_argument("--label", default="probe", help="本次实验标签（输出文件前缀）")
    ap.add_argument("--button", default="A", help="要按的手柄键（A/B/X/Y/LB/RB）")
    ap.add_argument("--hold-ms", type=int, default=150, help="按住时长（默认 150ms，同生产）")
    ap.add_argument("--repeat", type=int, default=3, help="重复次数（默认 3）")
    ap.add_argument("--settle-s", type=float, default=0.8, help="松手后等画面稳定的秒数")
    ap.add_argument("--dry-run", action="store_true",
                    help="基线对照：不按任何键，只采前后两帧量自然差异")
    ap.add_argument("--out", default=str(_OUT_DIR), help="输出目录")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    hwnd = find_window(args.title)
    grabber = _Grabber(hwnd)
    pad = None if args.dry_run else _VirtualPad(args.button)

    print(f"\n[准备] 输出目录: {out}")
    print(f"[准备] 目标: 光标停在要测的键上，出价面板保持打开；"
          f"{'仅采集（不按键）' if args.dry_run else f'将按 {args.button} 键'} × {args.repeat}")
    input("按 Enter 开始（先在游戏里把光标摆好）...")

    rows = []
    try:
        for i in range(1, args.repeat + 1):
            before = grabber.grab()
            time.sleep(0.2)
            before = grabber.grab()
            if pad is not None:
                pad.tap(args.hold_ms)
                print(f"[{i}/{args.repeat}] 已按 {args.button}（{args.hold_ms}ms）")
            else:
                print(f"[{i}/{args.repeat}] 基线采样（未按键）")
            time.sleep(args.settle_s)
            after = grabber.grab()
            time.sleep(0.2)
            after = grabber.grab()

            b_png = out / f"{args.label}_{i:02d}_before.png"
            a_png = out / f"{args.label}_{i:02d}_after.png"
            d_png = out / f"{args.label}_{i:02d}_diff.png"
            cv2.imwrite(str(b_png), before)
            cv2.imwrite(str(a_png), after)
            changed, ratio, box = _diff_report(before, after, d_png)
            rows.append((i, changed, ratio, box))
            print(f"         变化像素 {changed}（{ratio * 100:.3f}%），"
                  f"包围盒 x={box[0]} y={box[1]} w={box[2]} h={box[3]}")
            print(f"         存图 {b_png.name} / {a_png.name} / {d_png.name}")
    except KeyboardInterrupt:
        print("\n[中断] 用户中止")
    finally:
        if pad is not None:
            try:  # 保证不把键按死
                pad._pad.release_button(pad._btn)  # noqa: SLF001 —— 探针内最小实现
                pad._pad.update()                  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
        grabber.close()

    if rows:
        worst = max(r[2] for r in rows)
        print(f"\n[汇总] {args.label}: 最大变化占比 {worst * 100:.3f}%"
              f"（{'基线' if args.dry_run else f'{args.button} 键'}）")
        print("[判读] 与 --dry-run 基线比较：显著大于基线 = 按键确实改变了画面；"
              "≈ 基线 = 未生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
