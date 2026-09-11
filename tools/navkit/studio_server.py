#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NavKit v4 Studio —— 单进程单端口三页工具（ROI 校准台 / 策略表 / 模板截取）。

一个 `ThreadingHTTPServer` 同时承载三件事，浏览器只开一个端口（26530）：

- `/`           壳页（统一标签栏 + iframe 切三页）
- `/policy`     策略表薄页（读写 `treasure.policy.json` 的 `policy.rules`，原 policy_server 整体并入）
- `/roi`        ROI 校准台（`/static/calibrator.html`，离线回放 + 快速选区 + 测分）
- `/cropper`    模板截取（静态只读服务 `tools/template_cropper/index.html`）

数据面（本文件主体）：
- 读面 `GET /api/rois` 输出 **flat 投影**：spec 三组（template/point/ocr）+ `nodes` 组（pipeline
  两文件逐处 rect）+ `tuning` 组（第 54 个 rect）+ `_meta`。读盘**一律直读 JSON**，
  禁用 `load_nav_source`（其双层 lru_cache 冷生效，改完回读会拿旧值）。
- 写面 `POST /api/rois` 管线（顺序锁定）：base_hash 比对(409) → 内存合并 → 结构校验 →
  `check_truth` 三闸（validate_graph + namespace_checks + cross_checks + rect_checks，
  import 复用）→ preview 则回 diff/report 不落盘 → 落盘（逐文件 mkstemp + os.replace）→
  响应 `{ok, diff, report, compile, source_hash_new, base_hash}`。
- 测分/跨帧/OCR 走**生产同源**引擎：`maaracing_master.core.template_match.find_any_cs`、
  `maaracing_master.plugins.treasure.ocr.TreasureOcr.recognize_single`。

写盘格式纪律（policy_server 范式）：indent=2、ensure_ascii=False、LF、尾换行、键序原样、
float 全精度 repr 不回舍入；改完 diff 只许目标值那一行变化。

用法：
    python studio_server.py [--port 26530]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import select
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# ---------------- 真源定位（仓库内绝对定位，兼容从任意 cwd 启动） ----------------
REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "maaracing_master"
PLUGIN = PACK / "plugins" / "treasure"
RES = PLUGIN / "resources"
POLICY_FILE = RES / "policy" / "treasure.policy.json"
PIPELINE_DIR = RES / "pipeline"
TEMPLATE_DIR = RES / "image"
STATIC_DIR = Path(__file__).resolve().parent / "static"
CROPPER_HTML = REPO / "tools" / "template_cropper" / "index.html"

# pipeline 两真源（顺序固定，供 hash / 落盘 / 图装配共用）
PIPELINE_FILES: dict[str, Path] = {
    "treasure": PIPELINE_DIR / "treasure.json",
    "entry": PIPELINE_DIR / "treasure.entry.json",
}
HASH_KEYS = ("policy", "treasure", "entry")

# spec 三组 = kind 三值（template / point / ocr）
SPEC_GROUPS = ("template", "point", "ocr")
# 新增/删除只开放给 spec 锚点；nodes/tuning 是只读镜像面（图结构编辑归 MPE）
EDITABLE_GROUPS = SPEC_GROUPS
# 可编辑字段（编辑面仅此三项）
EDIT_FIELDS = ("rect", "threshold", "colorspace")

# 两面同值锚点清单（拍板默认联动；判定仍按 rect 值匹配，本清单是白名单上限）
MIRROR_ANCHORS = frozenset({
    "hall_peak_appraise_card", "goto_appraise_btn", "hall_session_cards",
    "is_matching_btn", "appraiser_title", "round_big_banner", "smart_bid_btn",
    "result_banner", "settle_title", "daily_high_banner", "egg_reward_title",
})

# 静态服务白名单（扩展名 + 目录）
STATIC_EXTS = frozenset({".html", ".js", ".css", ".png", ".svg", ".ico", ".map", ".woff2"})

# 模板名白名单（与 studio_sessions.TPL_RE 同形，此处独立以便上传/裁剪共用）
TPL_RE = re.compile(r"^[\w\-]+\.png$")
DATA_URL_RE = re.compile(r"^data:image/[\w.+-]+;base64,(?P<b64>.+)$", re.S)
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# 跨帧直方图 10 桶
HIST_BINS = 10


class SaveRejected(Exception):
    """写面拒绝（带 HTTP 码与响应体）。"""

    def __init__(self, code: int, payload: dict):
        super().__init__(payload.get("error") or "rejected")
        self.code = code
        self.payload = payload


# ============================================================================
# 读盘工具（一律直读 JSON，绕开 load_nav_source 的缓存）
# ============================================================================

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def base_hashes() -> dict[str, str]:
    """三真源当前指纹（乐观锁基线）。"""
    return {
        "policy": _sha256_file(POLICY_FILE),
        "treasure": _sha256_file(PIPELINE_FILES["treasure"]),
        "entry": _sha256_file(PIPELINE_FILES["entry"]),
    }


def read_policy() -> dict:
    return _load_json(POLICY_FILE)


def read_pipelines() -> dict[str, dict]:
    return {tag: _load_json(p) for tag, p in PIPELINE_FILES.items()}


def _check_truth_module():
    """延迟导入 check_truth（复用其图校验/交叉校验/几何校验三闸）。"""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from tools.navkit import check_truth as ct
    return ct


def _rect_key(rect) -> tuple:
    """rect 比较键（浮点容差 1e-6，避免 repr 抖动被判为改动）。"""
    return tuple(round(float(x), 6) for x in rect)


def _rect_slots(node: dict) -> list[dict]:
    """节点内所有含 `rect` 的识别参数表（按出现顺序）。

    口径与 `check_truth.custom_recognitions` 完全一致（v1 平铺 / v2 嵌套 / Or 分支全兼容），
    返回的是**文档内 dict 的引用**，改它即改文档。
    """
    ct = _check_truth_module()
    return [p for _name, p in ct.custom_recognitions(node) if isinstance(p.get("rect"), list)]


def build_graph(docs: dict[str, dict]) -> tuple[dict, dict]:
    """由内存文档装配 (节点表, 节点→来源文件)，口径同 check_truth.load_graph。"""
    graph: dict = {}
    origin: dict = {}
    for tag, doc in docs.items():
        path = PIPELINE_FILES[tag]
        for name, node in doc.items():
            if name.startswith("$"):
                continue
            if name in graph:
                raise ValueError(f"节点名重复（双真源）: {name} 同见于 {origin[name]} 与 {path}")
            graph[name] = node
            origin[name] = path
    return graph, origin


