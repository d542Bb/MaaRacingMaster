# -*- coding: utf-8 -*-
"""S1 影子：轻量车道线观测（TwinLiteNet+ Small）的推理、跟踪与状态机。

**只写 trace，不接控制**。生产者：`LaneObsWorker` 在独立线程跑
（默认 provider=cpu，不抢 GPU 锁——实测 dml 与深度共用锁会相位锁，把深度
饿到只剩 5% 帧；dml 仍可选，但要自证不与深度争锁），队列深度 1，控制拍只
push 最新帧并读 `latest()`；推理失败/权重缺失一律返回 None，不抛进控制环。

坐标系与离线 gold 同源：地面线满足 ``x_px = VPX + L*LANE_W*(y-Y_H)/cam_h``，
线的横向偏移 ``L = slope*cam_h/LANE_W``（与道号无关）；自车横位
``x = k - L``。**k 不再由颜色/黄边界决定**：每拍用深度路心做锚点
``k_i = round(anchor + L_i)``，等间距与残差门剔除野线，多条线取残差中值
还原横位。多线共同证实的新鲜锚点当拍重锚，单线仍等连续两拍；深度空窗里
新线由所有在期线投票定号，未过门的新线与被拒线都不留道号。颜色只用于
显示与黄线拟合符号质量门。

状态机：
  TRACK  有通过等间距/残差门的线：输出锚定后的绝对横位；
  COAST  短断链（≤``COAST_MAX_TICKS``）：保持上次横位，用恒速+横向速度预测；
  RELOCK 长断链/跳帧：清空可用性，等下一拍深度锚点或线重锁。
"""
from __future__ import annotations

import math
import queue
import threading

from maaracing_master.core.perf_sampler import beat
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from maaracing_master.core import dml_lock

LANE_W = 3.5
VPX = 640.7
Y_H = 354.2
FOCAL = 807.7
DEFAULT_CAM_H = 1.28
# 保险杠机位物理高度带（2026-10-29 深度尺度 ×0.893 后重标）；坏平面帧的
# coef[2] 仍会飙到 3.2~4.5，线几何不能用它。
CAM_H_LO = 1.12
CAM_H_HI = 1.55
YELLOW_MIN = 0.6
WHITE_MIN = 0.6
NEAR_Y = (560.0, 720.0)
FAR_Y = (440.0, 545.0)
PROB_THRESH = 0.30
TRACK_TOL = 0.15
TRACK_GAP = 6
COAST_MAX_TICKS = 10
# 线为主（2026-10-31）：线自身连续稳定这么多拍后，道号不再让新鲜深度改判，
# 深度也不再能否决线——整道级深度跳变时两根线会"整齐地"支持错误网格，正是
# 路心整道跳、场景参考系反复否决的来源。
LINE_LOCK_TICKS = 5
LINE_THETA_MAX = 0.30      # 线角（rad）；变道中车头相对道路可达 ~0.2
LINE_RMS_MAX = 2.5         # 单线拟合残差（px，短段/边缘线不参与位置原点）
LINE_JUMP_RESET = 0.5      # 线横位单拍跳超此值（道）= 连续性中断，重新锁定
# 黄边界侧别硬约束：用最靠车头的几个点判画面左右侧，压中线留死区。
SIDE_NEAR_PTS = 5
SIDE_DEADBAND_PX = 20.0


@dataclass(frozen=True)
class LaneObsConfig:
    enabled: bool = False
    weights: str = ""
    hz: float = 10.0
    provider: str = "cpu"      # cpu=独立线程不抢 GPU 锁；dml=与深度共用锁
    threads: int = 4           # 仅 CPU：4 线程实测约 23ms/帧，仍在控制拍内
    detect_every: int = 1      # 每 N 帧做一次完整检测；1=逐拍检测，中间帧只预测
    model_input: tuple[int, int] = (384, 640)


@dataclass
class _Track:
    L: float
    color: str
    idx: int
    v: float = 0.0
    seen: int = 0
    k: int | None = None
    resid: float | None = None
    k_pending: int | None = None
    k_pending_n: int = 0


