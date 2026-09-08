# -*- coding: utf-8 -*-
"""DebugStudio Core + treasure adapter（统一计划 P3）单测。

覆盖与内容无关的骨架：session 目录/文件白名单与目录穿越防护、categories 校验与缺省
填充、atomic 保存、treasure adapter 的类别/路径暴露；以及 adapter 复用 core 的正常接线。
reader/renderer 属于运行态（依赖 cv2），单测不覆盖图像内部逻辑。
"""
from __future__ import annotations

import json

import pytest

from tools.navkit.adapters import treasure as t_adapter
from tools.navkit.core.categories import CategoriesError, CategoryDefs

# ----------------------------------------------------------------------
# session：会话/文件白名单 + 目录穿越防护
# ----------------------------------------------------------------------


class TestSessionBrowser:
    @pytest.fixture
    def layout(self, tmp_path):
        """构造 debug_root/20260812_183611/raw/{0001_raw.png, bad}.jpg 等结构。"""
        root = tmp_path / "debug"
        root.mkdir()
        sess_dir = root / "20260812_183611"
        raw = sess_dir / "raw"
        raw.mkdir(parents=True)
        (raw / "0001_raw.jpg").write_bytes(b"x")
        (raw / "0002_raw.png").write_bytes(b"x")
        (raw / "not_raw.txt").write_text("nope", encoding="utf-8")   # 非法 raw 名
        # 既无 raw/ 也无 trace.jsonl 的空目录不算合法会话
        (root / "20260101_000000").mkdir()
        # 独立 trace 会话（无图，仅决策流水）：新契约入列
        trace_only = root / "20260202_000000"
        trace_only.mkdir()
        (trace_only / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        # 遗留 session_ 前缀形态：保持可读
        legacy = root / "session_20260303_000000"
        legacy.mkdir()
        (legacy / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        (root / "not_a_session").mkdir(parents=True)
        (root / "not_a_session" / "raw").mkdir()
        return root

    def test_list_sessions_only_valid(self, layout):
        from tools.navkit.core.session import SessionBrowser
        b = SessionBrowser(layout)
        # 无 raw 且无 trace 的空目录、非法名被排除；含二者任一即入列，按时间戳降序
        assert b.list_sessions() == [
            "20260812_183611", "session_20260303_000000", "20260202_000000",
        ]

    def test_has_frames_distinguishes_trace_only(self, layout):
        from tools.navkit.core.session import SessionBrowser
        b = SessionBrowser(layout)
        assert b.has_frames("20260812_183611") is True    # 截图会话
        assert b.has_frames("20260202_000000") is False   # 纯决策流水会话
        assert b.has_frames("not_a_session") is False     # 非法名恒 False

    def test_list_raw_filters_by_whitelist(self, layout):
        from tools.navkit.core.session import SessionBrowser
        b = SessionBrowser(layout)
        raw_list = b.list_raw("20260812_183611")
        assert raw_list == ["0001_raw.jpg", "0002_raw.png"]
        assert "not_raw.txt" not in raw_list

    def test_list_raw_invalid_session_empty(self, layout):
        from tools.navkit.core.session import SessionBrowser
        b = SessionBrowser(layout)
        assert b.list_raw("bad_session") == []
        assert b.list_raw("00000000_000000") == []  # 合法形态但目录不存在

    def test_resolve_raw_in_bounds(self, layout):
        from tools.navkit.core.session import SessionBrowser
        b = SessionBrowser(layout)
        p = b.resolve_raw("20260812_183611", "0001_raw.jpg")
        assert p is not None and p.is_file()
        assert p.parent.name == "raw"

    def test_resolve_raw_blocks_illegal_names(self, layout):
        from tools.navkit.core.session import SessionBrowser
        b = SessionBrowser(layout)
        assert b.resolve_raw("20260812_183611", "not_raw.txt") is None
        assert b.resolve_raw("20260812_183611", "..\\escape.png") is None
        assert b.resolve_raw("bad_session", "0001_raw.jpg") is None

    def test_resolve_raw_blocks_traversal_outside_raw(self, layout):
        # raw/ 内没有该文件、但 raw/ 之外存在同名文件时，绝不越界读取外部路径
        from tools.navkit.core.session import SessionBrowser
        (layout / "20260812_183611" / "0000_raw.jpg").write_bytes(b"x")  # 仅放在 raw 外
        b = SessionBrowser(layout)
        assert b.resolve_raw("20260812_183611", "0000_raw.jpg") is None


# ----------------------------------------------------------------------
# categories：校验 + 缺省填充 + 原子保存
# ----------------------------------------------------------------------

class TestCategoryDefs:
    def make_defs(self):
        return CategoryDefs(
            ("stage", "actions"),
            default_items={"actions": {"btn": {"rect": [0.0, 0.0, 0.0, 0.0]}}},
        )

    def test_validate_ok(self):
        d = self.make_defs()
        d.validate({
            "reference_size": [1280, 720],
            "stage": {"x": {"rect": [0.1, 0.2, 0.3, 0.4], "templates": ["t.png"], "threshold": 0.8}},
            "actions": {},
        })

    def test_validate_missing_reference_size(self):
        with pytest.raises(CategoriesError):
            self.make_defs().validate({"stage": {}})

    def test_validate_missing_category(self):
        with pytest.raises(CategoriesError):
            self.make_defs().validate({"reference_size": [1, 1], "stage": {}})

    def test_validate_bad_rect(self):
        with pytest.raises(CategoriesError):
            self.make_defs().validate({
                "reference_size": [1, 1], "stage": {"x": {"rect": [0.1, 0.2, 1.5, 0.4]}}, "actions": {},
            })

    def test_validate_bad_template_name(self):
        with pytest.raises(CategoriesError):
            self.make_defs().validate({
                "reference_size": [1, 1],
                "stage": {"x": {"rect": [0.0, 0.0, 0.5, 0.5], "templates": ["../evil.png"]}},
                "actions": {},
            })

    def test_validate_bad_threshold(self):
        with pytest.raises(CategoriesError):
            self.make_defs().validate({
                "reference_size": [1, 1],
                "stage": {"x": {"rect": [0.0, 0.0, 0.5, 0.5], "threshold": 1.5}},
                "actions": {},
            })

    def test_underscore_meta_keys_skipped(self):
        # 段内 `_comment` 等元数据键不被当条目校验
        self.make_defs().validate({
            "reference_size": [1280, 720],
            "stage": {"_comment": "任意文本", "x": {"rect": [0.0, 0.0, 0.5, 0.5]}},
            "actions": {},
        })

    def test_fill_defaults_idempotent(self):
        d = self.make_defs()
        data = d.fill_defaults({"actions": {"btn": {"rect": [0.9, 0.9, 1.0, 1.0]}}})
        data2 = d.fill_defaults({"actions": {"btn": {"rect": [0.9, 0.9, 1.0, 1.0]}}})
        # 缺 stage 段被补空；已有 btn 不被覆盖
        assert set(data["stage"]) == set()
        assert data["actions"]["btn"]["rect"] == [0.9, 0.9, 1.0, 1.0]
        assert data == data2  # 幂等

    def test_save_atomic_roundtrip(self, tmp_path):
        f = tmp_path / "rois.json"
        d = self.make_defs()
        data = {"reference_size": [1280, 720], "stage": {"x": {"rect": [0.0, 0.0, 0.5, 0.5]}}, "actions": {}}
        d.save_atomic(data, f)
        assert json.loads(f.read_text(encoding="utf-8")) == data
        # 无残留 tmp 文件
        assert list(tmp_path.glob("*.tmp")) == []

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(CategoriesError):
            self.make_defs().load(tmp_path / "nope.json")


# ----------------------------------------------------------------------
# treasure adapter：认领类别 + 复用 core 接线
# ----------------------------------------------------------------------

class TestTreasureAdapter:
    def test_categories_constant(self):
        assert t_adapter.CATEGORIES == ("stage", "actions", "ocr", "appraisers", "eggs")

    def test_category_defs_are_content_aware_but_generic(self):
        # adapter 声明类目集合；底层 CategoryDefs 只按通用规则校验
        defs = t_adapter.make_category_defs()
        assert "stage" in defs.categories
        assert "eggs" in defs.categories
        assert defs.name == "treasure"

    def test_rois_source_is_v3_single_truth(self):
        # M2-B1：校准台 ROI 真源改指 v3 资产（treasure_assets.json），与运行时读取路径一致，
        # 不再以 treasure_rois.json 为落点（消灭「校准写 v2、运行时读 v3」的改了不生效漂移）。
        from maaracing_assistant.plugins.treasure import CONFIG_DIR
        assert t_adapter.ROIS_SOURCE == "v3"
        assert hasattr(t_adapter, "flat_from_v3_doc")
        assert hasattr(t_adapter, "apply_v2flat_to_v3_doc")
        assert (CONFIG_DIR / "treasure_assets.json").is_file()
        # 唯一跨段改名：actions.session_start_match_btn(点击) → session_start_match_click；
        # stage.session_start_match_btn(判定) 仍指同名 template 锚点。
        assert t_adapter._calib_anchor_id("actions", "session_start_match_btn") == "session_start_match_click"
        assert t_adapter._calib_anchor_id("stage", "session_start_match_btn") == "session_start_match_btn"

    def test_session_browser_wired_to_treasure_debug(self):
        b = t_adapter.make_session_browser()
        assert b.debug_root.name == "treasure"

    def test_template_dir(self):
        assert t_adapter.template_dir().name == "image"

    def _real_v3_doc(self):
        from maaracing_assistant.plugins.treasure import CONFIG_DIR
        return json.loads((CONFIG_DIR / "treasure_assets.json").read_text(encoding="utf-8"))

    def test_flat_from_v3_projects_categories_from_anchors(self):
        doc = self._real_v3_doc()
        anchors = doc["anchors"]
        flat = t_adapter.flat_from_v3_doc(doc)
        assert set(t_adapter.CALIB_CATALOG) <= set(flat)
        # stage 模板 rect 直取锚点；appraisers prio←order；actions rename 命中 click 锚点
        assert flat["stage"]["smart_bid_btn"]["rect"] == [float(n) for n in anchors["smart_bid_btn"]["rect"]]
        assert flat["appraisers"]["appraiser_p1_caroline"]["prio"] == anchors["appraiser_p1_caroline"]["order"]
        assert flat["actions"]["session_start_match_btn"]["rect"] == [
            float(n) for n in anchors["session_start_match_click"]["rect"]
        ]
        # eggs 段级计数来自 egg.domain
        assert flat["eggs"]["_count_dx_norm"] == anchors["egg"]["domain"]["_count_dx_norm"]

    def test_apply_v2flat_idempotent_preserves_noncalib_and_validates(self):
        from maaracing_assistant.core.navkit import Assets, validate_assets
        doc = self._real_v3_doc()
        flat = t_adapter.flat_from_v3_doc(doc)
        roundtrip = t_adapter.apply_v2flat_to_v3_doc(doc, flat)
        for aid in ("smart_bid_btn", "egg", "appraiser_p1_caroline", "round_label_area"):
            assert roundtrip["anchors"][aid]["rect"] == doc["anchors"][aid]["rect"], f"{aid} project→apply 不幂等"
        # 改一个 rect → 仅该锚点变，未校准锚点不动
        flat2 = json.loads(json.dumps(flat))
        flat2["stage"]["smart_bid_btn"]["rect"] = [0.71, 0.77, 0.81, 0.85]
        edited = t_adapter.apply_v2flat_to_v3_doc(doc, flat2)
        assert edited["anchors"]["smart_bid_btn"]["rect"] == [0.71, 0.77, 0.81, 0.85]
        assert edited["anchors"]["round_big_banner"]["rect"] == doc["anchors"]["round_big_banner"]["rect"]
        # 编辑后文档仍能过运行时同款校验（校准不得写出读不懂的资产）
        assert validate_assets(Assets.from_document(edited, module="treasure")).ok