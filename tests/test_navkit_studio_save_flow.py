# -*- coding: utf-8 -*-
"""ROI Studio 保存管线契约（v4 重写基线：v3 `test_navkit_studio_save_flow.py`）。

覆盖规格 §4.2 锁定的保存语义：
- 写盘字节纪律：无改动回存**字节级幂等**；单点改 rect 后**仅该行变化**（键序/缩进/尾换行零漂移）；
- 镜像联动（默认两面同写）与「仅改此面」逃生口；colorspace 不联动；
- 校验类问题（E05/E09/E10/E12、nodes 组不开放增删、page 非法）进 `report.errors`：
  preview 回 ok=false 且不落盘，正式保存同样不落盘（HTTP 码由 Handler 层转 400）；
- `base_hash` 乐观锁 409；
- check_truth 三闸（图自洽 / 交叉互洽 / 几何）确实生效。

全部在 tmp 真源副本上跑（monkeypatch 路径常量），绝不触碰仓库真源。
"""
from __future__ import annotations

import json
import shutil

import pytest

from tools.navkit import studio_server as s


@pytest.fixture
def env(tmp_path, monkeypatch):
    """真源副本 + 路径常量重定向；返回 tmp 路径与原字节快照。"""
    policy = tmp_path / "treasure.policy.json"
    treasure = tmp_path / "treasure.json"
    entry = tmp_path / "treasure.entry.json"
    shutil.copy2(s.POLICY_FILE, policy)
    shutil.copy2(s.PIPELINE_FILES["treasure"], treasure)
    shutil.copy2(s.PIPELINE_FILES["entry"], entry)
    monkeypatch.setattr(s, "POLICY_FILE", policy)
    monkeypatch.setattr(s, "PIPELINE_FILES", {"treasure": treasure, "entry": entry})
    return {
        "dir": tmp_path,
        "policy": policy,
        "treasure": treasure,
        "entry": entry,
        "before": {"policy": policy.read_bytes(),
                   "treasure": treasure.read_bytes(),
                   "entry": entry.read_bytes()},
    }


def _body(**over):
    proj = s.project_rois()
    body = {**proj, "added": {}, "deleted": [],
            "base_hash": proj["_meta"]["base_hash"], "preview": False}
    body.update(over)
    return body


def _bytes(env) -> dict[str, bytes]:
    return {"policy": env["policy"].read_bytes(),
            "treasure": env["treasure"].read_bytes(),
            "entry": env["entry"].read_bytes()}


def _policy(env) -> dict:
    return json.loads(env["policy"].read_text(encoding="utf-8"))


def _errors(res) -> str:
    return " | ".join(res["report"]["errors"])


# ----------------------------------------------------------------------
# 写盘字节纪律
# ----------------------------------------------------------------------


