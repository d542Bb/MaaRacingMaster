#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M0 · v2↔v3 真源等价校验（TREASURE_V2_V3_SINGLE_SOURCE_PLAN 的收口前置闸门）。

用途：在把鉴宝运行时的 5 处「仍读 treasure_rois.json(v2)」切到 treasure_assets.json(v3) 之前，
先逐字段证明 v3 已覆盖 v2 且值等价——否则「归档 v2」会丢字段/改行为。本脚本只读两份 JSON，
纯标准库，不 import cv2/numpy，不改任何文件。

比对范围 = 运行时从 v2 实际消费的字段（见 plan §二）：
  stage.*        → anchors.*        ：rect / templates / threshold
  actions.*      → anchors.*        ：rect / templates
  appraisers.*   → anchors.*        ：rect / templates / threshold / prio(↔order)
  ocr.*          → anchors.*(kind)  ：rect
  eggs.egg + _count_*_norm → anchors.egg + domain ：rect / threshold / 四个 _count_*_norm

跨段改名：v2 `stage.session_start_match_btn`（判定，带模板）与 v2 `actions.session_start_match_btn`
（点击区，空模板）在 v3 分家为 `session_start_match_btn` / `session_start_match_click`——本脚本按此映射比对。

退出码：0 全等价；1 存在缺失或字段漂移（阻塞 M1）；2 输入/环境错误。
用法：python tools/navkit/check_v2_v3_parity.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[2]
CONFIG = _PROJ / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "config"
V2_PATH = CONFIG / "treasure_rois.json"
V3_PATH = CONFIG / "treasure_assets.json"

# rect 归一化浮点逐位复制，容差给到 1e-9 仅吸收极端浮点噪声；阈值/模板必须精确。
RECT_TOL = 1e-9

# v2 actions.session_start_match_btn → v3 改名后的锚点名
RENAME = {("actions", "session_start_match_btn"): "session_start_match_click"}

# 已核准偏差（用户 2026-09-07 裁决 v3 为准）：v2 侧为校准前滞后值，M4 将整体归档 v2。
# 命中此表的差异记为「已核准」而非「漂移」，不阻塞 M1；但任何未登记的新差异仍照常拦截。
ALLOWED_DIVERGENCE = {
    ("actions", "session_master_badge"):
        "v3 已重校准 rect 并补登记模板 session_master_badge.png；v2 为滞后值，以 v3 为准",
}


