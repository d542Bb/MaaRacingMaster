#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鉴宝 NavKit adapter（统一计划 P3，首次认领）。

adapter 的职责：声明鉴宝模块「有哪些可校准类别 + 缺省归属 + 路径布局 + 领域端点」，
并复用 core 的 session/categories/reader/renderer 完成浏览与匹配。generic studio
只 dispatch 端点，不在此理解 OCR/出价内容；OCR/彩蛋的**领域识别逻辑**注册为
adapter 端点（server 转发），供 racing 等未来模块复用同一 server 骨架。

结构落点（迁移自 NavKit 控制台/server.py）：
    - 类别：stage / actions / ocr / appraisers / eggs
    - ROI 文件：plugins/treasure/resources/config/treasure_rois.json
    - 截图根：debug/treasure/（会话目录）
    - 领域端点：/api/ocr_recognize（RapidOCR 单 ROI）、/api/eggs_recognize（彩蛋识别）
"""
from __future__ import annotations

from pathlib import Path

from tools.navkit.core.categories import CategoryDefs
from tools.navkit.core.session import SessionBrowser

from maaracing_assistant.core.paths import debug_dir

PROJ = Path(__file__).resolve().parent.parent.parent.parent

CATEGORIES: tuple[str, ...] = ("stage", "actions", "ocr", "appraisers", "eggs")

# v2 缺省归属（与 NavKit 控制台/server.py 的 DEFAULT_* 保持一致，幂等补填）。
DEFAULT_ACTIONS = {
    "bid_confirm_red_btn": {"rect": [0.0, 0.0, 0.0, 0.0], "templates": ["bid_confirm_red_btn.png"]},
    "confirm_red_btn": {"rect": [0.0, 0.0, 0.0, 0.0], "templates": ["confirm_red_btn.png"]},
    "settle_collect_red_btn": {"rect": [0.0, 0.0, 0.0, 0.0], "templates": ["settle_collect_red_btn.png"]},
}
DEFAULT_APPRAISERS = {
    "appraiser_p1_caroline": {"prio": 1, "rect": [0.03, 0.18, 0.97, 0.92],
                              "templates": ["appraiser_p1_caroline.png"], "threshold": 0.72},
    "appraiser_p2_shotaro": {"prio": 2, "rect": [0.03, 0.18, 0.97, 0.92],
                             "templates": ["appraiser_p2_shotaro.png"], "threshold": 0.72},
}
DEFAULT_ITEMS = {
    "actions": DEFAULT_ACTIONS,
    "appraisers": DEFAULT_APPRAISERS,
}


def make_category_defs() -> CategoryDefs:
    return CategoryDefs(CATEGORIES, name="treasure", default_items=DEFAULT_ITEMS)


def rois_path() -> Path:
    # 必须与运行时读取路径（插件 CONFIG_DIR / treasure_rois.json）完全一致，
    # 否则调试台校准保存后运行时读不到
    return PROJ / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "config" / "treasure_rois.json"


def session_dir() -> Path:
    """鉴宝 debug 截图根：%APPDATA%/MaaRacingAssistant/debug/treasure。

    与 treasure_module._prepare_debug_dirs 的写盘目录（debug_dir()/treasure）
    严格一致——旧实现的 `PROJ/debug/treasure` 与用户数据目录解耦才会导致调不到会话。
    """
    return debug_dir() / "treasure"


def make_session_browser() -> SessionBrowser:
    return SessionBrowser(session_dir())


def template_dir() -> Path:
    return PROJ / "maaracing_assistant" / "plugins" / "treasure" / "resources" / "image"


# ---------------------------------------------------------------------------
# M2-B1 · v2-flat ↔ v3 锚点双向投影（校准台以 v3 为唯一真源，treasure_rois.json 退役）
# ---------------------------------------------------------------------------
# 校准 UI 仍按 v2 扁平 {category:{key:{rect,templates,threshold}}} 契约与 /api/rois 通信；
# 但落点改为 treasure_assets.json。M1 后运行时全读 v3，若校准仍写 v2 就会「改了不生效」——
# 本投影消灭该漂移。adapter 声明「类别→键目录」（键名与 v3 锚点 id 同，除下方 rename）。
ROIS_SOURCE = "v3"

# 唯一跨段改名：v2 actions.session_start_match_btn(点击区，空模板) → v3 anchors.session_start_match_click
# （与 check_v2_v3_parity.py 的 RENAME 一致；stage.session_start_match_btn 判定版另指同名 template 锚点）。
ROIS_RENAME = {("actions", "session_start_match_btn"): "session_start_match_click"}

# 类别 → 可校准键列表（从原 treasure_rois.json 冻结，53 项）。
CALIB_CATALOG: dict[str, tuple[str, ...]] = {
    "stage": (
        "daily_high_banner", "egg_reward_title", "settle_title", "result_banner",
        "smart_bid_btn", "round_big_banner", "appraiser_title", "hall_peak_appraise_card",
        "goto_appraise_btn", "hall_session_cards", "is_matching_btn",
        "session_start_match_btn", "appraiser_selected_check",
    ),
    "appraisers": ("appraiser_p1_caroline", "appraiser_p2_shotaro"),
    "ocr": (
        "bid_result_amount_box", "bid_player1", "bid_player2", "bid_player3", "bid_player4",
        "player_name1", "player_name2", "player_name3", "player_name4",
        "settle_final_price", "settle_total_price", "settle_profit", "settle_my_income",
        "my_balance", "round_label_area", "bid_main_btn_label", "session_daily_count",
        "daily_high_score",
    ),
    "eggs": ("egg",),
    "actions": (
        "bid_confirm_red_btn", "confirm_red_btn", "settle_collect_red_btn",
        "session_master_badge", "session_expert_badge", "session_intern_badge",
        "session_start_match_btn", "bid_main_red_btn",
        "bid_numpad_1", "bid_numpad_2", "bid_numpad_3", "bid_numpad_4", "bid_numpad_5",
        "bid_numpad_6", "bid_numpad_7", "bid_numpad_8", "bid_numpad_9", "bid_numpad_0",
        "bid_numpad_clear",
    ),
}

_EGGS_COUNT_KEYS = ("_count_dx_norm", "_count_dy_norm", "_count_w_norm", "_count_h_norm")


def _calib_anchor_id(cat: str, key: str) -> str:
    return ROIS_RENAME.get((cat, key), key)


def flat_from_v3_doc(assets_doc: dict) -> dict:
    """v3 资产文档 → 校准 UI 的 v2 扁平结构（rect/templates/threshold/prio + eggs 段级计数）。"""
    anchors = assets_doc.get("anchors", {}) or {}
    flat: dict = {
        "_schema_ver": 2,
        "reference_size": list(assets_doc.get("reference_size") or [1280, 720]),
    }
    for cat, keys in CALIB_CATALOG.items():
        seg: dict = {}
        for key in keys:
            a = anchors.get(_calib_anchor_id(cat, key))
            if not isinstance(a, dict):
                continue
            item: dict = {
                "rect": [float(n) for n in (a.get("rect") or [0.0, 0.0, 0.0, 0.0])],
                "templates": [t for t in (a.get("templates") or []) if isinstance(t, str) and t],
            }
            if a.get("threshold") is not None:
                item["threshold"] = float(a["threshold"])
            if cat == "appraisers" and a.get("order") is not None:
                item["prio"] = int(a["order"])
            if a.get("comment"):
                item["comment"] = a["comment"]
            seg[key] = item
        if cat == "eggs":
            dom = (anchors.get("egg") or {}).get("domain") or {}
            for ck in _EGGS_COUNT_KEYS:
                if ck in dom:
                    seg[ck] = dom[ck]
        flat[cat] = seg
    return flat


def apply_v2flat_to_v3_doc(assets_doc: dict, flat: dict) -> dict:
    """校准提交的 v2 扁平结构 merge 回 v3 锚点：只改对应锚点 rect/threshold/templates/order 与
    egg 的 domain 计数参数，**保留 v3 中未被校准触碰的其它锚点及全部非锚点段**。返回新文档（未落盘）。
    """
    import copy
    doc = copy.deepcopy(assets_doc)
    anchors = doc.setdefault("anchors", {})
    if not isinstance(flat, dict):
        return doc
    for cat, keys in CALIB_CATALOG.items():
        seg = flat.get(cat) or {}
        if not isinstance(seg, dict):
            continue
        for key in keys:
            item = seg.get(key)
            if not isinstance(item, dict):
                continue
            a = anchors.get(_calib_anchor_id(cat, key))
            if not isinstance(a, dict):
                continue
            rect = item.get("rect")
            if isinstance(rect, list) and len(rect) == 4:
                a["rect"] = [float(n) for n in rect]
            tpls = item.get("templates")
            if isinstance(tpls, list) and (tpls or "templates" in a):
                # 仅在「有模板要写」或「该锚点本就建模了 templates」时写：避免把空 templates:[]
                # 注入 ocr/point 等原本无 templates 的锚点（否则每次保存凭空多出字段、破坏幂等）。
                a["templates"] = [t for t in tpls if isinstance(t, str) and t]
            if item.get("threshold") is not None:
                a["threshold"] = float(item["threshold"])
            if cat == "appraisers" and item.get("prio") is not None:
                a["order"] = int(item["prio"])
        if cat == "eggs":
            egg = anchors.get("egg")
            if isinstance(egg, dict):
                dom = egg.setdefault("domain", {})
                for ck in _EGGS_COUNT_KEYS:
                    if ck in seg:
                        dom[ck] = float(seg[ck])
    return doc


# ---------------------------------------------------------------------------
# 领域端点：OCR / 彩蛋（迁移自 NavKit 控制台/server.py；server only dispatch）
# ---------------------------------------------------------------------------
_ocr_instance = None
_ocr_init_attempted = False
_ocr_lock = __import__("threading").Lock()


def _get_ocr():
    """懒加载 TreasureOcr，失败一次不再重试。"""
    global _ocr_instance, _ocr_init_attempted
    if _ocr_init_attempted:
        return _ocr_instance
    with _ocr_lock:
        if _ocr_init_attempted:
            return _ocr_instance
        try:
            import sys as _sys
            if str(PROJ) not in _sys.path:
                _sys.path.insert(0, str(PROJ))
            from maaracing_assistant.plugins.treasure.ocr import TreasureOcr
            _ocr_instance = TreasureOcr(PROJ)
        except Exception as e:  # noqa: BLE001
            _ocr_instance = None
            print(f"[NavKit] TreasureOcr 初始化失败: {e}")
        finally:
            _ocr_init_attempted = True
    return _ocr_instance


def register_endpoints(state) -> None:
    """注册 treasure 领域端点（OCR/彩蛋）到 server 的 extra_handlers。"""
    state.extra_handlers.setdefault("POST", {})["/api/ocr_recognize"] = _handle_ocr
    state.extra_handlers.setdefault("POST", {})["/api/eggs_recognize"] = _handle_eggs


def _handle_ocr(handler, body: dict) -> None:
    """/api/ocr_recognize：单 ROI 用 RapidOCR 识别文字/金额 + 尺寸建议。"""
    import cv2
    _, crop_bgr, crop_preview, ocr = _crop_for_roi(handler, body, max_w=480)
    if crop_bgr is None:
        handler._send_json({"error": "截图不存在或 ROI 为空", "crop_preview": ""}, 400)
        return
    cw, ch = crop_bgr.shape[1], crop_bgr.shape[0]
    if ocr is None:
        handler._send_json({
            "crop_preview": crop_preview, "crop_size": [cw, ch],
            "error": "TreasureOcr 不可用（导入/初始化失败，终端有详细日志）",
        })
        return
    try:
        # rect 归一化 → 单块识别（手动拖出的临时 ROI 也能立刻出结果，且不做全量冗余识别）
        frame_rgb = cv2.cvtColor(handler._read_bgr(body) or _crop_frame(crop_bgr), cv2.COLOR_BGR2RGB)
        info = ocr.recognize_single(frame_rgb, body.get("rect")) or {}
        raw_lines = info.get("raw_lines") or []
        lines = [ln.strip() for ln in raw_lines if isinstance(ln, str) and ln.strip()]
        if not lines:
            t = info.get("text") or ""
            if t:
                lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
        est_char_h = int(ch * 0.65)
        size_warn = ""
        if ch < 36:
            size_warn = ("🔴 尺寸不足：ROI 高仅 {}px，估算字符高约 {}px。"
                         "PP-OCR 识别模型输入高固定 48px，建议把 ROI 上下外扩 10~15px 或整体高度拉到 ≥72px。").format(ch, est_char_h)
        elif ch < 48:
            size_warn = ("🟡 尺寸偏小：ROI 高 {}px，估算字符高约 {}px，能识别但不稳定。建议高度拉到 ≥72px。").format(ch, est_char_h)
        elif cw < 20:
            size_warn = "建议：ROI 宽度仅 {}px，可能没包含任何文字。".format(cw)
        handler._send_json({
            "crop_preview": crop_preview, "crop_size": [cw, ch],
            "lines": lines, "text": info.get("text") or "",
            "amount": info.get("amount"), "amounts": info.get("amounts") or [],
            "duration_ms": info.get("duration_ms", 0),
            "size_warning": size_warn, "error": None,
        })
    except Exception as e:  # noqa: BLE001
        handler._send_json({
            "crop_preview": crop_preview, "crop_size": [cw, ch],
            "lines": [], "text": "", "amount": None, "amounts": [],
            "duration_ms": 0, "error": f"OCR 调用异常: {e}",
        })


def _handle_eggs(handler, body: dict) -> None:
    """/api/eggs_recognize：彩蛋识别（图标匹配 + 计数 OCR）。"""
    import sys as _sys
    if str(PROJ) not in _sys.path:
        _sys.path.insert(0, str(PROJ))
    try:
        from maaracing_assistant.plugins.treasure.eggs import EggRewardRecognizer
        rec = EggRewardRecognizer(PROJ, ocr=_get_ocr())
    except Exception as e:  # noqa: BLE001
        handler._send_json({"error": f"彩蛋识别器不可用: {e}"})
        return
    if not rec.configured:
        handler._send_json({"configured": False,
                            "error": "eggs 段未配置完整模板/rect（缺 egg_*.png 或 rect 非法）"})
        return
    img = handler._read_bgr(body)
    if img is None:
        handler._send_json({"error": "截图不存在"}, 404)
        return
    try:
        frame_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res = rec.recognize(frame_rgb) or {"counts": {"red": 0, "yellow": 0, "blue": 0}, "eggs": []}
        res["configured"] = True
        handler._send_json(res)
    except Exception as e:  # noqa: BLE001
        handler._send_json({"error": f"彩蛋识别异常: {e}"})


def _crop_for_roi(handler, body: dict, max_w: int):
    """从 body 的 session/image+rect 抠 ROI，返回 (bgr_crop, crop_bgr, preview, ocr)。"""
    import cv2
    from tools.navkit.core.renderer import bgr_to_dataurl
    img = handler._read_bgr(body)
    if img is None:
        return None, None, "", None
    H, W = img.shape[:2]
    rect = body.get("rect")
    if not rect:
        return img, img, bgr_to_dataurl(img, max_w=max_w), _get_ocr()
    x1n, y1n, x2n, y2n = (float(v) for v in rect)
    x1, y1 = max(0, int(x1n * W)), max(0, int(y1n * H))
    x2, y2 = min(W, int(x2n * W)), min(H, int(y2n * H))
    if x2 <= x1 or y2 <= y1:
        return None, None, "", _get_ocr()
    crop = img[y1:y2, x1:x2]
    return img, crop, bgr_to_dataurl(crop, max_w=max_w), _get_ocr()


def _crop_frame(crop_bgr):
    return crop_bgr