class TestWriteDiscipline:
    def test_noop_save_is_byte_identical(self, env):
        """GET 投影原样回存 → 三真源字节不变（键序/缩进/行尾/尾换行/float 全精度）。"""
        res = s.apply_save(_body(), dry_run=False)
        assert res["ok"] is True
        assert not any(res["diff"][k] for k in ("added", "removed", "changed"))
        assert _bytes(env) == env["before"]

    def test_single_rect_edit_touches_one_line(self, env):
        """改一位小数 → 行数不变、仅 1 行不同、pipeline 未动。"""
        proj = s.project_rois()
        key = "bid_main_btn_label"
        old = proj["ocr"][key]["rect"]
        proj["ocr"][key]["rect"] = [round(old[0] + 0.001, 4)] + list(old[1:])
        res = s.apply_save({**proj, "added": {}, "deleted": [],
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert len(res["diff"]["changed"]) == 1, res["diff"]["changed"]
        assert res["diff"]["changed"][0]["path"] == f"policy.perception.spec.{key}.rect"

        old_lines = env["before"]["policy"].decode("utf-8").splitlines()
        new_lines = env["policy"].read_text(encoding="utf-8").splitlines()
        assert len(old_lines) == len(new_lines)
        diff_lines = [(i, a, b) for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        assert len(diff_lines) == 1, diff_lines[:3]
        # 期望值由本次改动推导，不写死常数——否则 ROI 一调（如加宽），这条断言会因为一个
        # 与它无关的原因变红（它要证的是「只动一行」，不是「值必须是某个数」）。
        assert str(proj["ocr"][key]["rect"][0]) in diff_lines[0][2]
        assert _bytes(env)["treasure"] == env["before"]["treasure"]
        assert _bytes(env)["entry"] == env["before"]["entry"]

    def test_resave_is_byte_idempotent(self, env):
        proj = s.project_rois()
        proj["ocr"]["bid_main_btn_label"]["rect"] = [0.4324, 0.805286343612335,
                                                     0.523953744493392, 0.8575624082232012]
        s.apply_save({**proj, "added": {}, "deleted": [],
                      "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        snap = _bytes(env)
        res = s.apply_save(_body(), dry_run=False)
        assert not any(res["diff"][k] for k in ("added", "removed", "changed"))
        assert _bytes(env) == snap

    def test_pipeline_files_keep_original_style(self, env):
        """写盘沿用各真源既有的行尾与缩进（否则 diff 会被格式噪音淹没）。

        真源在仓库内是 LF（`.gitattributes` 声明 `*.json text eol=lf`），Windows 工作
        副本可能是 CRLF——断言「写盘前后风格不变」，两种检出形态都成立。
        """
        keys = ("policy", "treasure", "entry")
        styles_before = {k: s._detect_style(env[k]) for k in keys}
        s.apply_save(_body(), dry_run=False)
        assert {k: s._detect_style(env[k]) for k in keys} == styles_before

    def test_pipeline_indent_is_four(self, env):
        """pipeline 真源缩进为 4（与 policy 的 2 不同），写盘后必须保持。"""
        assert s._detect_style(env["treasure"])[1] == 4
        s.apply_save(_body(), dry_run=False)
        assert s._detect_style(env["treasure"])[1] == 4

    def test_policy_indent_is_two(self, env):
        assert s._detect_style(env["policy"])[1] == 2


# ----------------------------------------------------------------------
# 镜像联动
# ----------------------------------------------------------------------


class TestMirrorLinkage:
    NODE = "treasure.hall_peak_appraise_card#0"   # mirrors = hall_peak_appraise_card

    def test_default_writes_both_sides(self, env):
        proj = s.project_rois()
        assert proj["nodes"][self.NODE]["mirrors"] == "hall_peak_appraise_card"
        rect = list(proj["nodes"][self.NODE]["rect"])
        rect[0] = round(rect[0] + 0.002, 4)
        proj["nodes"][self.NODE]["rect"] = rect
        res = s.apply_save({**proj, "added": {}, "deleted": [],
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        paths = [d["path"] for d in res["diff"]["changed"]]
        assert any("pipeline" in p for p in paths)
        assert any("spec.hall_peak_appraise_card" in p for p in paths)
        spec_rect = _policy(env)["perception"]["spec"]["hall_peak_appraise_card"]["rect"]
        assert [round(x, 4) for x in spec_rect] == [round(x, 4) for x in rect]

    def test_only_this_side_skips_spec(self, env):
        proj = s.project_rois()
        spec_before = list(_policy(env)["perception"]["spec"]["hall_peak_appraise_card"]["rect"])
        rect = list(proj["nodes"][self.NODE]["rect"])
        rect[0] = round(rect[0] + 0.003, 4)
        proj["nodes"][self.NODE]["rect"] = rect
        proj["nodes"][self.NODE]["only_this_side"] = True
        res = s.apply_save({**proj, "added": {}, "deleted": [],
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert all("pipeline" in d["path"] for d in res["diff"]["changed"])
        assert _policy(env)["perception"]["spec"]["hall_peak_appraise_card"]["rect"] == spec_before

    def test_colorspace_not_linked(self, env):
        """colorspace 不联动：节点面写 rgb 不得改 spec 面的 gray。"""
        proj = s.project_rois()
        node = next(k for k, v in proj["nodes"].items() if v["mirrors"] == "round_big_banner")
        rect = list(proj["nodes"][node]["rect"])
        rect[1] = round(rect[1] + 0.001, 4)
        proj["nodes"][node]["rect"] = rect
        s.apply_save({**proj, "added": {}, "deleted": [],
                      "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert _policy(env)["perception"]["spec"]["round_big_banner"]["colorspace"] == "gray"


# ----------------------------------------------------------------------
# 结构校验（错误进 report.errors，preview 不抛异常）
# ----------------------------------------------------------------------


class TestStructureValidation:
    def test_added_missing_page_e09(self, env):
        res = s.apply_save(_body(added={"point": {"new_btn": {
            "kind": "point", "page": "", "rect": [0.3, 0.8, 0.35, 0.85]}}}), dry_run=True)
        assert res["ok"] is False
        assert "E09" in _errors(res)
        assert _bytes(env) == env["before"]

    def test_added_point_without_guard_e10(self, env):
        res = s.apply_save(_body(added={"point": {"new_btn": {
            "kind": "point", "page": "bidding",
            "rect": [0.3, 0.8, 0.35, 0.85]}}}), dry_run=True)
        assert res["ok"] is False
        assert "E10" in _errors(res)

    def test_added_name_conflict_e05(self, env):
        res = s.apply_save(_body(added={"template": {"smart_bid_btn": {
            "kind": "template", "page": "bidding", "label": "x",
            "rect": [0.1, 0.1, 0.2, 0.2]}}}), dry_run=True)
        assert res["ok"] is False
        assert "E05" in _errors(res)

    def test_added_bad_page_rejected(self, env):
        res = s.apply_save(_body(added={"template": {"new_a": {
            "kind": "template", "page": "__no_such_page", "label": "x",
            "rect": [0.1, 0.1, 0.2, 0.2]}}}), dry_run=True)
        assert res["ok"] is False
        assert "page" in _errors(res)

    def test_nodes_group_add_forbidden(self, env):
        res = s.apply_save(_body(added={"nodes": {"x#0": {"rect": [0.1, 0.1, 0.2, 0.2]}}}),
                           dry_run=True)
        assert res["ok"] is False
        assert "MPE" in _errors(res)

    def test_implicit_new_key_not_fabricated(self, env):
        """未在 spec 的普通键不得被字段覆盖路径凭空创建。"""
        proj = s.project_rois()
        proj["template"]["mystery_btn"] = {"rect": [0.1, 0.1, 0.2, 0.2], "templates": []}
        res = s.apply_save({**proj, "added": {}, "deleted": [],
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert not res["diff"]["added"]
        assert "mystery_btn" not in _policy(env)["perception"]["spec"]

    def test_added_valid_anchor_written(self, env):
        res = s.apply_save(_body(added={"template": {"new_title": {
            "kind": "template", "page": "bidding", "label": "新标题",
            "rect": [0.1, 0.1, 0.3, 0.2], "templates": ["smart_bid_btn.png"]}}}),
            dry_run=False)
        assert res["ok"] is True, res["report"]
        entry = _policy(env)["perception"]["spec"]["new_title"]
        assert entry["kind"] == "template" and entry["page"] == "bidding"
        assert res["diff"]["added"][0]["path"].endswith("new_title")


# ----------------------------------------------------------------------
# 引用检查（双口径）
# ----------------------------------------------------------------------


class TestReferenceGuard:
    def test_delete_referenced_blocked_with_names(self, env):
        res = s.apply_save(_body(deleted=["template.round_big_banner"]), dry_run=False)
        assert res["ok"] is False
        text = _errors(res)
        assert "round_big_banner" in text and "active" in text
        assert _bytes(env) == env["before"]

    def test_delete_missing_anchor_e12(self, env):
        res = s.apply_save(_body(deleted=["template.ghost_key"]), dry_run=False)
        assert res["ok"] is False
        assert "ghost_key" in _errors(res)

    def test_delete_nodes_group_forbidden(self, env):
        res = s.apply_save(_body(deleted=["nodes.x#0"]), dry_run=False)
        assert res["ok"] is False

    def test_actuators_reference_blocks_delete(self, env):
        """actuators 侧是 `treasure.` 前缀口径——同样必须拦住（双口径检查）。"""
        refs = s.find_references("appraiser_p1_caroline", s.read_policy(), s.read_pipelines())
        assert any(r.startswith("actuators.treasure.") for r in refs), refs
        res = s.apply_save(_body(deleted=["template.appraiser_p1_caroline"]), dry_run=False)
        assert res["ok"] is False
        assert "actuators" in _errors(res)

    def test_delete_unreferenced_anchor_ok(self, env):
        """找一个无任何引用的锚点：删除应成功且真的从 spec 移除。"""
        policy = s.read_policy()
        docs = s.read_pipelines()
        spec = policy["perception"]["spec"]
        free = [k for k in spec if not s.find_references(k, policy, docs)]
        if not free:
            pytest.skip("当前真源无「零引用」锚点")
        victim = free[0]
        group = spec[victim]["kind"]
        res = s.apply_save(_body(deleted=[f"{group}.{victim}"]), dry_run=False)
        assert res["ok"] is True, res["report"]
        assert victim not in _policy(env)["perception"]["spec"]


# ----------------------------------------------------------------------
# preview 与乐观锁
# ----------------------------------------------------------------------


class TestPreviewAndLock:
    def test_preview_does_not_write(self, env):
        proj = s.project_rois()
        proj["ocr"]["bid_main_btn_label"]["rect"] = [0.5, 0.5, 0.6, 0.6]
        res = s.apply_save({**proj, "added": {}, "deleted": [], "preview": True,
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=True)
        assert res["ok"] is True
        assert res["diff"]["changed"]
        assert res["compile"]["status"] == "skipped_no_routes"
        assert _bytes(env) == env["before"]

    def test_preview_reports_errors_without_raising(self, env):
        """preview 形态：校验失败回 ok=false（不抛），前端靠 ok 决定是否可保存。"""
        res = s.apply_save(_body(deleted=["template.round_big_banner"], preview=True),
                           dry_run=True)
        assert res["ok"] is False
        assert any("round_big_banner" in e for e in res["report"]["errors"])
        assert _bytes(env) == env["before"]

    def test_real_save_failure_does_not_write(self, env):
        res = s.apply_save(_body(deleted=["template.round_big_banner"]), dry_run=False)
        assert res["ok"] is False
        assert _bytes(env) == env["before"]

    def test_base_hash_conflict_409(self, env):
        with pytest.raises(s.SaveRejected) as ei:
            s.apply_save(_body(base_hash={"policy": "deadbeef", "treasure": "x", "entry": "y"}),
                         dry_run=False)
        assert ei.value.code == 409
        assert _bytes(env) == env["before"]

    def test_legacy_string_base_hash_conflict(self, env):
        with pytest.raises(s.SaveRejected) as ei:
            s.apply_save(_body(base_hash="deadbeef"), dry_run=False)
        assert ei.value.code == 409

    def test_matching_base_hash_passes(self, env):
        res = s.apply_save(_body(preview=True), dry_run=True)
        assert res["ok"] is True


# ----------------------------------------------------------------------
# 字段编辑语义
# ----------------------------------------------------------------------


class TestFieldEdits:
    def test_colorspace_set_and_clear(self, env):
        proj = s.project_rois()
        proj["template"]["smart_bid_btn"]["colorspace"] = "gray"
        s.apply_save({**proj, "added": {}, "deleted": [],
                      "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert _policy(env)["perception"]["spec"]["smart_bid_btn"]["colorspace"] == "gray"

        proj = s.project_rois()
        proj["template"]["smart_bid_btn"]["colorspace"] = ""
        res = s.apply_save({**proj, "added": {}, "deleted": [],
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert res["ok"] is True
        assert "colorspace" not in _policy(env)["perception"]["spec"]["smart_bid_btn"]

    def test_threshold_written_back(self, env):
        proj = s.project_rois()
        proj["template"]["smart_bid_btn"]["threshold"] = 0.75
        s.apply_save({**proj, "added": {}, "deleted": [],
                      "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert _policy(env)["perception"]["spec"]["smart_bid_btn"]["threshold"] == 0.75

    def test_bad_rect_reported_not_raised(self, env):
        proj = s.project_rois()
        proj["ocr"]["bid_main_btn_label"]["rect"] = [0.1, 0.2, 0.3]
        res = s.apply_save({**proj, "added": {}, "deleted": [],
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert res["ok"] is False
        assert "4 元数组" in _errors(res)
        assert _bytes(env) == env["before"]

    def test_tuning_rect_written(self, env):
        proj = s.project_rois()
        proj["tuning"]["appraiser_search_roi"]["rect"] = [0.04, 0.19, 0.96, 0.91]
        res = s.apply_save({**proj, "added": {}, "deleted": [],
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=False)
        assert res["ok"] is True
        assert _policy(env)["policy"]["tuning"]["perception"]["appraiser_search_roi"] == \
            [0.04, 0.19, 0.96, 0.91]


# ----------------------------------------------------------------------
# check_truth 三闸确实生效
# ----------------------------------------------------------------------


class TestTruthGates:
    def test_rect_out_of_range_caught_by_gate(self, env):
        """越界 rect 必须被 check_truth 的几何闸拦下（不是被结构校验挡）。"""
        proj = s.project_rois()
        proj["ocr"]["bid_main_btn_label"]["rect"] = [1.5, 0.2, 1.6, 0.3]
        res = s.apply_save({**proj, "added": {}, "deleted": [], "preview": True,
                            "base_hash": proj["_meta"]["base_hash"]}, dry_run=True)
        assert res["ok"] is False
        assert any("1.5" in e for e in res["report"]["errors"]), res["report"]["errors"]
        assert _bytes(env) == env["before"]

    def test_report_shape(self, env):
        res = s.apply_save(_body(preview=True), dry_run=True)
        assert isinstance(res["report"], dict)
        assert set(res["report"]) == {"errors", "warnings"}

    def test_gate_errors_are_from_check_truth(self, env):
        """三闸至少有一条真实错误路径：制造跨面引用断裂应被 cross_checks 抓到。"""
        proj = s.project_rois()
        # 直接改内存 spec：把某 stage 的 active 指向不存在锚点（模拟外部半途改动）
        policy = s.read_policy()
        defs = policy["perception"]["stages"]["definitions"]
        first = next(iter(defs.values()))
        first["active"] = list(first["active"]) + ["__ghost_anchor__"]
        ct = s._check_truth_module()
        errs = ct.cross_checks({}, policy)
        assert any("__ghost_anchor__" in e for e in errs)
