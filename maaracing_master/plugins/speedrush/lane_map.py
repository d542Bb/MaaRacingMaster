# -*- coding: utf-8 -*-
"""持久车道图：自车弧长索引的车道中心/边界记忆（2026-11-01，第 2 期）。

**它是什么**：把每拍走廊带投影到一张固定弧长网格上，随自车前进/横移预测，
随新走廊带校正；每个节点带 `conf/age/src`。它是走廊的**时间维**——走廊回答
「这一拍看到什么」，本图回答「上一拍看到的路，现在还在不在、错多少」。

**本阶段只观测**：图每拍推进、落 trace `map` 与预测误差样本，不接控制、
不接安全、不改路线/预置。视界门（4~6 秒只在误差达标段启用）等误差曲线出来
再定（方案第 2 阶段的验收闸）。

**坐标**：与走廊带同帧（自车系米，右正）；节点弧长 s 从 0 到 `MAX_S_M`。
前进按 `v_mps×dt` 把内容向自车平移，横移按 `v_lat×lane_w×dt` 反向平移
（自车右移 → 同一路点在自车系左移）。

**来源与置信**：车道级带（measured/mixed）直接采纳（quality=`band.conf` 与
龄衰减）；粗带（inferred）只填没有更好证据的节点，置信封顶 0.3。无新证据的
节点按时间衰减，超龄退役。

**误差样本**：校正**之前**，用新鲜实测带的中点与图上对应节点比，得到
「预测距离 → 误差」。这是第 2 阶段的地图支撑曲线，离线聚合即可。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MAX_S_M = 80.0
STEP_M = 2.0
CONF_TAU_S = 1.0          # 无新证据时置信 e 折时间
CONF_FLOOR = 0.05
COARSE_CONF = 0.3         # 粗带作方向证据的置信上限
AGE_DECAY_MS = 500.0      # 测量龄对 quality 的折扣时间常数
MAX_AGE_MS = 2500.0       # 节点最长寿命（无证据）
MAX_DT_S = 0.2            # 慢拍保护：超出按 0.2 s 计，避免一次跳几十米

_TRACE_S = (10.0, 20.0, 30.0, 50.0, 70.0)


@dataclass(frozen=True)
class MapNode:
    s: float
    center: float | None = None
    left: float | None = None
    right: float | None = None
    conf: float = 0.0
    age_ms: float = math.inf
    src: str = "none"

    @property
    def empty(self) -> bool:
        return self.center is None and self.left is None and self.right is None


def _mid(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return 0.5 * (left + right)


class LaneMap:
    """固定弧长网格；重建成本 O(N)，N≈41。"""

    def __init__(self, lane_w_m: float, *, max_s_m: float = MAX_S_M,
                 step_m: float = STEP_M) -> None:
        if lane_w_m <= 0 or max_s_m <= 0 or step_m <= 0:
            raise ValueError("车道图参数非法")
        self.lane_w_m = float(lane_w_m)
        self.step_m = float(step_m)
        self.max_s_m = float(max_s_m)
        self._ss = [i * step_m for i in range(int(max_s_m / step_m) + 1)]
        self.reset()

    def reset(self) -> None:
        self._nodes: list[MapNode] = [MapNode(s) for s in self._ss]

    # ---------- 采样与查询 ----------

    def _index(self, s: float) -> tuple[int, int, float]:
        s = min(max(s, 0.0), self.max_s_m)
        pos = s / self.step_m
        i0 = min(int(pos), len(self._ss) - 1)
        i1 = min(i0 + 1, len(self._ss) - 1)
        return i0, i1, pos - i0

    def at(self, s: float) -> MapNode | None:
        i0, i1, w = self._index(s)
        a, b = self._nodes[i0], self._nodes[i1]
        if a.empty and b.empty:
            return None

        def blend(x, y):
            if x is None:
                return y
            if y is None:
                return x
            return x + (y - x) * w

        return MapNode(
            s=s, center=blend(a.center, b.center),
            left=blend(a.left, b.left), right=blend(a.right, b.right),
            conf=a.conf + (b.conf - a.conf) * w,
            age_ms=min(a.age_ms, b.age_ms),
            src=a.src if a.conf >= b.conf else b.src)

    def support(self, conf_min: float = 0.4) -> float:
        """最远的**车道级**可信节点（米）；粗带不算支撑，0=无支撑。

        支撑是「这条路还能信多远」的保守读数：粗带只给方向，不能当车道
        中心证据；中间被粗带填过的节点不改变这个距离。"""
        last = 0.0
        for n in self._nodes:
            if n.src != "inferred" and n.conf >= conf_min and not n.empty:
                last = n.s
        return last

    # ---------- 推进 ----------

    def update(self, *, bands=(), coarse_bands=(), v_mps: float | None = 0.0,
               v_lat_lane_s: float = 0.0, dt_s: float = 0.0) -> dict:
        """推进一拍；返回本拍误差样本（校正前预测 vs 新鲜实测）。"""
        dt = max(0.0, min(float(dt_s or 0.0), MAX_DT_S))
        ds = max(0.0, float(v_mps or 0.0)) * dt
        dx = -float(v_lat_lane_s or 0.0) * self.lane_w_m * dt
        prev = self._nodes
        moved = [self._shifted(prev, s + ds, s, dx) for s in self._ss]
        decay = math.exp(-dt / CONF_TAU_S)
        aged = [
            MapNode(n.s, n.center, n.left, n.right, n.conf * decay,
                    n.age_ms + dt * 1000.0, n.src)
            for n in moved]
        errs = self._errors(aged, bands)
        self._nodes = self._correct(aged, bands, coarse_bands)
        return {"err": errs, "sup": round(self.support(), 1),
                "n": sum(1 for n in self._nodes if not n.empty)}

    def _shifted(self, prev: list[MapNode], s_src: float, s_dst: float,
                 dx: float) -> MapNode:
        s = s_src
        if s > self.max_s_m:
            return MapNode(s_dst)
        pos = min(max(s, 0.0), self.max_s_m) / self.step_m
        i0 = min(int(pos), len(prev) - 1)
        i1 = min(i0 + 1, len(prev) - 1)
        w = pos - i0
        a, b = prev[i0], prev[i1]

        def blend(x, y):
            if x is None:
                return y
            if y is None:
                return x
            return x + (y - x) * w

        node = MapNode(
            s=s_dst, center=blend(a.center, b.center),
            left=blend(a.left, b.left), right=blend(a.right, b.right),
            conf=a.conf + (b.conf - a.conf) * w,
            age_ms=min(a.age_ms, b.age_ms),
            src=a.src if a.conf >= b.conf else b.src)
        if node.empty:
            return node
        return MapNode(
            s=node.s,
            center=None if node.center is None else node.center + dx,
            left=None if node.left is None else node.left + dx,
            right=None if node.right is None else node.right + dx,
            conf=node.conf, age_ms=node.age_ms, src=node.src)

    def _errors(self, nodes: list[MapNode], bands) -> list:
        """新鲜实测带 vs 校正前的图：[[弧长, 误差(m), 图置信, 来源], ...]。"""
        out = []
        for b in bands:
            if b.left is None or b.right is None or b.src == "inferred":
                continue
            if b.age_ms is not None and b.age_ms > 100.0:
                continue
            z = 0.5 * (b.z0 + b.z1)
            i0, i1, w = self._index(z)
            a, bnode = nodes[i0], nodes[i1]
            if a.center is None and bnode.center is None:
                continue
            g = self._interp(a, bnode, w)
            if g is None:
                continue
            ref = 0.5 * (b.left + b.right)
            out.append([round(z, 1), round(g.center - ref, 3),
                        round(g.conf, 2), g.src])
        return out

    @staticmethod
    def _interp(a: MapNode, b: MapNode, w: float) -> MapNode | None:
        if a.center is None and b.center is None:
            return None
        if a.center is None:
            return b
        if b.center is None:
            return a
        return MapNode(
            s=a.s, center=a.center + (b.center - a.center) * w,
            left=None, right=None,
            conf=a.conf + (b.conf - a.conf) * w,
            age_ms=min(a.age_ms, b.age_ms),
            src=a.src if a.conf >= b.conf else b.src)

    def _correct(self, nodes: list[MapNode], bands, coarse_bands
                 ) -> list[MapNode]:
        out = list(nodes)
        # 先粗带（弱证据），后实测带（强证据覆盖）
        for b in coarse_bands:
            self._apply(out, b, coarse=True)
        for b in bands:
            if b.left is None and b.right is None:
                continue
            self._apply(out, b, coarse=(b.src == "inferred"))
        for i, n in enumerate(out):
            if n.empty:
                continue
            if n.conf < CONF_FLOOR or n.age_ms > MAX_AGE_MS:
                out[i] = MapNode(n.s)
        return out

    def _apply(self, nodes: list[MapNode], band, *, coarse: bool) -> None:
        z0 = max(0.0, band.z0)
        z1 = min(self.max_s_m, band.z1)
        if z1 < z0:
            return
        center = _mid(band.left, band.right)
        if center is None:
            return
        q = COARSE_CONF if coarse else max(
            float(getattr(band, "conf", 0.0) or 0.0), 0.5)
        age = float(getattr(band, "age_ms", 0.0) or 0.0)
        q *= math.exp(-age / AGE_DECAY_MS)
        if q <= 0.0:
            return
        # 只校正**落在带内**的节点（索引采样会把带外相邻节点一起算进来）
        i0 = max(0, int(math.ceil(z0 / self.step_m - 1e-9)))
        i1 = min(len(nodes) - 1, int(math.floor(z1 / self.step_m + 1e-9)))
        if i1 < i0:
            i0 = i1 = min(range(len(nodes)),
                          key=lambda i: abs(nodes[i].s - 0.5 * (z0 + z1)))
        for i in range(i0, i1 + 1):
            n = nodes[i]
            if coarse and n.conf > q and n.src != "inferred":
                continue                # 粗证据不覆盖更好的实测节点
            if coarse:
                nodes[i] = MapNode(
                    n.s, center if center is not None else n.center,
                    n.left, n.right, min(q, COARSE_CONF), age, "inferred")
            else:
                nodes[i] = MapNode(
                    n.s, center, band.left, band.right, q, age,
                    str(band.src))

    # ---------- trace ----------

    def as_trace(self, err: dict | None = None) -> dict:
        pts = []
        for s in _TRACE_S:
            n = self.at(s)
            if n is None:
                pts.append([s, None, 0.0, None, "none"])
                continue
            pts.append([s,
                        None if n.center is None else round(n.center, 3),
                        round(n.conf, 2),
                        None if not math.isfinite(n.age_ms)
                        else round(n.age_ms, 1),
                        n.src])
        return {"sup": round(self.support(), 1), "pts": pts,
                "err": None if err is None else err}