def _letterbox(rgb: np.ndarray) -> np.ndarray:
    import cv2
    im = cv2.resize(rgb, (640, 360), interpolation=cv2.INTER_AREA)
    im = cv2.copyMakeBorder(im, 12, 12, 0, 0, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return np.ascontiguousarray(
        (im.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])


def _prob_from_out(out) -> np.ndarray:
    logits = out[1][0][:, 12:372, :]
    e = np.exp(logits - logits.max(0, keepdims=True))
    return (e[1] / e.sum(0)).astype(np.float32)


def _lane_prob(sess, rgb: np.ndarray) -> np.ndarray:
    inp = sess.get_inputs()[0].name
    return _prob_from_out(sess.run(None, {inp: _letterbox(rgb)}))


def _row_segments(prob: np.ndarray) -> list[tuple[int, list[dict]]]:
    rows = []
    for yc in range(206, 360, 3):
        row = prob[yc]
        idx = np.flatnonzero(row > PROB_THRESH)
        if len(idx) == 0:
            continue
        splits = np.flatnonzero(np.diff(idx) > 1) + 1
        segs = []
        for seg in np.split(idx, splits):
            if len(seg) == 0:
                continue
            p = row[seg]
            segs.append({"yc": yc, "xc": float((seg * p).sum() / p.sum()),
                         "p": float(p.mean())})
        if segs:
            rows.append((yc, segs))
    return rows


def _build_tracks(rows, tol: float = 13.0, max_gap: int = 10) -> list[list[dict]]:
    tracks: list[dict] = []
    for yc, segs in rows:
        used = set()
        for tr in tracks:
            tr["_pred"] = None
            if yc - tr["yc"] > max_gap:
                continue
            tr["_pred"] = tr["xc"] + tr.get("v", 0.0) * (yc - tr["yc"])
        for seg in segs:
            best, best_d = None, tol
            for j, tr in enumerate(tracks):
                if j in used or tr["_pred"] is None:
                    continue
                d = abs(seg["xc"] - tr["_pred"])
                if d < best_d:
                    best, best_d = j, d
            if best is None:
                tracks.append({"pts": [seg], "yc": yc, "xc": seg["xc"], "v": 0.0})
                used.add(len(tracks) - 1)
            else:
                tr = tracks[best]
                dv = (seg["xc"] - tr["xc"]) / max(1, yc - tr["yc"])
                tr["v"] = 0.7 * tr.get("v", 0.0) + 0.3 * float(np.clip(dv, -8, 8))
                tr["pts"].append(seg)
                tr["yc"], tr["xc"] = yc, seg["xc"]
                used.add(best)
    return [t["pts"] for t in tracks]


def _near_x(pts) -> float | None:
    """最靠车头的几个点的全幅像素横坐标中值（判左右侧用）。"""
    near = pts[-SIDE_NEAR_PTS:]
    if not near:
        return None
    return float(np.median([2.0 * q["xc"] for q in near]))


def _side_from_x(x: float | None) -> int | None:
    """画面左侧=-1、右侧=+1；压在中线死区内不给侧别。"""
    if x is None or abs(x - VPX) < SIDE_DEADBAND_PX:
        return None
    return -1 if x < VPX else 1


def _lane_cam_h(cam_h: float | None) -> tuple[float, bool]:
    """线几何用的相机高度：越出保险杠物理高度带就回退标定值。

    深度平面坏帧的 coef[2] 实测可达 3.2~4.5（正常 1.26~1.29）；线的 L 与
    cam_h 成正比，直接采用会把同一根线放大三倍多，算出错 1~2 道的横位。
    """
    if cam_h is None or not (CAM_H_LO <= cam_h <= CAM_H_HI):
        return DEFAULT_CAM_H, True
    return float(cam_h), False


def _fit(pts, cam_h: float, y_lo: float, y_hi: float):
    use = [q for q in pts if y_lo <= 2.0 * q["yc"] <= y_hi]
    if len(use) < 4:
        return None
    ys = np.array([2.0 * q["yc"] for q in use])
    xs = np.array([2.0 * q["xc"] for q in use])
    wt = np.array([max(0.1, q["p"]) for q in use])
    if ys.max() - ys.min() < 40:
        return None
    A = np.vstack([ys, np.ones_like(ys)]).T
    s, a = np.linalg.lstsq(A * wt[:, None], xs * wt, rcond=None)[0]
    L = float(s * cam_h / LANE_W)
    theta = math.atan((VPX - a - L * LANE_W * Y_H / cam_h) / FOCAL)
    rms = float(np.sqrt(np.average((xs - (s * ys + a)) ** 2, weights=wt)))
    return {"L": L, "theta": float(theta), "rms": rms,
            "y0": float(ys.min()), "y1": float(ys.max()), "n": len(ys)}


def _color(hsv: np.ndarray, pts) -> tuple[float, float]:
    H, W = hsv.shape[:2]
    ny = nw = n = 0
    step = max(1, len(pts) // 12)      # 12 个采样点足够判色，少抢 GIL
    for q in pts[::step]:
        x, y = int(round(2 * q["xc"])), int(round(2 * q["yc"]))
        if not (0 <= x < W and 0 <= y < H):
            continue
        win = hsv[max(0, y - 1):y + 2, max(0, x - 5):x + 6]
        h, s, v = (win[..., 0].astype(int), win[..., 1].astype(int),
                   win[..., 2].astype(int))
        n += 1
        if ((h >= 14) & (h <= 42) & (s >= 110) & (v >= 90)).any():
            ny += 1
        if ((s < 75) & (v >= 125)).any():
            nw += 1
    return (ny / n if n else 0.0), (nw / n if n else 0.0)


class LineTracker:
    """线观测状态机：等间距约束 + 深度路心定整数道号（2026-10-24 重写）。

    颜色不再是道号身份。跨帧只按拟合横位 L 关联；每条线当前帧的整数道号由
    **深度路心**给出：``k = round(anchor + L)``，残差 ``r = anchor + L - k``
    是这条线的子道偏差。多条线取残差中值还原自车横位（等间距由整数分配 +
    残差门共同保证），重复道号/离群线淘汰。深度空窗时锚点退回上一拍横位
    （模型/线），线只做子道细化，不再自己漂绝对位；大跳变需连续
    ``ANCHOR_CONFIRM`` 拍同向一致才换锚。
    """

    OFF_MAX = 3.0
    ANCHOR_AGREE_TOL = 0.5
    ANCHOR_CONFIRM = 2
    RESID_MAX = 0.45
    # 多线共同证实锚点时的收紧门：0.5 道级的一拍深度噪声不能同时拟合进
    # 两根线，避免整道翻转；超过就退回单线两拍确认。
    STRONG_RESID = 0.30
    OUTLIER_TOL = 0.35
    SPREAD_MAX = 0.55

    def __init__(self) -> None:
        self.tracks: list[_Track] = []
        self.state = "RELOCK"
        self.x_last: float | None = None
        self.x_prev: float | None = None
        self.x_vel = 0.0
        self.lost = 0
        self.locked = 0            # 连续 TRACK 稳定性拍数（线为主的门）
        self.grid_reject = 0
        self.side_reject = 0
        self.last_theta: float | None = None
        self.last_lines: list[dict] = []
        self.anchor_src = "none"

    @staticmethod
    def _side_bad(c: dict) -> bool:
        """黄线拟合符号与画面侧别矛盾：这条线不能当绝对参照。"""
        side = c.get("side")
        return (c.get("color") == "y" and side in (-1, 1)
                and ((side < 0 and float(c["L"]) > 0)
                     or (side > 0 and float(c["L"]) < 0)))

    def _neighbor_k(self, c: dict, fid: int) -> int | None:
        """无深度锚时用所有在期线投票给新线定道号；有分歧就不定。

        只看最近一条邻居时，长空窗里那条邻居自己漂了一道，新线就会跟着
        错一道，并一直保持到下一次深度重锚。等间距下每条在期线都能独立给
        出一票，全部同票才采用。
        """
        L = float(c["L"])
        votes: list[int] = []
        for t in self.tracks:
            if t.k is None:
                continue
            gap_now = fid - t.idx
            if gap_now > TRACK_GAP:
                continue
            gap = L - (t.L + t.v * gap_now)
            if abs(gap - round(gap)) > self.RESID_MAX:
                continue
            votes.append(int(t.k + int(round(gap))))
        if not votes:
            return None
        if any(v != votes[0] for v in votes[1:]):
            return None
        return votes[0]

    def _assign_k(self, c: dict, x_anchor: float | None,
                  fid: int, strong_anchor: bool = False,
                  line_locked: bool = False) -> bool:
        """给一条线定/重锚持久道号；False=本拍不能参选横位。

        线已连续锁定时（``line_locked``）：道号只跟持久轨迹/邻居投票，深度
        锚不参与——深度整道跳变时两根线会整齐地支持错误网格，此时再让锚点
        改判等于把错网格写成权威。线未锁定（冷启/重锁）：深度新鲜拍是重锚
        来源——新线直接初始化 k；老线与锚点冲突时，多条线同拍支持新网格
        （``strong_anchor``）就立即改 k，否则连续 ``ANCHOR_CONFIRM`` 拍同向
        一致才改，未确认前本拍横位用深度兜底。深度空窗时保留持久 k（线随
        画面移动继续给绝对横位），新线由所有在期线的整数间距票一致后补位；
        票分歧不硬定号。没通过门的新线不留 k，被拒线也不更新已有关联轨迹
        的身份，避免野线把错道号传给下一帧。
        """
        L = float(c["L"])
        tk = c.get("_track")
        if line_locked:
            if tk is not None and tk.k is not None:
                c["k"] = tk.k
                return True
            k = self._neighbor_k(c, fid)
            if k is None:
                return False
            c["k"] = k
            return True
        if x_anchor is not None:
            k_a = int(round(x_anchor + L))
            resid = x_anchor + L - k_a
            if tk is None or tk.k is None:
                if abs(resid) > self.RESID_MAX:
                    c["reject"] = "resid"
                    self.grid_reject += 1
                    return False
                c["k"] = k_a
                return True
            if k_a == tk.k:
                tk.k_pending = None
                tk.k_pending_n = 0
                c["k"] = tk.k
                return True
            if tk.k_pending == k_a:
                tk.k_pending_n += 1
            else:
                tk.k_pending, tk.k_pending_n = k_a, 1
            if tk.k_pending_n >= self.ANCHOR_CONFIRM or strong_anchor:
                # 多线同拍支持这个锚点：网格已被线自身证实，不必再等第二拍；
                # 单线锚点仍等连续确认，防一拍深度噪声翻道。
                tk.k = k_a
                tk.k_pending = None
                tk.k_pending_n = 0
                c["k"] = k_a
                return True
            c["k"] = tk.k
            c["reanchor"] = True
            return False
        if tk is not None and tk.k is not None:
            c["k"] = tk.k
            return True
        k = self._neighbor_k(c, fid)
        if k is None:
            return False
        c["k"] = k
        return True

    def update(self, lines: list[dict], fid: int, dt: float,
               v_lat: float = 0.0, fid_jump: bool = False,
               anchor: float | None = None) -> dict:
        # 线锁：上一拍起线已连续稳定 LINE_LOCK_TICKS 拍 → 本拍道号为权威，
        # 深度锚只能用于「线丢失后的重建」，不能改判/否决。
        line_locked = self.state == "TRACK" and self.locked >= LINE_LOCK_TICKS
        # ① 关联：颜色只作软提示，横位 L 才是身份
        cur = []
        for d in lines:
            c = dict(d)
            for key in ("k", "resid", "accepted", "reject", "_init_k",
                        "_xi", "reanchor"):
                c.pop(key, None)
            c["k"], c["resid"], c["accepted"] = None, None, False
            cur.append(c)
        for c in cur:
            best, bd = None, 1e9
            for t in self.tracks:
                gap = fid - t.idx
                if gap > TRACK_GAP:
                    continue
                pred = t.L + t.v * gap
                d = abs(c["L"] - pred)
                if (t.color != "?" and c.get("color") != "?"
                        and t.color != c.get("color")):
                    d += 0.10          # 换色不拆线：只加距离惩罚
                if d < TRACK_TOL + 0.05 * min(gap, TRACK_GAP) and d < bd:
                    best, bd = t, d
            if best is not None:
                gap = max(1, fid - best.idx)
                dv = (c["L"] - best.L) / gap
                c["v"] = float(np.clip(
                    0.7 * best.v + 0.3 * np.clip(dv, -0.15, 0.15),
                    -0.15, 0.15))
                c["seen"] = best.seen + 1
                c["_track"] = best
            else:
                c["v"], c["seen"] = 0.0, 0

        # ② 锚点：只认新鲜深度；空窗靠持久 k 的线继续给绝对横位
        x_anchor = None
        if anchor is not None and math.isfinite(anchor) \
                and abs(anchor) <= self.OFF_MAX:
            x_anchor = float(anchor)
        self.anchor_src = ("depth" if x_anchor is not None
                           else ("lines" if self.x_last is not None
                                 else "none"))
        if x_anchor is None and self.x_last is None:
            self.lost += 1
            self.locked = 0
            self.state = "RELOCK"
            self._commit_tracks(cur, fid)
            return self._out(None, 0, cur, self.anchor_src,
                             relock_src=None, pred=False)

        # ③ 颜色/侧别质量门 + 持久道号分配/重锚
        strong_anchor = False
        if not line_locked and x_anchor is not None and self.x_last is not None \
                and abs(x_anchor - self.x_last) <= self.ANCHOR_AGREE_TOL:
            # 只有离当前横位不远的锚点才享受当拍重锚：整道级深度跳变时
            # 两根线会整齐地支持错误网格（残差照样很小），必须仍走两拍确认。
            ks = set()
            for c in cur:
                if self._side_bad(c):
                    continue
                L = float(c["L"])
                k_a = int(round(x_anchor + L))
                if abs(x_anchor + L - k_a) <= self.STRONG_RESID:
                    ks.add(k_a)
            strong_anchor = len(ks) >= 2
        cand = []
        for c in cur:
            L = float(c["L"])
            if self._side_bad(c):
                c["reject"] = "side"
                self.side_reject += 1
                continue
            if not self._assign_k(c, x_anchor, fid, strong_anchor,
                                  line_locked):
                continue
            xi = float(c["k"]) - L
            c["_xi"] = xi
            if not line_locked and x_anchor is not None \
                    and abs(xi - x_anchor) > self.ANCHOR_AGREE_TOL:
                # 持久 k 与新鲜深度冲突：本拍用深度兜底，线不参选
                c["reject"] = "anchor"
                self.grid_reject += 1
                continue
            cand.append(c)

        # ④ 同 k 重复：留点数多的（等间距下同一道不应有两条线）
        byk: dict[int, dict] = {}
        for c in cand:
            k = c["k"]
            if k not in byk:
                byk[k] = c
            elif int(c.get("n") or 0) > int(byk[k].get("n") or 0):
                byk[k]["reject"] = "dup"
                self.grid_reject += 1
                byk[k] = c
            else:
                c["reject"] = "dup"
                self.grid_reject += 1
        keep = list(byk.values())

        # ⑤ 离群：相对横位中值偏超 OUTLIER_TOL 的线不参选
        if len(keep) >= 2:
            med = float(np.median([c["_xi"] for c in keep]))
            inl = [c for c in keep if abs(c["_xi"] - med)
                   <= self.OUTLIER_TOL]
            for c in keep:
                if c not in inl:
                    c["reject"] = "outlier"
                    self.grid_reject += 1
            keep = inl
            if keep and max(c["_xi"] for c in keep) \
                    - min(c["_xi"] for c in keep) > self.SPREAD_MAX:
                best = min(keep, key=lambda c: abs(c["_xi"] - med))
                for c in keep:
                    if c is not best:
                        c["reject"] = "spread"
                        self.grid_reject += 1
                keep = [best]

        # ⑥ 输出：有可用线=线横位；无可用线但深度新鲜=跟深度；否则滑行
        if keep:
            for c in keep:
                c["accepted"] = True
            x_out = float(np.median([c["_xi"] for c in keep]))
            # 稳定性计数：只有横位连续（单拍不跳 >LINE_JUMP_RESET）才累加；
            # 跳变=连续性中断，从 1 重新数（线锁的要件）。
            step = (abs(x_out - self.x_last)
                    if self.x_last is not None else 0.0)
            self.locked = (self.locked + 1
                           if step <= LINE_JUMP_RESET else 1)
            if self.x_prev is not None and dt > 1e-3:
                dvx = float(np.clip((x_out - self.x_last) / dt, -1.5, 1.5))
                self.x_vel = 0.6 * self.x_vel + 0.4 * dvx
            self.x_prev, self.x_last = self.x_last, x_out
            self.lost = 0
            self.state = "TRACK"
            ths = [(float(c["theta"]), max(1, int(c.get("n") or 1)))
                   for c in keep if c.get("theta") is not None]
            if ths:
                w = sum(w for _, w in ths)
                self.last_theta = sum(t * w for t, w in ths) / w
            relock_src = "line"
            pred = False
        elif x_anchor is not None:
            if self.x_prev is not None and self.x_last is not None \
                    and dt > 1e-3:
                dvx = float(np.clip((x_anchor - self.x_last) / dt,
                                    -1.5, 1.5))
                self.x_vel = 0.6 * self.x_vel + 0.4 * dvx
            self.x_prev, self.x_last = self.x_last, x_anchor
            self.lost += 1
            self.locked = 0          # 绝对位换了来源：线锁作废，重新数
            self.state = ("RELOCK" if (fid_jump
                                       or self.lost > COAST_MAX_TICKS)
                          else "COAST")
            x_out = x_anchor
            relock_src = None
            pred = False
        else:
            self.lost += 1
            self.locked = 0
            # 盲窗封顶（2026-10-31）：线不可用又没有深度锚时，横位只剩「上次
            # 横速」外推。短断链（≤COAST_MAX_TICKS）这么走是对的；再长就没有
            # 可积的运动证据了——旧行为会以恒速一路外推到整道外（实测两秒
            # 飘 2 道后随新鲜证据整道跳回）。超过即冻住：位置保持、状态标
            # RELOCK，让上游知道这是外推值而不是测量值。
            if self.lost <= COAST_MAX_TICKS:
                vp = 0.5 * self.x_vel + 0.5 * float(v_lat)
                self.x_last = (self.x_last + float(np.clip(vp, -1.5, 1.5))
                               * dt)
                self.state = ("RELOCK" if fid_jump else "COAST")
            else:
                self.state = "RELOCK"
            x_out = self.x_last
            relock_src = None
            pred = True
        # 线残差统一按最终横位记（accepted 线应接近 0；被拒线留偏差可审计）
        for c in cur:
            xi = c.get("_xi")
            if xi is not None:
                c["resid"] = round(float(xi) - x_out, 3)
        self._commit_tracks(cur, fid)
        return self._out(x_out, len(keep), cur, self.anchor_src,
                         relock_src=relock_src, pred=pred)

    def _commit_tracks(self, cur: list[dict], fid: int) -> None:
        for c in cur:
            c.pop("_init_k", None)
            c.pop("_xi", None)
            tk = c.pop("_track", None)
            if tk is not None:
                # 没通过门的线不更新身份：野线被关联到真线上时若照写 L/k，
                # 下一帧真线会接上这条被带偏的轨迹，继承错道号。
                if c.get("accepted"):
                    tk.L, tk.color, tk.idx = c["L"], c.get("color", "?"), fid
                    tk.seen, tk.v = c["seen"], c["v"]
                    tk.k, tk.resid = c.get("k"), c.get("resid")
            else:
                # 新线本拍没通过门（离群/跨度/残差）就不留道号：它可能是一条
                # 野线，下一帧真线接上这条轨迹时会继承错 k，连带整对线偏一道。
                k = c.get("k") if c.get("accepted") else None
                self.tracks.append(_Track(
                    c["L"], c.get("color", "?"), fid, c["v"], 0,
                    k, c.get("resid")))
        self.tracks = [t for t in self.tracks if fid - t.idx <= TRACK_GAP]

    def _out(self, x, n_locked, lines, anchor_src,
             relock_src, pred) -> dict:
        if x is not None:
            x = round(float(x), 3)
        self.last_lines = [dict(c) for c in lines]
        # 位置原点可用性（严格）：线连续锁定 + 至少两条高质量线 + 左右两侧
        # 都在 + 线角不过分。上游据此把线当绝对位置；不满足时深度才重建。
        good = [c for c in lines if c.get("accepted")
                and (c.get("rms") is None
                     or float(c["rms"]) <= LINE_RMS_MAX)
                and abs(float(c.get("theta") or 0.0)) <= LINE_THETA_MAX]
        sides = {c.get("side") for c in good}
        usable = (self.state == "TRACK" and self.locked >= LINE_LOCK_TICKS
                  and len(good) >= 2 and sides == {-1, 1})
        return {"state": self.state, "x": x, "theta": self.last_theta,
                "n_locked": int(n_locked), "relock_src": relock_src,
                "n_lines": len(lines), "grid_reject": self.grid_reject,
                "side_reject": self.side_reject, "lost": int(self.lost),
                "anchor_src": anchor_src, "lines": lines, "pred": pred,
                "usable": bool(usable), "locked": int(self.locked)}

    def predict(self, dt: float, v_lat: float = 0.0) -> dict:
        """非检测帧：只推进横位；上一帧线保留但标 pred，不再参与绝对位。"""
        if self.x_last is not None:
            self.lost += 1
            # 与 update 的盲窗同一口径：超过 COAST_MAX_TICKS 的外推不再积分
            if self.lost <= COAST_MAX_TICKS:
                vp = 0.5 * self.x_vel + 0.5 * float(v_lat)
                self.x_last += float(np.clip(vp, -1.5, 1.5)) * dt
            self.state = "COAST"
            return {"state": self.state, "x": round(self.x_last, 3),
                    "theta": self.last_theta, "n_locked": 0,
                    "relock_src": None, "n_lines": 0,
                    "grid_reject": self.grid_reject,
                    "side_reject": self.side_reject,
                    "lost": int(self.lost),
                    "anchor_src": self.anchor_src,
                    "usable": False, "locked": int(self.locked),
                    "lines": [dict(c) for c in self.last_lines], "pred": True}
        return {"state": "RELOCK", "x": None, "theta": self.last_theta,
                "n_locked": 0, "relock_src": None, "n_lines": 0,
                "grid_reject": self.grid_reject,
                "side_reject": self.side_reject, "lost": int(self.lost),
                "anchor_src": "none", "usable": False,
                "locked": int(self.locked),
                "lines": [], "pred": True}


class LaneObsWorker:
    """后台线观测线程；`latest()` 只读，失败静默返回 None。"""

    def __init__(self, cfg: LaneObsConfig) -> None:
        self.cfg = cfg
        self._q: queue.Queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._last_det_t: float | None = None
        self._last_det_fid: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sess = None
        self._tracker = LineTracker()
        self._last_fid = -1
        self._last_t = 0.0
        self._n = 0
        self._use_dml = False
        self.busy_skips = 0
        self.hold_ms = 0.0
        self.cam_h_fb = 0
        # 节拍记账：真实检测频率=det_n/时长，不受推送节流口径误导
        self.det_n = 0
        self.pred_n = 0
        self.push_n = 0
        self.drop_n = 0
        self.error: str | None = None
        self.note: str | None = None

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        weights = Path(self.cfg.weights)
        if not weights.is_absolute():
            from maaracing_master.core.paths import data_dir
            weights = data_dir() / weights
        if not weights.is_file():
            self.error = f"权重缺失：{weights}"
            return
        try:
            import onnxruntime as ort
            avail = set(ort.get_available_providers())
            if self.cfg.provider == "dml":
                if "DmlExecutionProvider" in avail:
                    providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
                else:
                    providers = ["CPUExecutionProvider"]
                    self.note = "DML 不可用，影子回退 CPU"
            elif self.cfg.provider == "cpu":
                providers = ["CPUExecutionProvider"]
            else:
                self.error = f"未知 provider：{self.cfg.provider}"
                return
            so = ort.SessionOptions()
            so.intra_op_num_threads = int(self.cfg.threads)
            so.inter_op_num_threads = 1
            self._sess = ort.InferenceSession(
                str(weights), sess_options=so, providers=providers)
            self._use_dml = providers[0] == "DmlExecutionProvider"
        except Exception as exc:  # noqa: BLE001 —— 影子故障不碰控制
            self.error = repr(exc)
            return
        self._thread = threading.Thread(target=self._loop, name="lane-obs",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def push(self, rgb: np.ndarray, fid: int, cam_h: float | None,
             v_lat: float, dt: float, fid_jump: bool,
             anchor: float | None = None) -> None:
        """入队一帧；``anchor`` 是同帧新鲜深度路心（None=复用/缺供）。

        锚点随帧一起入队，保证线观测用的绝对参照与这条帧同拍——不拿后一帧
        的深度去锚前一帧的线。"""
        if self._sess is None:
            return
        now = time.perf_counter()
        if self.cfg.hz > 0 and now - self._last_t < 1.0 / self.cfg.hz:
            return
        self._last_t = now
        self.push_n += 1
        used_h, fb = _lane_cam_h(cam_h)
        if fb:
            self.cam_h_fb += 1
        try:
            self._q.get_nowait()
            self.drop_n += 1
        except queue.Empty:
            pass
        try:
            self._q.put_nowait((rgb, int(fid), used_h,
                                float(v_lat), float(dt), bool(fid_jump),
                                None if anchor is None else float(anchor)))
        except queue.Full:
            pass

    def latest(self) -> dict | None:
        """最新一帧的完整快照（横位/状态/线/锚点同源）。

        2026-10-24 重写后不再把「最新预测横位」和「上一次检测的线」拼在一起：
        线和横位必须来自同一拍，否则调试图会画出与横位不对应的线，离线也
        无法判断线锚是否算错。检测龄 ``det_age_ms`` 单列最近一次真实检测的
        年龄；``pred=true`` 表示这一拍横位来自滑行预测（无可用线或跳帧），
        不是线观测结果。"""
        with self._lock:
            if self._latest is None:
                return None
            out = dict(self._latest)
        t_done = out.pop("t_done", None)
        t_det = out.pop("t_det", None)
        out["det_fid"] = self._last_det_fid
        out["det_age_ms"] = (None if t_det is None
                             else round((time.perf_counter() - t_det) * 1000.0,
                                        1))
        out["age_ms"] = (0.0 if t_done is None
                         else round((time.perf_counter() - t_done) * 1000.0, 1))
        out["provider"] = "dml" if self._use_dml else "cpu"
        out["busy_skips"] = self.busy_skips
        out["hold_ms"] = self.hold_ms
        out["det_n"] = self.det_n
        out["pred_n"] = self.pred_n
        out["push_n"] = self.push_n
        out["drop_n"] = self.drop_n
        out["cam_h_fb"] = self.cam_h_fb
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rgb, fid, cam_h, v_lat, dt, jump, anchor = \
                    self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            beat()          # 心跳：性能面板的「每秒几拍」由 core 差分得出
            t0 = time.perf_counter()
            try:
                self._n += 1
                every = max(1, int(self.cfg.detect_every))
                pred = every > 1 and bool(self._n % every)
                if pred:
                    self.pred_n += 1
                    res = self._tracker.predict(dt, v_lat)
                else:
                    self.det_n += 1
                    rec = self._infer(rgb, cam_h)
                    if rec is None:      # DML 被占：本帧让路，下一帧再来
                        continue
                    res = self._tracker.update(rec["lines"], fid, dt, v_lat,
                                               jump, anchor)
                t_done = time.perf_counter()
                if not pred:
                    self._last_det_t = t_done
                    self._last_det_fid = int(fid)
                out = self._snapshot(res, fid, cam_h, t0, t_done, pred)
                with self._lock:
                    self._latest = out
            except Exception as exc:  # noqa: BLE001 —— 单帧失败只记错误
                self.error = repr(exc)

    def _snapshot(self, res: dict, fid: int, cam_h: float, t0: float,
                  t_done: float, pred_fallback: bool) -> dict:
        """tracker 输出 → 线程对外快照。

        字段必须逐个显式搬运（不是 ``dict(res)``）：上游新增的位置原点判据
        （usable/locked）若在这里漏搬，消费方永远读到 None——线为主会静默
        失效且看不出错误。"""
        return {"fid": fid, "cam_h": round(cam_h, 3),
                "pred": bool(res.get("pred", pred_fallback)),
                "ms": round((t_done - t0) * 1000.0, 1),
                "n_lines": res["n_lines"],
                "x": None if res["x"] is None else round(res["x"], 3),
                "theta": None if res["theta"] is None
                else round(res["theta"], 4),
                "state": res["state"],
                "n_locked": res["n_locked"],
                "relock_src": res["relock_src"],
                "anchor_src": res.get("anchor_src"),
                "grid_reject": res["grid_reject"],
                "side_reject": res["side_reject"],
                "usable": bool(res.get("usable", False)),
                "locked": int(res.get("locked", 0)),
                "t_done": t_done, "t_det": self._last_det_t,
                # 线与横位同拍：annotated（k/resid/accepted）由 tracker 给出
                "lines": res["lines"]}

    def _run(self, rgb: np.ndarray):
        """DML 走共享锁：只包 run 本体；抢不到即弃帧（latest-only，不排队）。"""
        inp = self._sess.get_inputs()[0].name
        feed = {inp: _letterbox(rgb)}
        if not self._use_dml:
            return _prob_from_out(self._sess.run(None, feed))
        if not dml_lock.LOCK.acquire(blocking=False):
            self.busy_skips += 1
            return None
        try:
            t = time.perf_counter()
            out = self._sess.run(None, feed)
            self.hold_ms = round((time.perf_counter() - t) * 1000.0, 1)
        finally:
            dml_lock.LOCK.release()
        return _prob_from_out(out)

    def _infer(self, rgb: np.ndarray, cam_h: float) -> dict | None:
        prob = self._run(rgb)
        if prob is None:
            return None
        import cv2
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)   # 每帧一次，不按线重复
        lines = []
        for pts in _build_tracks(_row_segments(prob)):
            yf, wf = _color(hsv, pts)
            if max(yf, wf) < WHITE_MIN:
                continue
            color = "y" if yf >= YELLOW_MIN else ("w" if wf >= WHITE_MIN else "?")
            near = _fit(pts, cam_h, *NEAR_Y)
            far = _fit(pts, cam_h, *FAR_Y)
            f = near or far
            if f is None:
                continue
            x_near = _near_x(pts)
            lines.append({"L": round(f["L"], 3), "theta": round(f["theta"], 4),
                          "n": f["n"], "rms": round(f["rms"], 2),
                          "y0": int(f["y0"]), "y1": int(f["y1"]),
                          "color": color, "yellow": round(yf, 2),
                          "white": round(wf, 2),
                          "x_near": None if x_near is None
                          else round(x_near, 1),
                          "side": _side_from_x(x_near)})
        return {"lines": lines}