def _detect_style(path: Path) -> tuple[str, int]:
    """探测既有文件的 (行尾, 缩进)，缺省 (LF, 2)。

    真源两种风格并存（policy.json = LF/indent 2；pipeline 两文件 = CRLF/indent 4），
    写盘必须逐文件沿用原风格，否则 diff 会被格式噪音淹没（缩进 4→2 会让整个文件变化）。
    探测而非硬编码：工作副本将来按 `.gitattributes`（`*.json text eol=lf`）归一后自适应。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return "\n", 2
    newline = "\r\n" if raw.split(b"\n", 1)[0].endswith(b"\r") else "\n"
    indent = 2
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith(" ") and line.strip():
            indent = len(line) - len(line.lstrip(" "))
            break
    return newline, indent


def _atomic_write_json(path: Path, doc: dict) -> None:
    """原子落盘：同目录 mkstemp + os.replace，保留原权限。

    格式纪律（policy_server 范式）：indent / 行尾沿用原文件（`_detect_style`）、
    ensure_ascii=False、尾换行、键序原样、float 全精度 repr（json.dump 默认走
    float.__repr__，最短往返表示）。
    """
    newline, indent = _detect_style(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix="." + path.name + ".")
    mode = path.stat().st_mode if path.exists() else 0o644
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            json.dump(doc, f, ensure_ascii=False, indent=indent)
            f.write("\n")
        os.replace(tmp, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================================
# 读投影：spec 三组 + nodes 组 + tuning 组 + _meta
# ============================================================================

def _mirror_of(rect, spec: dict) -> str | None:
    """pipeline 处 rect 命中的镜像锚点名（按 rect 值匹配 11 个已知镜像锚点）。

    未在 `MIRROR_ANCHORS` 白名单内的同名值不算镜像——白名单是拍板结论，防止把
    偶然同值的锚点误当联动对象。
    """
    key = _rect_key(rect)
    for name in sorted(MIRROR_ANCHORS):
        anchor = spec.get(name)
        if isinstance(anchor, dict) and _rect_key(anchor["rect"]) == key:
            return name
    return None


def project_rois() -> dict:
    """读面投影（形状锁定，前端 `state.rois[cat]` 消费）。"""
    policy = read_policy()
    perception = policy["perception"]
    spec = perception["spec"]
    out: dict = {group: {} for group in SPEC_GROUPS}

    for key, anchor in spec.items():
        kind = anchor.get("kind")
        if kind not in out:
            continue  # 未知 kind 不投影（保守：不让新 kind 悄悄进列表）
        item: dict = {
            "kind": kind,
            "label": anchor.get("label", key),
            "order": anchor.get("order"),
            "page": anchor.get("page"),
            "rect": [float(x) for x in anchor["rect"]],
        }
        for field in ("templates", "threshold", "colorspace", "guarded_by",
                      "arbitration", "domain", "_comment"):
            if anchor.get(field) is not None:
                item[field] = anchor[field]
        out[kind][key] = item

    # nodes 组：pipeline 两文件逐处 rect（双真源可见性）
    nodes: dict = {}
    for tag, doc in read_pipelines().items():
        for node_name, node in doc.items():
            if node_name.startswith("$"):
                continue
            for idx, param in enumerate(_rect_slots(node)):
                rect = [float(x) for x in param["rect"]]
                nodes[f"{node_name}#{idx}"] = {
                    "kind": "node",
                    "label": node_name,
                    "page": tag,
                    "node": node_name,
                    "index": idx,
                    "file": PIPELINE_FILES[tag].name,
                    "rect": rect,
                    "templates": list(param.get("templates") or []),
                    "colorspace": param.get("colorspace"),
                    "mirrors": _mirror_of(rect, spec),
                }
    out["nodes"] = nodes

    # tuning 组：第 54 个 rect（不在 spec）
    tuning: dict = {}
    appraiser_roi = (policy.get("policy", {}).get("tuning", {})
                     .get("perception", {}).get("appraiser_search_roi"))
    if isinstance(appraiser_roi, list) and len(appraiser_roi) == 4:
        tuning["appraiser_search_roi"] = {
            "kind": "tuning",
            "label": "鉴宝师搜索区（tuning.perception）",
            "page": "appraiser",
            "rect": [float(x) for x in appraiser_roi],
        }
    out["tuning"] = tuning

    definitions = perception["stages"].get("definitions") or {}
    out["_meta"] = {
        "match": perception.get("match"),
        "pages": sorted({d.get("page") for d in definitions.values() if d.get("page")}),
        "stage_anchors": list(perception["stages"].get("global_anchors") or []),
        "mirror_anchors": sorted(MIRROR_ANCHORS),
        "base_hash": base_hashes(),
    }
    return out


# ============================================================================
# 写面：引用检查 → 合并 → 校验 → 落盘
# ============================================================================

def find_references(name: str, policy: dict, docs: dict[str, dict]) -> list[str]:
    """列出仍引用 spec 锚点 `name` 的位置（删除前的防脱钩检查）。

    两种口径都查：`guarded_by` 在 spec 侧是短名，`actuators` 侧是 `treasure.` 前缀名。
    """
    refs: list[str] = []
    perception = policy["perception"]
    for stage, d in (perception["stages"].get("definitions") or {}).items():
        if name in (d.get("active") or []):
            refs.append(f"perception.stages.definitions.{stage}.active")
        if name in (d.get("ocr") or []):
            refs.append(f"perception.stages.definitions.{stage}.ocr")
    for tr in perception.get("transitions") or []:
        if tr.get("on") == name:
            refs.append(f"perception.transitions[{tr.get('stage')}→{tr.get('to')}].on")
    if name in (perception["stages"].get("global_anchors") or []):
        refs.append("perception.stages.global_anchors")
    for key, anchor in perception["spec"].items():
        if anchor.get("guarded_by") == name:
            refs.append(f"perception.spec.{key}.guarded_by")
    actuators = policy.get("actuators") or {}
    if f"treasure.{name}" in actuators:
        refs.append(f"actuators.treasure.{name}")
    return refs


def _set_field(container: dict, field: str, value) -> tuple:
    """写/删字段，返回 (old, new)。空值语义：None/"" → 删除字段。"""
    old = container.get(field)
    if value is None or value == "":
        container.pop(field, None)
        return old, None
    container[field] = value
    return old, value


def _merge_spec_groups(body: dict, policy: dict, spec: dict, diff: dict,
                       problems: list[str]) -> set[str]:
    """合并 spec 三组（编辑面仅 rect/threshold/colorspace）。返回被显式改动的锚点名集合。

    校验类问题（rect 形态非法等）进 `problems`（由 `apply_save` 汇总进 report.errors），
    不在此处抛异常——preview 要能回 200 + ok=false 让前端展示阻断原因。
    """
    explicit: set[str] = set()
    for group in SPEC_GROUPS:
        for key, item in (body.get(group) or {}).items():
            if not isinstance(item, dict):
                continue
            anchor = spec.get(key)
            if anchor is None:
                continue  # 未在 spec 的普通键不得被字段覆盖路径凭空创建
            for field in EDIT_FIELDS:
                if field not in item:
                    continue
                new = item[field]
                if field == "rect":
                    if not (isinstance(new, list) and len(new) == 4):
                        problems.append(f"{group}.{key}.rect 必须是 4 元数组：{new!r}")
                        continue
                    new = [float(x) for x in new]
                    if _rect_key(new) == _rect_key(anchor["rect"]):
                        continue
                elif new == anchor.get(field):
                    continue
                old, applied = _set_field(anchor, field, new)
                diff["changed"].append({
                    "path": f"policy.perception.spec.{key}.{field}",
                    "old": old, "new": applied,
                })
                explicit.add(key)
    return explicit


def _merge_nodes_group(body: dict, docs: dict[str, dict], spec: dict, diff: dict,
                       explicit: set[str]) -> None:
    """合并 nodes 组（只动 rect 值，不重排键、不碰 `$__mpe_*`）。

    镜像联动（拍板默认）：命中的 11 个两面同值锚点默认同步写 spec 同名锚点；
    `only_this_side` 为真时只改 pipeline 面。colorspace 不联动（两面语义独立是现状）。
    """
    for key, item in (body.get("nodes") or {}).items():
        if not isinstance(item, dict):
            continue
        node_name, sep, idx_s = key.rpartition("#")
        if not sep or not idx_s.isdigit():
            continue
        idx = int(idx_s)
        new_rect = item.get("rect")
        if not (isinstance(new_rect, list) and len(new_rect) == 4):
            continue
        new_rect = [float(x) for x in new_rect]
        for tag, doc in docs.items():
            node = doc.get(node_name)
            if node is None:
                continue
            slots = _rect_slots(node)
            if idx >= len(slots):
                continue
            param = slots[idx]
            old_rect = [float(x) for x in param["rect"]]
            if _rect_key(old_rect) == _rect_key(new_rect):
                break
            mirror = _mirror_of(old_rect, spec)
            param["rect"] = new_rect
            diff["changed"].append({
                "path": f"pipeline.{PIPELINE_FILES[tag].name}.{node_name}#{idx}.rect",
                "old": old_rect, "new": new_rect,
            })
            if mirror and not item.get("only_this_side") and mirror not in explicit:
                anchor = spec.get(mirror)
                if isinstance(anchor, dict) and _rect_key(anchor["rect"]) != _rect_key(new_rect):
                    anchor["rect"] = list(new_rect)
                    diff["changed"].append({
                        "path": f"policy.perception.spec.{mirror}.rect",
                        "old": old_rect, "new": list(new_rect),
                    })
            break


def _merge_tuning_group(body: dict, policy: dict, diff: dict) -> None:
    """合并 tuning 组（当前唯一项：appraiser_search_roi）。"""
    target = policy.get("policy", {}).get("tuning", {}).get("perception")
    if not isinstance(target, dict):
        return
    for key, item in (body.get("tuning") or {}).items():
        if key not in target or not isinstance(item, dict):
            continue
        new_rect = item.get("rect")
        if not (isinstance(new_rect, list) and len(new_rect) == 4):
            continue
        new_rect = [float(x) for x in new_rect]
        old_rect = target[key]
        if _rect_key(old_rect) == _rect_key(new_rect):
            continue
        target[key] = new_rect
        diff["changed"].append({
            "path": f"policy.tuning.perception.{key}",
            "old": old_rect, "new": new_rect,
        })


def _merge_added(body: dict, policy: dict, spec: dict, diff: dict,
                 problems: list[str]) -> None:
    """新增锚点：只允许进 policy.spec（kind/page/label 必填校验；节点侧不开放新增）。"""
    known_pages = set(project_rois()["_meta"]["pages"])
    for group, entries in (body.get("added") or {}).items():
        if group not in EDITABLE_GROUPS:
            problems.append(
                f"新增仅允许 spec 锚点（{'/'.join(EDITABLE_GROUPS)}），收到分组 {group!r}"
                "（图结构编辑归 MPE）")
            continue
        if not isinstance(entries, dict):
            continue
        for key, item in entries.items():
            if not isinstance(item, dict):
                continue
            if key in spec:
                problems.append(f"E05 名称冲突：spec 已存在锚点 {key}")
                continue
            kind = item.get("kind") or group
            if kind not in SPEC_GROUPS:
                problems.append(f"新增 {key}：kind 非法 {kind!r}（可选 {'/'.join(SPEC_GROUPS)}）")
                continue
            page = item.get("page")
            if not page:
                problems.append(f"E09 新增 {key}：未选择 page")
                continue
            if known_pages and page not in known_pages:
                problems.append(f"新增 {key}：page {page!r} 不在既有页面集 {sorted(known_pages)}")
                continue
            label = item.get("label") or key
            rect = item.get("rect")
            if not (isinstance(rect, list) and len(rect) == 4):
                problems.append(f"新增 {key}：rect 必须是 4 元数组")
                continue
            entry: dict = {
                "kind": kind,
                "label": label,
                "order": item.get("order", 9999),
                "page": page,
                "rect": [float(x) for x in rect],
            }
            if item.get("templates"):
                entry["templates"] = list(item["templates"])
            if item.get("threshold") is not None:
                entry["threshold"] = float(item["threshold"])
            if item.get("colorspace"):
                entry["colorspace"] = item["colorspace"]
            if item.get("guarded_by"):
                entry["guarded_by"] = item["guarded_by"]
            if kind == "point" and not entry.get("guarded_by"):
                problems.append(f"E10 新增 {key}：point 锚点必须挂模板担保人")
                continue
            spec[key] = entry
            diff["added"].append({"path": f"policy.perception.spec.{key}", "new": entry})


def _merge_deleted(body: dict, policy: dict, spec: dict, docs: dict[str, dict], diff: dict,
                   problems: list[str]) -> None:
    """删除锚点：只删 spec 项；被任何一处引用则记 E12 并点名引用方。"""
    for ref in body.get("deleted") or []:
        group, sep, key = str(ref).partition(".")
        if not sep:
            problems.append(f"deleted 项格式非法：{ref!r}（应为 <组>.<键>）")
            continue
        if group not in EDITABLE_GROUPS:
            problems.append(f"删除仅允许 spec 锚点（{'/'.join(EDITABLE_GROUPS)}），收到 {group!r}")
            continue
        if key not in spec:
            problems.append(f"E12 删除失败：spec 不存在锚点 {key}")
            continue
        refs = find_references(key, policy, docs)
        if refs:
            problems.append(f"E12 删除被拒：{key} 仍被引用 → " + "；".join(refs))
            continue
        old = spec.pop(key)
        diff["removed"].append({"path": f"policy.perception.spec.{key}", "old": old})


def apply_save(body: dict, *, dry_run: bool) -> dict:
    """保存管线主体（顺序锁定）。dry_run=True 即 preview，只回 diff/report 不落盘。"""
    current = base_hashes()
    want = body.get("base_hash")
    if isinstance(want, dict):
        for key in HASH_KEYS:
            expect = want.get(key)
            if expect and expect != current[key]:
                raise SaveRejected(409, {
                    "ok": False,
                    "error": f"base_hash 不符（{key} 真源已被他处修改），请刷新后重试",
                    "base_hash": current,
                })
    elif isinstance(want, str) and want and want != current["policy"]:
        raise SaveRejected(409, {"ok": False, "error": "base_hash 不符，请刷新后重试",
                                 "base_hash": current})

    policy = read_policy()
    docs = read_pipelines()
    spec = policy["perception"]["spec"]
    diff: dict = {"added": [], "removed": [], "changed": []}
    problems: list[str] = []

    explicit = _merge_spec_groups(body, policy, spec, diff, problems)
    _merge_nodes_group(body, docs, spec, diff, explicit)
    _merge_tuning_group(body, policy, diff)
    _merge_added(body, policy, spec, diff, problems)
    _merge_deleted(body, policy, spec, docs, diff, problems)

    # check_truth 三闸（import 复用；内存文档直接校验，无需落盘）
    ct = _check_truth_module()
    graph, origin = build_graph(docs)
    errors, warns = ct.validate_graph(graph)
    errors += ct.namespace_checks(graph, origin)
    errors += ct.cross_checks(graph, policy)
    errors += ct.rect_checks(graph, policy)
    report = {"errors": problems + list(errors), "warnings": list(warns)}
    ok = not report["errors"]

    result = {
        "ok": ok,
        "diff": diff,
        "report": report,
        # 保留 compile 字段形态供前端消费（v4 保存不产生成物，status 恒 skipped_no_routes）
        "compile": {"status": "skipped_no_routes"},
        "compiled": "skipped_no_routes",
        "base_hash": current,
        "source_hash_new": current,
    }
    if dry_run or not ok:
        return result

    _atomic_write_json(POLICY_FILE, policy)
    for tag, doc in docs.items():
        _atomic_write_json(PIPELINE_FILES[tag], doc)
    new_hashes = base_hashes()
    result["base_hash"] = new_hashes
    result["source_hash_new"] = new_hashes
    return result


# ============================================================================
# 图像/引擎（延迟导入 cv2，保持模块导入轻量）
# ============================================================================

def _cv2():
    import cv2
    return cv2


def _np():
    import numpy as np
    return np


def _read_frame_rgb(path: Path):
    """盘上帧 → RGB ndarray（走字节流解码，兼容非 ASCII 路径）。"""
    cv2, np = _cv2(), _np()
    buf = np.frombuffer(path.read_bytes(), np.uint8)
    bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _encode_data_url(img_rgb) -> str:
    cv2 = _cv2()
    ok, buf = cv2.imencode(".png", cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        return ""
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def _norm_to_px(rect, W: int, H: int) -> tuple[int, int, int, int]:
    """归一化 rect [x1,y1,x2,y2]（x2/y2 排他）→ 像素 (x1, y1, x2, y2)。"""
    x1 = max(0, min(W, int(round(float(rect[0]) * W))))
    y1 = max(0, min(H, int(round(float(rect[1]) * H))))
    x2 = max(0, min(W, int(round(float(rect[2]) * W))))
    y2 = max(0, min(H, int(round(float(rect[3]) * H))))
    return x1, y1, x2, y2


def _match_scales(policy: dict | None = None) -> tuple:
    """尺度真源 = `perception.match.scales`（读盘直读）；缺省回落引擎默认。"""
    from maaracing_master.core.template_match import DEFAULT_SCALES
    doc = policy if policy is not None else read_policy()
    scales = (doc.get("perception", {}).get("match") or {}).get("scales")
    if isinstance(scales, list) and scales:
        return tuple(float(s) for s in scales)
    return tuple(DEFAULT_SCALES)


def _fits_any_scale(crop_w: int, crop_h: int, tpl_w: int, tpl_h: int, scales) -> bool:
    """是否存在某个尺度让模板放得进裁剪区（口径同 `_best_match` 的 min 4px + round）。"""
    for s in scales:
        nw = max(4, int(round(tpl_w * s)))
        nh = max(4, int(round(tpl_h * s)))
        if nh <= crop_h and nw <= crop_w:
            return True
    return False


_OCR_LOCK = threading.Lock()
_OCR_INSTANCE = None
_OCR_FAILED = False


def get_ocr():
    """进程内驻留的 TreasureOcr 单例（RapidOCR 首载数秒，懒加载）。"""
    global _OCR_INSTANCE, _OCR_FAILED
    if _OCR_FAILED:
        return None
    if _OCR_INSTANCE is None:
        with _OCR_LOCK:
            if _OCR_INSTANCE is None and not _OCR_FAILED:
                try:
                    if str(REPO) not in sys.path:
                        sys.path.insert(0, str(REPO))
                    from maaracing_master.plugins.treasure.ocr import TreasureOcr
                    _OCR_INSTANCE = TreasureOcr(PLUGIN)
                except Exception:
                    _OCR_FAILED = True
                    return None
    return _OCR_INSTANCE


# ============================================================================
# 各 API 实现
# ============================================================================

def api_match_score(body: dict) -> dict:
    """单帧测分：v4 起吃多模板 + colorspace，引擎与生产同源（find_any_cs）。"""
    from maaracing_master.core.template_match import find_any_cs, load_template

    session = body.get("session")
    image = body.get("image") or body.get("name")
    rect = body.get("rect")
    templates = body.get("templates") or ([body["template"]] if body.get("template") else [])
    if not (session and image and isinstance(rect, list) and len(rect) == 4):
        raise SaveRejected(400, {"error": "session/image/rect 必填"})
    if not templates:
        raise SaveRejected(400, {"error": "templates 不能为空"})

    path = BROWSER.resolve_raw(session, image)
    if path is None:
        raise SaveRejected(404, {"error": f"帧不存在或越权: {session}/{image}"})
    frame = _read_frame_rgb(path)
    if frame is None:
        raise SaveRejected(400, {"error": f"帧解码失败: {image}"})

    policy = read_policy()
    spec_threshold = (policy.get("perception", {}).get("match") or {}).get("threshold", 0.75)
    threshold = float(body.get("threshold") if body.get("threshold") is not None else spec_threshold)
    colorspace = body.get("colorspace") or "rgb"
    scales = tuple(body["scales"]) if body.get("scales") else _match_scales(policy)

    H, W = frame.shape[:2]
    x1, y1, x2, y2 = _norm_to_px(rect, W, H)
    crop = frame[y1:y2, x1:x2]
    crop_h, crop_w = crop.shape[:2]

    # 模板逐个尝试加载（缺失的跳过），用于尺寸不足判定与预览
    loaded = [(name, img) for name in templates
              if (img := load_template(name, [TEMPLATE_DIR])) is not None]
    tpl_img = loaded[0][1] if loaded else None
    tpl_wh = (tpl_img.shape[1], tpl_img.shape[0]) if tpl_img is not None else (0, 0)
    size_ok = any(_fits_any_scale(crop_w, crop_h, img.shape[1], img.shape[0], scales)
                  for _n, img in loaded)

    result: dict = {
        "score": -1.0,
        "box": None,
        "hit_template": "",
        "scales_used": list(scales),
        "crop_preview": _encode_data_url(crop) if crop_w > 0 and crop_h > 0 else "",
        "tpl_preview": _encode_data_url(tpl_img) if tpl_img is not None else "",
        # calibrator 面板消费字段
        "crop_size": [crop_w, crop_h],
        "tpl_size": [tpl_wh[0], tpl_wh[1]],
        "size_ok": size_ok,
        "best_scale": None,
        "hit_norm": None,
    }
    if not size_ok:
        return result

    roi = (x1, y1, crop_w, crop_h)
    box, score, hit_name = find_any_cs(frame, list(templates), [TEMPLATE_DIR],
                                       colorspace=colorspace, threshold=threshold,
                                       scales=scales, roi=roi)
    result["score"] = float(score)
    result["hit_template"] = hit_name or ""
    if box is not None:
        bx1, by1, bx2, by2 = box
        result["box"] = [int(bx1), int(by1), int(bx2), int(by2)]
        result["hit_norm"] = [bx1 / W, by1 / H, bx2 / W, by2 / H]
        if tpl_wh[0]:
            result["best_scale"] = round((bx2 - bx1) / tpl_wh[0], 3)
    return result


def api_cross_frame_test(body: dict) -> dict:
    """跨帧测试：全量帧逐帧读 cv2 跑匹配，分母诚实（无效帧不进 total_frames）。"""
    from maaracing_master.core.template_match import find_any_cs, load_template

    session = body.get("session")
    rect = body.get("rect")
    templates = body.get("templates") or ([body["template"]] if body.get("template") else [])
    if not (session and isinstance(rect, list) and len(rect) == 4 and templates):
        raise SaveRejected(400, {"error": "session/rect/templates 必填"})

    policy = read_policy()
    spec_threshold = (policy.get("perception", {}).get("match") or {}).get("threshold", 0.75)
    threshold = float(body.get("threshold") if body.get("threshold") is not None else spec_threshold)
    colorspace = body.get("colorspace") or "rgb"
    scales = tuple(body["scales"]) if body.get("scales") else _match_scales(policy)

    images = BROWSER.list_raw(session)
    scores: list[float] = []
    excluded = 0
    best: list[dict] = []
    for name in images:
        path = BROWSER.resolve_raw(session, name)
        frame = _read_frame_rgb(path) if path is not None else None
        if frame is None:
            excluded += 1
            continue
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = _norm_to_px(rect, W, H)
        cw, ch = x2 - x1, y2 - y1
        if cw <= 0 or ch <= 0:
            excluded += 1
            continue
        # 有效帧判定：至少一个模板在某个尺度下放得进（放不下 = score -1，不计入分母）
        ok = False
        for n in templates:
            tpl = load_template(n, [TEMPLATE_DIR])
            if tpl is not None and _fits_any_scale(cw, ch, tpl.shape[1], tpl.shape[0], scales):
                ok = True
                break
        if not ok:
            excluded += 1
            continue
        _, val, _hit = find_any_cs(frame, list(templates), [TEMPLATE_DIR],
                                   colorspace=colorspace, threshold=threshold,
                                   scales=scales, roi=(x1, y1, cw, ch))
        scores.append(float(val))
        best.append({"name": name, "score": float(val)})

    total = len(scores)
    result: dict = {
        "total_frames": total,
        "excluded_invalid": excluded,
        "scanned_frames": total + excluded,
        "threshold": threshold,
        "histogram": [[i / HIST_BINS, (i + 1) / HIST_BINS, 0] for i in range(HIST_BINS)],
        "frames_ge_threshold": 0,
        "frames_ge_060": 0,
        "frames_ge_070": 0,
        "frames_ge_080": 0,
        "best_frames": [],
    }
    if total:
        for s in scores:
            idx = min(HIST_BINS - 1, max(0, int(s * HIST_BINS)))
            result["histogram"][idx][2] += 1
        ordered = sorted(scores)
        result["max"] = ordered[-1]
        result["median"] = float(statistics.median(ordered))
        result["p95"] = ordered[min(total - 1, int(round(0.95 * (total - 1))))]
        result["frames_ge_threshold"] = sum(1 for s in scores if s >= threshold)
        result["frames_ge_060"] = sum(1 for s in scores if s >= 0.60)
        result["frames_ge_070"] = sum(1 for s in scores if s >= 0.70)
        result["frames_ge_080"] = sum(1 for s in scores if s >= 0.80)
        best.sort(key=lambda b: -b["score"])
        result["best_frames"] = [{"name": b["name"], "score": round(b["score"], 4)}
                                 for b in best[:10]]
    return result


def api_ocr_recognize(body: dict) -> dict:
    """单区 OCR：直调生产 `TreasureOcr.recognize_single`。"""
    import time as _time

    session = body.get("session")
    image = body.get("image") or body.get("name")
    rect = body.get("rect")
    if not (session and image and isinstance(rect, list) and len(rect) == 4):
        raise SaveRejected(400, {"error": "session/image/rect 必填"})
    path = BROWSER.resolve_raw(session, image)
    if path is None:
        raise SaveRejected(404, {"error": f"帧不存在或越权: {session}/{image}"})
    frame = _read_frame_rgb(path)
    if frame is None:
        raise SaveRejected(400, {"error": f"帧解码失败: {image}"})

    H, W = frame.shape[:2]
    x1, y1, x2, y2 = _norm_to_px(rect, W, H)
    crop = frame[y1:y2, x1:x2]
    ch, cw = crop.shape[:2]

    ocr = get_ocr()
    if ocr is None:
        return {"error": "OCR 引擎不可用（RapidOCR 未安装或加载失败）"}
    started = _time.perf_counter()
    out = ocr.recognize_single(frame, [float(x) for x in rect])
    duration_ms = int((_time.perf_counter() - started) * 1000)
    if out is None:
        return {"error": "识别失败（引擎无输出或区域为空）",
                "crop_size": [cw, ch], "crop_preview": _encode_data_url(crop)}
    raw_lines = [str(t) for t in (out.get("raw_lines") or [])]
    return {
        "amount": out.get("amount"),
        "amounts": list(out.get("amounts") or []),
        "text": out.get("text") or "",
        "raw_lines": raw_lines,
        # calibrator 面板消费字段
        "lines": raw_lines,
        "crop_size": [cw, ch],
        "crop_preview": _encode_data_url(crop),
        "duration_ms": duration_ms,
    }


def api_crop_to_template(body: dict) -> dict:
    """从盘帧裁剪写模板（服务端落盘到 `plugins/treasure/resources/image/`）。"""
    cv2 = _cv2()
    session = body.get("session")
    image = body.get("image") or body.get("name")
    target = body.get("target") or ""
    rect = body.get("rect")
    if not TPL_RE.match(str(target)):
        raise SaveRejected(400, {"error": f"模板名非法（需匹配 {TPL_RE.pattern}）: {target!r}"})
    if not (session and image and isinstance(rect, list) and len(rect) == 4):
        raise SaveRejected(400, {"error": "session/image/rect 必填"})
    path = BROWSER.resolve_raw(session, image)
    if path is None:
        raise SaveRejected(404, {"error": f"帧不存在或越权: {session}/{image}"})
    frame = _read_frame_rgb(path)
    if frame is None:
        raise SaveRejected(400, {"error": f"帧解码失败: {image}"})

    H, W = frame.shape[:2]
    x1, y1, x2, y2 = _norm_to_px(rect, W, H)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        raise SaveRejected(400, {"error": "裁剪区为空（rect 越界或退化）"})
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(TEMPLATE_DIR / target), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
    if not ok:
        raise SaveRejected(500, {"error": f"写盘失败: {target}"})
    # 模板缓存按 mtime/size 指纹热修（template_match._cache），写后无需额外失效
    return {"ok": True, "name": target, "size": [crop.shape[1], crop.shape[0]],
            "rect_px": [x1, y1, x2, y2]}


def api_template_upload(body: dict) -> dict:
    """上传模板（base64 data URL，≤5MB，任意图片格式 → 重编码 PNG 落盘）。"""
    cv2, np = _cv2(), _np()
    name = str(body.get("name") or "")
    data_url = str(body.get("dataUrl") or "")
    if not TPL_RE.match(name):
        raise SaveRejected(400, {"error": f"模板名非法（需匹配 {TPL_RE.pattern}）: {name!r}"})
    m = DATA_URL_RE.match(data_url)
    if not m:
        raise SaveRejected(400, {"error": "dataUrl 形态非法（需 data:image/*;base64,...）"})
    try:
        raw = base64.b64decode(m.group("b64"), validate=False)
    except (binascii.Error, ValueError) as exc:
        raise SaveRejected(400, {"error": f"base64 解码失败: {exc}"}) from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise SaveRejected(400, {"error": f"图片过大（{len(raw)} > {MAX_UPLOAD_BYTES} 字节）"})
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SaveRejected(400, {"error": "图片解码失败（不支持的格式或数据损坏）"})
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(TEMPLATE_DIR / name), img):
        raise SaveRejected(500, {"error": f"写盘失败: {name}"})
    return {"ok": True, "name": name, "size": [img.shape[1], img.shape[0]]}


def api_template_status() -> dict:
    """模板引用状态（命名锁定：unassigned = 存在未引用，dangling = 引用不存在）。"""
    listed = set(BROWSER.list_templates())
    referenced: set[str] = set()
    for anchor in read_policy()["perception"]["spec"].values():
        for t in anchor.get("templates") or []:
            referenced.add(str(t))
    return {
        "listed": sorted(listed),
        "referenced": sorted(referenced),
        "unassigned": sorted(listed - referenced),
        "dangling": sorted(referenced - listed),
    }


def api_list_templates() -> list[str]:
    return BROWSER.list_templates()


# ============================================================================
# 帧库浏览器（进程级；根 = maaracing_master.core.paths.debug_dir()/"treasure"）
# ============================================================================

class _Browser:
    """帧库浏览器：根从现网 `debug_dir()` 派生（勿硬编码路径）。"""

    def __init__(self) -> None:
        self._browser = None

    def _ensure(self):
        if self._browser is None:
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            from maaracing_master.core.paths import debug_dir
            from tools.navkit.studio_sessions import SessionBrowser
            self._browser = SessionBrowser(debug_dir() / "treasure")
        return self._browser

    def list_sessions(self) -> list[str]:
        return self._ensure().list_sessions()

    def list_raw(self, session: str) -> list[str]:
        return self._ensure().list_raw(session)

    def resolve_raw(self, session: str, name: str):
        return self._ensure().resolve_raw(session, name)

    def list_templates(self) -> list[str]:
        from tools.navkit.studio_sessions import list_templates
        return list_templates(TEMPLATE_DIR)


BROWSER = _Browser()


# ============================================================================
# 策略表页（原 policy_server.PAGE 整体并入）
# ============================================================================

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>NavKit v4 策略表</title><style>
body{font-family:system-ui,sans-serif;margin:16px;background:#161b22;color:#e6edf3}
h1{font-size:18px;margin:0 0 8px} .bar{display:flex;gap:8px;align-items:center;margin-bottom:8px}
button{background:#238636;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer}
button.ghost{background:#30363d} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #30363d;padding:4px 8px;font-size:13px;vertical-align:top}
th{background:#21262d;position:sticky;top:0}
input,textarea{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:2px 4px;width:100%;box-sizing:border-box}
.when{color:#9da7b3;font-family:monospace;font-size:11px;white-space:pre-wrap}
#msg{min-height:18px;font-size:13px} .ok{color:#3fb950} .err{color:#f85149}
</style></head><body>
<h1>NavKit v4 策略表 · <span id="path"></span></h1>
<div class="bar">
  <button onclick="load()">重新加载</button>
  <button onclick="addRow()" class="ghost">新增规则</button>
  <button onclick="save()">保存</button>
  <span id="msg"></span>
</div>
<table><thead><tr>
  <th style="width:150px">id</th><th style="width:34%">when（只读）</th>
  <th style="width:120px">decision.key</th><th>decision.hint</th><th style="width:44px"></th>
</tr></thead><tbody id="tb"></tbody></table>
<script>
let data=null;
async function api(path,opts){ const r=await fetch(path,opts); const t=await r.text();
  if(!r.ok){ setMsg(t,'err'); throw new Error(t);} return t?JSON.parse(t):null; }
function setMsg(m,ok){ const e=document.getElementById('msg'); e.className=ok||'ok'; e.textContent=m; }
function render(){
  const tb=document.getElementById('tb'); tb.innerHTML='';
  data.policy.rules.forEach((r,i)=>{
    const tr=document.createElement('tr');
    let td=document.createElement('td'); const id=document.createElement('input'); id.value=r.id; id.disabled=true; td.appendChild(id); tr.appendChild(td);
    td=document.createElement('td'); td.className='when'; td.textContent=JSON.stringify(r.when??{}); tr.appendChild(td);
    td=document.createElement('td'); const k=document.createElement('input'); k.value=r.decision?.key??''; k.oninput=e=>saveKey(i,e.target.value); td.appendChild(k); tr.appendChild(td);
    td=document.createElement('td'); const h=document.createElement('textarea'); h.rows=2; h.value=r.decision?.hint??''; h.oninput=e=>saveHint(i,e.target.value); td.appendChild(h); tr.appendChild(td);
    td=document.createElement('td'); const b=document.createElement('button'); b.className='ghost'; b.textContent='删'; b.onclick=()=>{ data.policy.rules.splice(i,1); render(); }; td.appendChild(b); tr.appendChild(td);
    tb.appendChild(tr);
  });
}
function saveKey(i,v){ const r=data.policy.rules[i]; r.decision=r.decision||{}; r.decision.key=v; }
function saveHint(i,v){ const r=data.policy.rules[i]; r.decision=r.decision||{}; r.decision.hint=v; }
function addRow(){ data.policy.rules.push({id:'new_rule',when:{},decision:{key:'',hint:''}}); render();
  setMsg('已新增一行（id 留 new_ 前缀占位，保存前请改名）',''); }
async function load(){ try{ data=await api('/api/policy'); document.getElementById('path').textContent='policy.rules = '
  + data.policy.rules.length + ' 条'; render(); setMsg('已加载',''); }catch(e){ setMsg('加载失败: '+e.message,'err'); } }
async function save(){ setMsg('保存中...','');
  try{ const need=new Set(['id','when','decision']); for(const r of data.policy.rules){
    if(r.id&&r.id.startsWith('new_')) throw new Error('存在未命名规则 id='+r.id+'，请先命名'); }
    await api('/api/policy',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(data)}); setMsg('已保存。深层校验请跑: tools\\\\navkit\\\\check_truth.py','ok');
  }catch(e){ setMsg('保存失败: '+e.message,'err'); } }
load();
new EventSource("/api/events");
</script></body></html>
"""


