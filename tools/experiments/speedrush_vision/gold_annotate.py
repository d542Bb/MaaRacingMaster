"""金标帧标注器（极简版）：每侧点两下成一条边界线 + 键选类别/障碍。

口径：碰撞边界（黄线外沿/墙基）是 3D 直线 ⇒ 画面投影是直线 ⇒ **每侧两个点唯一确定**。
点序固定：① 左侧·近（画面下方）② 左侧·远（画面上方）→ 按类别键 →
③ 右侧·近 ④ 右侧·远 → 按类别键 → [d 有障碍] → Enter 保存。
类别键：w=墙/护栏  c=抬升路缘  x=该侧无可见边界（开阔）。
点的位置 = "车不能再往那边去"的那条线的外沿（贴线走，不点线上方的墙身）。
u=重标本帧  q=退出。断点续标：已在 labels CSV 的帧自动跳过。

用法（仓库根 .venv）：
  python tools/experiments/speedrush_vision/gold_annotate.py [--list 路径.csv]
输出 = 清单同目录 gold_labels.csv。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import cv2

DEF_LIST = (Path(os.environ.get("APPDATA", ".")) / "MaaRacingMaster" / "data" /
            "speedrush" / "depth_review" / "gold_frames.csv")
OUT_COLS = ["path", "stratum", "l_nx", "l_ny", "l_fx", "l_fy", "lcls",
            "r_nx", "r_ny", "r_fx", "r_fy", "rcls", "obstacle"]
KEYCLS = {ord("w"): "wall", ord("c"): "kerb", ord("x"): "skip"}
STEPS = ["左侧·点近处", "左侧·点远处", "右侧·点近处", "右侧·点远处", "选右类别→Enter"]


def draw(frame, pts, cls, obstacle, hint):
    im = frame.copy()
    for pair, c in zip((pts[:2], pts[2:]), cls):
        if all(p is not None for p in pair):
            cv2.line(im, pair[0], pair[1], (0, 0, 0), 5)
        col = (0, 255, 0) if c == "wall" else (0, 200, 255) if c == "kerb" else (200, 200, 200)
        for p in pair:
            if p is not None:
                cv2.circle(im, p, 7, col, -1)
                cv2.circle(im, p, 7, (0, 0, 0), 1)
    if obstacle:
        cv2.rectangle(im, (0, 0), (frame.shape[1] - 1, frame.shape[0] - 1), (0, 0, 255), 3)
    cv2.putText(im, hint, (8, frame.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (255, 255, 255), 1)
    return im


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default=str(DEF_LIST))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    lst = Path(args.list)
    out = Path(args.out) if args.out else lst.parent / "gold_labels.csv"
    items = [(r["path"], r["stratum"]) for r in csv.DictReader(lst.open(encoding="utf-8"))]
    HDR = OUT_COLS
    rows = {}
    if out.exists():
        for r in csv.DictReader(out.open(encoding="utf-8")):
            if r.get("l_nx") is not None:
                rows[r["path"]] = [r.get(c, "") for c in HDR]

    def save():
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HDR)
            for p, _ in items:
                if p in rows:
                    w.writerow(rows[p])

    todo_n = sum(1 for p, _ in items if p not in rows)
    print(f"[gold] 清单 {len(items)} 帧，已标 {len(items) - todo_n}，待标 {todo_n}（b=回上一帧重标）")
    if not todo_n:
        return
    win = "gold"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 1280, 740)
    st = {"pts": [None] * 4, "cls": ["", ""], "obs": False}

    def on_mouse(ev, x, y, _f, _p):
        if ev == cv2.EVENT_LBUTTONDOWN and None in st["pts"]:
            st["pts"][st["pts"].index(None)] = (int(x), int(y))

    cv2.setMouseCallback(win, on_mouse)
    i = 0
    while i < len(items):
        p, s = items[i]
        if p in rows:
            i += 1
            continue
        st.update(pts=[None] * 4, cls=["", ""], obs=False)
        frame = cv2.imread(p)
        if frame is None:
            print(f"[gold] 读不到，跳过：{p}")
            i += 1
            continue
        while True:
            n = sum(pt is not None for pt in st["pts"])
            step = STEPS[min(n, 4)]
            if n == 2 and not st["cls"][0]:
                step = "左侧类别 w/c/x"
            elif n == 4 and not st["cls"][1]:
                step = "右侧类别 w/c/x"
            hint = (f"[{i + 1}/{len(items)}] {s}  {Path(p).name}  {step}  "
                    f"L[{st['cls'][0]}] R[{st['cls'][1]}] obs={st['obs']}")
            cv2.imshow(win, draw(frame, st["pts"], st["cls"], st["obs"], hint))
            k = cv2.waitKey(30) & 0xFF
            if k == ord("q"):
                cv2.destroyAllWindows()
                sys.exit(0)
            elif k == ord("b"):
                j = i - 1
                while j >= 0 and items[j][0] not in rows:
                    j -= 1
                if j >= 0:
                    del rows[items[j][0]]
                    save()
                    i = j
                break
            elif k == ord("d"):
                st["obs"] = not st["obs"]
            elif k == ord("u"):
                st.update(pts=[None] * 4, cls=["", ""], obs=False)
            elif k in KEYCLS:
                if k == ord("x") and not st["cls"][0] and n < 2:
                    st["cls"][0] = "skip"
                    st["pts"][0] = st["pts"][1] = (0, 0)
                elif not st["cls"][0] and n >= 2:
                    st["cls"][0] = KEYCLS[k]
                elif not st["cls"][1] and (n >= 4 or k == ord("x")):
                    st["cls"][1] = KEYCLS[k]
                    if k == ord("x"):
                        st["pts"][2] = st["pts"][3] = (0, 0)
            elif k == 13 and n == 4 and all(st["cls"]):
                ln, lf, rn, rf = st["pts"]
                rows[p] = [p, s, ln[0], ln[1], lf[0], lf[1], st["cls"][0],
                           rn[0], rn[1], rf[0], rf[1], st["cls"][1], int(st["obs"])]
                save()
                print(f"[gold] +1 {Path(p).name} {st['cls']} obs={st['obs']}")
                i += 1
                break
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
