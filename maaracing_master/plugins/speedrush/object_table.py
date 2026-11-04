# -*- coding: utf-8 -*-
"""动态对象表：按身份键合并跟踪轨道、车流视图、米制状态与币组账。

**它是什么**：每拍把同一份世界观测投影成一张按身份索引的表，并给每条轨道
算一个 `safety_ok`。表自己保留已退出跟踪宽限的轨道（短于计划视界），供后续
规划价值项使用；安全消费点只读 `safety_ok` 子集。

**身份规则**（方案 A.1，先于实现定死）：
- 动态对象键 `(kind, track_id)`。track id 由 Tracker 统一分配：跨 kind 唯一、
  全局单调、永不复用；阶段切换清空轨道但不回卷计数。
- 金币组键 `("cg", group_id)`，与轨道键分开存放；组只引用成员轨道 id
  （`member_ids/lead_id`），不替代成员做位置/速度外推。
- 只做同 id 续命。**不做跨 id 重识别**：同位置出现新 id 就是新对象，
  旧条目按龄退役；两者不合并、不继承年龄或置信。

**safety_ok 的口径**：安全对象只有车；同时满足
1. 横位所用测量的年龄 ≤ `safety_age_s`（调用方传 motion.max_obj_age_s；
   米制车按锚龄、像素车按观测龄——与场景现有硬删同源）；
2. 有可用横位（像素横位或米制横位）。
「新鲜」是**在安全龄内**，不是本拍必须见到：短暂遮挡（跟踪宽限内）仍是
一条实测过、位置未外推的对象，今天的安全链就用它。上游不再输出（跟踪器
已退役）的条目保留在表里供价值项，但 `safety_ok=False`；帧率未预热算不出
龄时直接退役，不带未知年龄进规划。

**不是什么**：本模块不选道、不判碰撞、不做外推轨迹；坐标与单位照搬来源
（`x_lane` 道、`x_m/z_m` 自车系米），由消费端换算。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from maaracing_master.plugins.speedrush.tracking import DistanceSource
from maaracing_master.plugins.speedrush.world_model import KIND_CAR

SAFETY_OK = "ok"
SAFETY_NOT_CAR = "not_car"
SAFETY_STALE = "stale"
SAFETY_NO_LATERAL = "no_lateral"


@dataclass(frozen=True)
class TrackObj:
    """一条跟踪轨道的当拍投影（含超龄保留的状态）。"""

    kind: str
    id: int
    x_lane: float | None       # 像素横向（道，右正）；None=域外/无尺子
    x_m: float | None          # 米制横向（自车系米，右正）；None=无米制解
    cy: int
    z_m: float | None          # 米制纵深（自车系米）；None=无锚
    rel_approach: float        # px/fid，正=接近（单位陷阱见 tracking）
    x_sigma: float             # 像素横向不确定度（跟踪层模型）
    half_w_m: float | None     # 车半宽（米，来自框宽投票；None=标称）
    conf: float
    src: str                   # metric / pixel / far
    first_seen_fid: int
    last_seen_fid: int
    age_ms: float              # 观测新鲜度（距最后一次真实读数）
    src_age_ms: float          # 横位所用测量的年龄（米制=锚龄，像素=观测龄）
    seen: bool                 # 本拍是否真实见到（False=宽限存续）
    safety_ok: bool
    safety_why: str
    v_close_mps: float | None = None   # 锚点链闭速（m/s，正=接近）
    v_lat_lane_s: float | None = None  # 车流视图横速（道/s，右正）
    # 车道线插值成员（车道线定道，闸关恒 None；None=闸关/弃权）
    x_lane_line: float | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.id)

    @property
    def x_usable(self) -> bool:
        return self.x_lane is not None or self.x_m is not None


@dataclass(frozen=True)
class GroupObj:
    """一个金币组的当拍投影；成员是 coin 轨道 id，组号不在轨道编号空间里。"""

    group_id: int
    x_center: float
    cy_min: int
    cy_max: int
    conf_min: float
    observed_count: int
    member_ids: tuple[int, ...]
    lead_id: int
    first_seen_fid: int
    last_seen_fid: int
    age_ms: float
    seen: bool


def _age_ms(fid: int, last_fid: int, fps: float | None) -> float:
    """帧号差 → 毫秒；帧率未预热时=0（与 decision 的年龄口径一致，不臆造）。"""
    if not fps or fps <= 0:
        return 0.0
    return max(0, fid - last_fid) / fps * 1000.0


def _safety(kind: str, src_age_ms: float, x_lane: float | None,
            x_m: float | None, safety_age_ms: float) -> tuple[bool, str]:
    """安全门与场景现有硬删同源：按**横位所用测量**的年龄，不是观测龄。

    米制车的横位/纵深来自锚点，场景一直按锚龄 0.6 s 截断；像素车按观测龄。
    两套口径混用会让同一辆车在表里安全、在 scene 里已被删。"""
    if kind != KIND_CAR:
        return False, SAFETY_NOT_CAR
    if src_age_ms > safety_age_ms:
        return False, SAFETY_STALE
    if x_lane is None and x_m is None:
        return False, SAFETY_NO_LATERAL
    return True, SAFETY_OK


class ObjectTable:
    """按身份键维护的当拍对象表（有状态：超龄条目按 plan_age 保留）。"""

    def __init__(self, *, safety_age_s: float, plan_age_s: float) -> None:
        if safety_age_s <= 0:
            raise ValueError(f"safety_age_s 必须 > 0：{safety_age_s}")
        if plan_age_s < safety_age_s:
            raise ValueError(
                f"plan_age_s={plan_age_s} 必须 ≥ safety_age_s={safety_age_s}")
        self.safety_age_ms = safety_age_s * 1000.0
        self.plan_age_ms = plan_age_s * 1000.0
        self.reset()

    def reset(self) -> None:
        self._tracks: dict[tuple[str, int], TrackObj] = {}
        self._groups: dict[int, GroupObj] = {}

    def step(self, *, obs, views=(), metric_cars=(), fps=None) -> None:
        """吃一拍世界观测，更新表。调用方保证同拍只喂一次（WGC 重复帧由
        Tracker 幂等重发，本表重算结果不变）。"""
        fid = obs.frame_id
        metric_by_id = {m.target_id: m for m in metric_cars}
        view_by_id = {v.id: v for v in views}
        present: set[tuple[str, int]] = set()
        for t in (*obs.targets, *obs.far_targets):
            x_lane = getattr(t, "x_lane", None)
            metric = metric_by_id.get(t.id)
            view = view_by_id.get(t.id)
            x_m = None if metric is None else metric.x_m
            z_m = None if metric is None else metric.z_m
            age = _age_ms(fid, t.last_seen_fid, fps)
            if metric is not None and z_m is not None:
                if metric.source is DistanceSource.BOX_SIZE:
                    src = "metric_coarse"
                    src_age = 0.0           # 与 scene 的 BOX_SIZE 入场口径一致
                else:
                    src = "metric"
                    src_age = metric.anchor_age_ms or 0.0
            elif x_lane is not None:
                src = "pixel"
                src_age = age
            else:
                src = "far"
                src_age = age
            half_w = None
            if view is not None and view.w_m:
                half_w = view.w_m / 2.0
            seen = t.last_seen_fid == fid
            ok, why = _safety(t.kind, src_age, x_lane, x_m,
                              self.safety_age_ms)
            key = (t.kind, t.id)
            self._tracks[key] = TrackObj(
                kind=t.kind, id=t.id, x_lane=x_lane, x_m=x_m,
                cy=t.cy, z_m=z_m, rel_approach=t.rel_approach,
                v_close_mps=None if metric is None else metric.v_close_mps,
                v_lat_lane_s=None if view is None else view.v_lat,
                x_sigma=getattr(t, "x_sigma", 0.0),
                x_lane_line=getattr(t, "x_lane_line", None),
                half_w_m=half_w, conf=t.conf, src=src,
                first_seen_fid=t.first_seen_fid,
                last_seen_fid=t.last_seen_fid,
                age_ms=age, src_age_ms=src_age, seen=seen,
                safety_ok=ok, safety_why=why)
            present.add(key)
        # 已退出跟踪宽限：保留到计划视界，安全标记立即失效（不再更新位置）。
        # 帧率未预热时算不出龄，宁可立即退役也不带着未知年龄进规划。
        for key, e in list(self._tracks.items()):
            if key in present:
                continue
            age = _age_ms(fid, e.last_seen_fid, fps)
            if not fps or age > self.plan_age_ms:
                del self._tracks[key]
                continue
            why = SAFETY_NOT_CAR if e.kind != KIND_CAR else SAFETY_STALE
            self._tracks[key] = replace(e, age_ms=age, seen=False,
                                        safety_ok=False, safety_why=why)
        # 币组账由聚合器持有；本表只镜像当拍结果
        self._groups = {}
        for g in obs.coin_groups:
            self._groups[g.group_id] = GroupObj(
                group_id=g.group_id, x_center=g.x_center,
                cy_min=g.cy_min, cy_max=g.cy_max, conf_min=g.conf_min,
                observed_count=g.observed_count,
                member_ids=tuple(g.member_ids), lead_id=g.lead_id,
                first_seen_fid=g.first_seen_fid,
                last_seen_fid=g.last_seen_fid,
                age_ms=_age_ms(fid, g.lead_fid or g.last_seen_fid, fps),
                seen=g.last_seen_fid == fid)

    def track(self, kind: str, track_id: int) -> TrackObj | None:
        return self._tracks.get((kind, track_id))

    @property
    def tracks(self) -> tuple[TrackObj, ...]:
        return tuple(sorted(self._tracks.values(), key=lambda e: e.key))

    @property
    def groups(self) -> tuple[GroupObj, ...]:
        return tuple(sorted(self._groups.values(), key=lambda g: g.group_id))

    @property
    def safety_cars(self) -> tuple[TrackObj, ...]:
        """可进安全判断的车（安全消费点唯一入口）。"""
        return tuple(e for e in self.tracks
                     if e.kind == KIND_CAR and e.safety_ok)

    def as_trace(self) -> dict:
        """紧凑观测面：轨道逐条 13 元组，币组逐组 8 元组。"""
        return {
            "n": len(self._tracks),
            "nsafe": sum(1 for e in self._tracks.values()
                         if e.kind == KIND_CAR and e.safety_ok),
            "t": [[e.kind, e.id,
                   None if e.x_lane is None else round(e.x_lane, 3),
                   None if e.x_m is None else round(e.x_m, 3),
                   None if e.z_m is None else round(e.z_m, 2),
                   round(e.age_ms, 1), e.seen, e.safety_ok, e.safety_why,
                   e.src, round(e.src_age_ms, 1),
                   None if e.v_close_mps is None else round(e.v_close_mps, 2),
                   None if e.v_lat_lane_s is None else round(e.v_lat_lane_s, 3),
                   None if e.x_lane_line is None else round(e.x_lane_line, 3)]
                  for e in self.tracks],
            "g": [[g.group_id, round(g.x_center, 3), g.cy_max,
                   g.observed_count, round(g.age_ms, 1), g.seen,
                   list(g.member_ids), g.lead_id]
                  for g in self.groups],
        }