def api_policy_get() -> dict:
    return _load_json(POLICY_FILE)


def api_policy_put(doc: dict) -> dict:
    """策略表全文档写盘（原 policy_server 逻辑：原子替换 + 保权限）。"""
    _atomic_write_json(POLICY_FILE, doc)
    return {"status": "ok", "rules": len(doc.get("policy", {}).get("rules", []))}


# ============================================================================
# 空闲退出：SSE 长连接计数（关掉页签即断连 → 延迟自动收摊）
# ============================================================================
# 纯 HTTP 无状态，服务端无法感知「页面还在不在」；页面开一条 text/event-stream
# 长连接即可让断开事件变成 TCP 层事实。计数归零后延迟 `--idle-exit` 秒退出，
# 期间有页面重连（含 EventSource 自动重连）则取消。
# `_ever_connected` 保证「从未有页面连过」时不退出——脚本/curl 直接调 API 的
# 场景（CI、验收脚本）不会被误杀。`--idle-exit 0` 完全关闭该行为。
IDLE_EXIT_DEFAULT = 15
_idle_lock = threading.Lock()
_stream_count = 0
_ever_connected = False
_idle_timer: threading.Timer | None = None
_idle_exit_s = IDLE_EXIT_DEFAULT


def _cancel_idle_timer() -> None:
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None