def _close(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= RECT_TOL


def _rect_eq(v2rect, v3rect) -> bool:
    if not (isinstance(v2rect, list) and isinstance(v3rect, list)):
        return False
    if len(v2rect) != 4 or len(v3rect) != 4:
        return False
    return all(_close(x, y) for x, y in zip(v2rect, v3rect))


def _norm_templates(tpls):
    if not isinstance(tpls, list):
        return ()
    return tuple(t for t in tpls if isinstance(t, str))


class Parity:
    def __init__(self):
        self.ok_rows = 0
        self.issues: list[str] = []   # 漂移 / 缺失（阻塞）
        self.allowed: list[str] = []  # 已核准偏差（不阻塞，明列备查）

    def _v3_anchor(self, v3, name):
        return v3.get("anchors", {}).get(name)

    def _compare(self, v3, seg, key, entry, *, want_rect, want_tpl, want_th, want_prio=None):
        v3name = RENAME.get((seg, key), key)
        a = self._v3_anchor(v3, v3name)
        if a is None:
            self.issues.append(f"缺失 v3 锚点: [{seg}] {key} → anchors.{v3name}")
            return
        diffs = []
        if want_rect and not _rect_eq(entry.get("rect"), a.get("rect")):
            diffs.append(f"rect {entry.get('rect')}≠{a.get('rect')}")
        if want_tpl:
            t2 = _norm_templates(entry.get("templates"))
            t3 = _norm_templates(a.get("templates"))
            if set(t2) != set(t3):
                diffs.append(f"templates {sorted(t2)}≠{sorted(t3)}")
        if want_th:
            th2, th3 = entry.get("threshold"), a.get("threshold")
            if (th2 is None) != (th3 is None) or (th2 is not None and not _close(th2, th3)):
                diffs.append(f"threshold {th2}≠{th3}")
        if want_prio is not None:
            # v2 prio → v3 order（数值应一致；v3 无显式 prio 时以 order 承载优先级）
            prio2 = entry.get("prio")
            order3 = a.get("order")
            if prio2 is not None and order3 is not None and int(prio2) != int(order3):
                diffs.append(f"prio↔order {prio2}≠{order3}")
        if diffs:
            reason = ALLOWED_DIVERGENCE.get((seg, key))
            if reason:
                self.allowed.append(
                    f"已核准 [{seg}] {key}: {'; '.join(diffs)} —— {reason}"
                )
            else:
                self.issues.append(f"漂移 [{seg}] {key} → anchors.{v3name}: " + "; ".join(diffs))
        else:
            self.ok_rows += 1

    def run(self, v2: dict, v3: dict) -> int:
        for key, entry in (v2.get("stage") or {}).items():
            if isinstance(entry, dict):
                self._compare(v3, "stage", key, entry, want_rect=True,
                              want_tpl=True, want_th=True)
        for key, entry in (v2.get("actions") or {}).items():
            if isinstance(entry, dict):
                self._compare(v3, "actions", key, entry, want_rect=True,
                              want_tpl=True, want_th=False)
        for key, entry in (v2.get("appraisers") or {}).items():
            if isinstance(entry, dict) and not key.startswith("_"):
                self._compare(v3, "appraisers", key, entry, want_rect=True,
                              want_tpl=True, want_th=True, want_prio=True)
        for key, entry in (v2.get("ocr") or {}).items():
            if isinstance(entry, dict):
                self._compare(v3, "ocr", key, entry, want_rect=True,
                              want_tpl=False, want_th=False)

        eggs_v2 = v2.get("eggs") or {}
        egg = eggs_v2.get("egg")
        if isinstance(egg, dict):
            self._compare(v3, "eggs", "egg", egg, want_rect=True, want_tpl=True, want_th=True)
        # 彩蛋领域参数：v2 顶层 _count_*_norm → v3 anchors.egg.domain
        v3_egg = self._v3_anchor(v3, "egg") or {}
        dom = v3_egg.get("domain") or {}
        for fld in ("_count_dx_norm", "_count_dy_norm", "_count_w_norm", "_count_h_norm"):
            v2v, v3v = eggs_v2.get(fld), dom.get(fld)
            if v2v is not None and not _close(v2v, v3v):
                self.issues.append(f"漂移 eggs.{fld} 领域参数: {v2v}≠{v3v}")
            elif v2v is None and v3v is None:
                self.ok_rows += 1
            elif v3v is None:
                self.issues.append(f"缺失 eggs.{fld} 领域参数（v3 anchors.egg.domain 无此项）")
            else:
                self.ok_rows += 1
        return 0 if not self.issues else 1


def main() -> int:
    if not V2_PATH.is_file():
        print(f"[parity] 找不到 v2：{V2_PATH}", file=sys.stderr)
        return 2
    if not V3_PATH.is_file():
        print(f"[parity] 找不到 v3：{V3_PATH}", file=sys.stderr)
        return 2
    try:
        v2 = json.loads(V2_PATH.read_text(encoding="utf-8"))
        v3 = json.loads(V3_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[parity] JSON 解析失败：{exc}", file=sys.stderr)
        return 2

    p = Parity()
    rc = p.run(v2, v3)
    print(f"[parity] v2={V2_PATH.name} v3={V3_PATH.name}")
    print(f"[parity] 等价字段项：{p.ok_rows}")
    print(f"[parity] 已核准偏差：{len(p.allowed)}")
    for msg in p.allowed:
        print("  · " + msg)
    print(f"[parity] 漂移/缺失：{len(p.issues)}")
    for msg in p.issues:
        print("  - " + msg)
    if rc == 0:
        tail = f"（含 {len(p.allowed)} 项已核准偏差）" if p.allowed else ""
        print(f"[parity] ✅ v3 已覆盖 v2 运行时消费字段{tail}，M1 切换无丢数据风险")
    else:
        print("[parity] ❌ 存在未核准的缺失/漂移：先补 v3，再进 M1", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
