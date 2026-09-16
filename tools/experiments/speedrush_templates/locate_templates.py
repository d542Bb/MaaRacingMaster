"""speedrush 模板定位与 ROI 计算探针。

用途：把人工裁剪的模板图丢进真机截帧做模板匹配，定位它在 1280×720 内容区中的
实际位置，再按"模板尺寸外扩"算出归一化搜索区域 rect，供 navkit 真源写入。

⚠ 坐标系坑点（2026-09-16 实测）：人工截图工具产出的 PNG 是**带窗口边框的窗口截图**
（1281×759），而运行时 MaaFramework 帧是 1280×720 纯客户区。实测偏移为
x=1 / y=38（x=0 是左边框，0~37 行是标题栏）。模板必须先在内容区坐标系里定位，
否则 ROI 会整体偏 38 像素、运行期全部失配。

自检：脚本启动时用 treasure 既有模板（来自真正的 720p 运行帧）在内容区匹配，
分数应达 ~1.0000，以此确认偏移量正确。

输入：模板 PNG（Downloads 目录） + 真机截帧（image-cache 目录）
输出：控制台报告 + templates_roi.json，并把模板复制进插件资源目录

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_templates/locate_templates.py

注意：本探针属 tools/experiments/，豁免 ruff 门禁。
"""

from __future__ import annotations

import glob
import json
import os

import cv2

SRC_DIR = "C:/Users/yomen/Downloads"
DST_DIR = "maaracing_master/plugins/speedrush/resources/image"
FRAME_GLOB = "C:/Users/yomen/.zcode/cli/image-cache/sess_b4ad42bf-3e3c-49e5-a3a7-f1f753355af3/*.png"
OUT_JSON = "tools/experiments/speedrush_templates/templates_roi.json"

# 内容区偏移与尺寸（实测：窗口截图 → 720p 客户区）
OFFSET_X, OFFSET_Y = 1, 38
CONTENT_W, CONTENT_H = 1280, 720

# 自检用：treasure 既有模板（来自真正的运行帧）
SELFCHECK_TPL = "maaracing_master/plugins/treasure/resources/image/hall_peak_appraise_card.png"
SELFCHECK_MIN = 0.99

# 模板名（不含扩展名），与人工裁剪文件名一致
TEMPLATES = [
    "hall_race_btn",
    "rank_fun_tab",
    "fun_speedrush_card",
    "speedrush_find_opponent_btn",
    "speedrush_giveup_btn",
    "speedrush_reward_hint",
    "speedrush_store_confirm_btn",
    "speedrush_store_sell_title",
    "speedrush_stage_label",
    "speedrush_round_end_detail_btn",
    "speedrush_continue_btn",
    "speedrush_giveup_continue_btn",
    "speedrush_rank_score_label",
]

# 外扩口径：每边外扩模板宽度的 EXPAND_W、高度的 EXPAND_H
EXPAND_W = 0.15
EXPAND_H = 0.25


def load_content(path: str):
    """读截帧并裁出 720p 客户区。"""
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        return None
    return img[OFFSET_Y : OFFSET_Y + CONTENT_H, OFFSET_X : OFFSET_X + CONTENT_W]


def selfcheck(frames: list[str]) -> None:
    """用 treasure 运行帧模板校验内容区偏移是否正确。"""
    tpl = cv2.imread(SELFCHECK_TPL, cv2.IMREAD_COLOR)
    if tpl is None:
        print("自检跳过：未找到参照模板")
        return
    best = max(
        (cv2.matchTemplate(load_content(f), tpl, cv2.TM_CCOEFF_NORMED).max(), f)
        for f in frames
    )
    ok = best[0] >= SELFCHECK_MIN
    print(f"自检：treasure 参照模板最高匹配分={best[0]:.4f} → {'偏移正确 ✓' if ok else '偏移可疑 ✗'}")
    if not ok:
        raise SystemExit("内容区偏移校验失败，先修正 OFFSET 再继续")


def main() -> None:
    frames = sorted(glob.glob(FRAME_GLOB))
    if not frames:
        raise SystemExit("未找到真机截帧，检查 FRAME_GLOB")
    raw = cv2.imread(frames[0], cv2.IMREAD_COLOR)
    print(f"截帧数={len(frames)}  原始尺寸={raw.shape[1]}x{raw.shape[0]}  "
          f"内容区偏移=({OFFSET_X},{OFFSET_Y}) → {CONTENT_W}x{CONTENT_H}")
    selfcheck(frames)

    results: dict[str, dict] = {}
    print()
    print(f"{'模板':34s} {'尺寸':>11s} {'匹配分':>7s} {'内容区位置':>14s}  rect")
    print("-" * 112)

    for name in TEMPLATES:
        src = f"{SRC_DIR}/{name}.png"
        if not os.path.exists(src):
            print(f"{name:34s}   源文件缺失")
            continue
        tpl = cv2.imread(src, cv2.IMREAD_COLOR)  # 丢弃 alpha，统一 3 通道
        th, tw = tpl.shape[:2]
        os.makedirs(DST_DIR, exist_ok=True)
        cv2.imwrite(f"{DST_DIR}/{name}.png", tpl)

        best_score, best_frame, best_loc = -1.0, "", (0, 0)
        for fr in frames:
            content = load_content(fr)
            if content is None:
                continue
            res = cv2.matchTemplate(content, tpl, cv2.TM_CCOEFF_NORMED)
            _, maxv, _, maxloc = cv2.minMaxLoc(res)
            if maxv > best_score:
                best_score, best_frame, best_loc = maxv, fr, maxloc

        x, y = best_loc
        mx, my = round(tw * EXPAND_W), round(th * EXPAND_H)
        x1, y1 = max(0, x - mx), max(0, y - my)
        x2, y2 = min(CONTENT_W, x + tw + mx), min(CONTENT_H, y + th + my)
        rect = [round(x1 / CONTENT_W, 4), round(y1 / CONTENT_H, 4),
                round(x2 / CONTENT_W, 4), round(y2 / CONTENT_H, 4)]

        results[name] = {
            "template_size": [tw, th],
            "match_score": round(float(best_score), 4),
            "frame": os.path.basename(best_frame),
            "pixel_rect": [x1, y1, x2, y2],
            "rect": rect,
        }
        flag = ""
        if best_score < 0.90:
            flag = "  ⚠低分"
        elif best_score < 0.98:
            flag = "  需人工核对"
        print(f"{name:34s} {tw:5d}x{th:<5d} {best_score:7.4f} ({x:4d},{y:4d})  {rect}{flag}")

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print()
    print(f"已写出 {OUT_JSON}")
    print(f"已复制 {len(results)} 张模板到 {DST_DIR}")


if __name__ == "__main__":
    main()