def _stream_open() -> None:
    global _stream_count, _ever_connected
    with _idle_lock:
        _stream_count += 1
        _ever_connected = True
        _cancel_idle_timer()


def _stream_close() -> None:
    global _stream_count, _idle_timer
    with _idle_lock:
        _stream_count = max(0, _stream_count - 1)
        if _stream_count or not _ever_connected or _idle_exit_s <= 0:
            return
        _cancel_idle_timer()
        timer = threading.Timer(_idle_exit_s, _idle_exit_fire)
        timer.daemon = True
        _idle_timer = timer
        timer.start()


def _idle_exit_fire() -> None:
    with _idle_lock:
        if _stream_count:
            return   # 期间已有页面重连
    os._exit(0)


# ============================================================================
# HTTP Handler
# ============================================================================

SERVER_REF: ThreadingHTTPServer | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "NavKitStudio/1.0"
    # HTTP/1.1 才能用 chunked 流式响应（SSE）；其余响应均带 Content-Length，连接可复用
    protocol_version = "HTTP/1.1"

    # ---- 响应工具 ----
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _txt(self, code: int, text: str, ctype: str = "text/html; charset=utf-8") -> None:
        self._send(code, text.encode("utf-8"), ctype)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _route(self) -> str:
        return unquote(urlparse(self.path).path)

    # ---- 静态服务（白名单目录 + 扩展名 + 穿越拒绝） ----
    def _serve_static(self, rel: str) -> None:
        base = STATIC_DIR.resolve()
        target = (base / rel.lstrip("/")).resolve()
        if not target.is_relative_to(base) or target.suffix.lower() not in STATIC_EXTS:
            self._txt(404, "not found")
            return
        if not target.is_file():
            self._txt(404, "not found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".map": "application/json",
            ".woff2": "font/woff2",
            ".png": "image/png",
        }.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-store"})

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        path = self._route()
        try:
            if path in ("/", "/index.html"):
                self._send(200, (STATIC_DIR / "shell.html").read_bytes(),
                           "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            elif path == "/policy":
                self._txt(200, PAGE)
            elif path == "/api/policy":
                self._json(200, api_policy_get())
            elif path in ("/roi", "/roi/", "/calibrator"):
                self._send(200, (STATIC_DIR / "calibrator.html").read_bytes(),
                           "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            elif path in ("/cropper", "/cropper/"):
                if not CROPPER_HTML.is_file():
                    self._txt(404, "template_cropper/index.html 不存在")
                else:
                    self._send(200, CROPPER_HTML.read_bytes(),
                               "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/api/rois":
                self._json(200, project_rois())
            elif path == "/api/list_sessions":
                self._json(200, BROWSER.list_sessions())
            elif path == "/api/list_images":
                self._json(200, BROWSER.list_raw(self._query().get("session", "")))
            elif path == "/api/image":
                self._serve_frame()
            elif path == "/api/list_templates":
                self._json(200, api_list_templates())
            elif path == "/api/template_status":
                self._json(200, api_template_status())
            elif path == "/api/events":
                self._serve_events()
            else:
                self._txt(404, "not found")
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _serve_frame(self) -> None:
        """盘上原始字节直传（Content-Type 恒 image/png，浏览器嗅探 jpg 可显示）。"""
        q = self._query()
        path = BROWSER.resolve_raw(q.get("session", ""), q.get("name", ""))
        if path is None:
            self._txt(404, "not found")
            return
        self._send(200, path.read_bytes(), "image/png", {"Cache-Control": "no-store"})

    # ---- SSE 长连接（空闲退出用） ----
    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _serve_events(self) -> None:
        """页面保活通道：连接断开（关页签/关浏览器）即让服务端知道。

        用 `select` 探测对端关闭——EventSource 不会主动发数据，socket 变为可读
        且 `recv` 返回空即对端已断，比「写失败才发现」快得多（关页签立即感知）。
        本方法吞掉全部异常：此时响应头已发出，不能再回错误响应。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        _stream_open()
        try:
            self._write_chunk(b": connected\n\n")
            while True:
                ready, _, _ = select.select([self.connection], [], [], 3.0)
                if ready and not self.connection.recv(1):
                    break   # 对端关闭
                self._write_chunk(b": ping\n\n")
        except Exception:  # noqa: BLE001
            pass
        finally:
            _stream_close()

    # ---- PUT ----
    def do_PUT(self):  # noqa: N802
        if self._route() != "/api/policy":
            self._txt(404, "not found")
            return
        try:
            doc = self._read_json_body()
        except Exception as exc:  # noqa: BLE001
            self._txt(400, f"非法 JSON: {exc}")
            return
        try:
            self._json(200, api_policy_put(doc))
        except Exception as exc:  # noqa: BLE001
            self._txt(500, f"写盘失败: {exc}")

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        path = self._route()
        if path == "/api/shutdown":
            self._json(200, {"ok": True})
            # 锁定：同步 shutdown 会阻塞 handler 线程导致响应永不 flush → 先响应，再延迟杀
            threading.Thread(target=_delayed_exit, args=(SERVER_REF,), daemon=True).start()
            return
        try:
            body = self._read_json_body()
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": f"非法 JSON: {exc}"})
            return
        try:
            if path == "/api/rois":
                preview = bool(body.get("preview"))
                res = apply_save(body, dry_run=preview)
                # preview 恒 200（前端靠 ok 决定能否保存）；正式保存失败 → 400
                self._json(200 if (preview or res["ok"]) else 400, res)
            elif path == "/api/match_score":
                self._json(200, api_match_score(body))
            elif path == "/api/cross_frame_test":
                self._json(200, api_cross_frame_test(body))
            elif path == "/api/ocr_recognize":
                self._json(200, api_ocr_recognize(body))
            elif path == "/api/crop_to_template":
                self._json(200, api_crop_to_template(body))
            elif path == "/api/template_upload":
                self._json(200, api_template_upload(body))
            else:
                self._txt(404, "not found")
        except SaveRejected as rej:
            self._json(rej.code, rej.payload)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, *a):  # 静默访问日志
        pass


def _delayed_exit(srv) -> None:
    time.sleep(0.3)
    try:
        if srv is not None:
            srv.shutdown()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


# ============================================================================
# 入口
# ============================================================================

def main() -> int:
    global SERVER_REF, _idle_exit_s
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=26530)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--idle-exit", type=int, default=IDLE_EXIT_DEFAULT,
                    help="最后一个页面断开后自动退出的秒数；0 = 常驻不退出（脚本/curl 调试用）")
    args = ap.parse_args()
    _idle_exit_s = max(0, args.idle_exit)

    for p in (POLICY_FILE, *PIPELINE_FILES.values()):
        if not p.exists():
            print(f"[studio] 真源不存在: {p}")
            return 1
    if not (STATIC_DIR / "shell.html").is_file():
        print(f"[studio] 壳页缺失: {STATIC_DIR / 'shell.html'}")
        return 1

    SERVER_REF = ThreadingHTTPServer((args.host, args.port), Handler)
    idle = f"{_idle_exit_s}s 后自动退出" if _idle_exit_s else "常驻不退出"
    print(f"[studio] http://{args.host}:{args.port}/  "
          f"(ROI 校准台 /roi · 策略表 /policy · 模板截取 /cropper · 关闭页面 {idle})")
    try:
        SERVER_REF.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
