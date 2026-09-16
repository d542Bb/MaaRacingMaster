"""把门控日志里的判定帧号导成可直接看的画面拼图（门控校准用）。

**要回答的问题**：这次门控判定落在什么画面上——判早了还是判晚了？

**为什么需要它**：`logger` 的时间戳只到秒且是墙钟，录制器用单调时钟（`perf_counter_ns`），
两个时基对不上；唯一的共同坐标是**帧号**——`plugins/speedrush/module.py` 的 `_frame_note`
把 `frame=` 写进每条门控判定日志。本工具把"日志里的判定点"翻译成"一叠能直接看的帧"：
每个判定点导出一张拼图，标记帧居中、加框，左右是它前后的若干帧。

**判读口径**：看标记帧及其前后——若"已进入驾驶页"那一帧还在起步动画上（画面是过场/黑屏/
车尚未出现在赛道上），说明就绪判据偏松；若"已离开对局"那一帧落在过渡动画中间，说明容差
或复查间隔需要调。**标记帧前后各若干帧的变化过程**比单帧更能说明问题。

用法：
    .venv/Scripts/python.exe tools/experiments/speedrush_gating/export_gating_marks.py \
        --session "<录制会话目录>" [--log <日志文件>] [--context 4] [--out <输出目录>]

`--session` 指 `data/speedrush/demos/<时间戳>_p<N>/`（含 frames.jsonl 与 frames/）。
不带 `--log` 时自动取用户数据目录 `logs/` 下最近修改的 .log。
输出：`<会话目录>/gating_marks/mark_<序号>_<帧号>.jpg`，并在控制台打印帧号 → 标记文本的对应表。

**尚未在真机数据上跑过**（本工具随门控取证链路一起提交，首次使用时先核对输出是否与日志一致）。
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.core import opencv_utf8_patch  # noqa: F401,E402 —— 中文路径 IO，须先于 cv2 文件操作
from maaracing_master.core.paths import logs_dir  # noqa: E402

_MARK_RE = re.compile(r"frame=(\d+)")
# 只认本模块的门控行：日志里其它模块也可能出现 frame=（如鉴宝的 trace 摘要）
_MODULE_TAG = "极速狂飙"


def load_frames(session: Path) -> list[tuple[int, Path]]:
    """读 frames.jsonl → [(frame_id, 帧文件路径)]，按键号升序。"""
    rows: list[tuple[int, Path]] = []
    with (session / "frames.jsonl").open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            rows.append((int(r["frame_id"]), session / "frames" / str(r["file"])))
    rows.sort(key=lambda t: t[0])
    return rows


def load_marks(log_path: Path) -> list[tuple[str, int]]:
    """从日志抽门控判定点 → [(该行文本, frame_id)]，保持出现顺序。"""
    marks: list[tuple[str, int]] = []
    with log_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if _MODULE_TAG not in line:
                continue
            m = _MARK_RE.search(line)
            if m:
                marks.append((line.strip(), int(m.group(1))))
    return marks


def nearest_log() -> Path:
    """用户数据目录 logs/ 下最近修改的 .log（会话目录形态 logs/<会话>/MaaRM_*.log）。"""
    cands = sorted(logs_dir().rglob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        raise SystemExit(f"logs/ 下没有 .log：{logs_dir()}")
    return cands[0]


def _window(rows: list[tuple[int, Path]], frame_id: int, context: int):
    """取标记帧及其前后 context 帧；返回 (是否精确命中, 窗口起点下标, 窗口)。"""
    ids = [fid for fid, _ in rows]
    i = bisect.bisect_left(ids, frame_id)
    hit = i < len(ids) and ids[i] == frame_id
    lo = max(0, i - context)
    hi = min(len(rows), i + context + 1)
    return hit, lo, rows[lo:hi]


def _read(path: Path, height: int) -> np.ndarray | None:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return None
    scale = height / float(img.shape[0])
    return cv2.resize(img, (int(img.shape[1] * scale), height), interpolation=cv2.INTER_AREA)


def _grid(images: list[np.ndarray], cols: int, pad: int = 6) -> np.ndarray:
    """按 cols 列排成网格（补黑边），返回一张图。"""
    cols = max(1, min(cols, len(images)))
    h, w = images[0].shape[:2]
    rows = (len(images) + cols - 1) // cols
    canvas = np.zeros((rows * h + (rows - 1) * pad, cols * w + (cols - 1) * pad, 3), np.uint8)
    for k, img in enumerate(images):
        r, c = divmod(k, cols)
        canvas[r * (h + pad):r * (h + pad) + h, c * (w + pad):c * (w + pad) + w] = img
    return canvas


def export(session: Path, log_path: Path, context: int, cols: int,
           out_dir: Path, height: int) -> int:
    rows = load_frames(session)
    if not rows:
        raise SystemExit(f"frames.jsonl 为空：{session}")
    marks = load_marks(log_path)
    if not marks:
        raise SystemExit(
            f"日志里没有门控判定点（含 {_MODULE_TAG} 且带 frame= 的行）：{log_path}\n"
            "提示：判定日志由 plugins/speedrush/module.py 的 _frame_note 打出，"
            "确认跑的是含该实现之后的版本。")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"会话：{session}\n日志：{log_path}\n判定点 {len(marks)} 个，帧共 {len(rows)} 张\n")

    written = 0
    for n, (text, fid) in enumerate(marks, 1):
        hit, start, win = _window(rows, fid, context)
        imgs = []
        for k, (wfid, path) in enumerate(win):
            img = _read(path, height)
            if img is None:
                continue
            if wfid == fid:
                # 标记帧加红框：它回答"判定落在哪一帧"，前后帧回答"过程是怎么走的"
                cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), (0, 0, 255), 3)
            cv2.putText(img, f"#{k + start} f={wfid}", (6, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
            imgs.append(img)
        if not imgs:
            print(f"[{n}] frame={fid} 附近的帧文件都读不出来，跳过")
            continue
        canvas = _grid(imgs, cols)
        name = f"mark_{n:02d}_{fid}.jpg"
        cv2.imwrite(str(out_dir / name), canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        written += 1
        flag = "命中" if hit else f"未精确命中（窗口从此帧之后开始，最近邻差 {abs(rows[start][0] - fid)} 帧）"
        print(f"[{n}] {flag} frame={fid} → {out_dir / name}")
        print(f"     日志行：{text}")
    print(f"\n共导出 {written} 张 → {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="门控判定点 → 画面拼图")
    ap.add_argument("--session", required=True, help="录制会话目录（含 frames.jsonl 与 frames/）")
    ap.add_argument("--log", default=None, help="门控日志文件；省略则取 logs/ 下最近修改的 .log")
    ap.add_argument("--context", type=int, default=4, help="标记帧前后各取几帧（默认 4）")
    ap.add_argument("--cols", type=int, default=5, help="拼图列数（默认 5）")
    ap.add_argument("--height", type=int, default=220, help="每帧缩放后的高度像素（默认 220）")
    ap.add_argument("--out", default=None, help="输出目录；默认 <会话目录>/gating_marks")
    args = ap.parse_args(argv)

    session = Path(args.session)
    if not (session / "frames.jsonl").is_file():
        raise SystemExit(f"不是录制会话目录（缺 frames.jsonl）：{session}")
    log_path = Path(args.log) if args.log else nearest_log()
    if not log_path.is_file():
        raise SystemExit(f"日志文件不存在：{log_path}")
    out_dir = Path(args.out) if args.out else session / "gating_marks"
    return export(session, log_path, max(0, args.context), args.cols, out_dir, args.height)


if __name__ == "__main__":
    raise SystemExit(main())
