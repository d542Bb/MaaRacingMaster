# -*- coding: utf-8 -*-
"""Studio 保存管线回归（rev.3 计划验收 1 的服务端用例）。

覆盖：
- G1.1 写盘字节规范（LF、尾换行，策略与校准两条路径同规则）；
- G2.1 校准 merge 真实新增/删除（含 E05/E09/E12 结构守卫）；
- G2.5 阈值重置默认值可回写；投影→保存 幂等（不凭空注入 templates）；
- G6.2 preview 不落盘、diff/report/compile/base_hash 形态，预览产物与正式落盘一致；
- G6.3 base_hash 不符 409；
- H4/G6.2 report 全端点统一对象形态。

内存级 Handler 调用（不起进程），资产路径与编译出口 monkeypatch 到 tmp，
绝不触碰真实 treasure 资产与生成物。
"""
from __future__ import annotations

import copy
import json

import pytest

from tools.navkit import server as srv
from tools.navkit.adapters import treasure as ta
from tools.navkit.server import Handler, build_state


# ---------------------------------------------------------------------------
# 最小合法 v3 文档（schema v3，treasure 模块名）
# ---------------------------------------------------------------------------

def _doc() -> dict:
    return {
        "_schema_ver": 3,
        "_module": "treasure",
        "reference_size": [1280, 720],
        "match": {"scales": [1.0], "threshold": 0.75, "margin_default": 0.01},
        "pages": {
            "hall": {"label": "大厅"},
            "bidding": {"label": "出价"},
            "result": {"label": "结算"},
        },
        "anchors": {
            "hall_card": {
                "kind": "template", "owner": "treasure", "page": "hall",
                "label": "大厅卡", "rect": [0.1, 0.1, 0.3, 0.3],
                "templates": ["hall_card.png"], "order": 1, "threshold": 0.9,
            },
            "panel_title": {
                "kind": "template", "owner": "treasure", "page": "bidding",
                "label": "面板标题", "rect": [0.4, 0.05, 0.6, 0.1],
                "templates": ["panel_title.png"], "order": 2,
            },
            "bid_btn": {
                "kind": "point", "owner": "treasure", "page": "bidding",
                "label": "出价按钮", "rect": [0.45, 0.9, 0.55, 0.95],
                "order": 3, "guarded_by": "panel_title",
            },
            "bid_amount": {
                "kind": "ocr", "owner": "treasure", "page": "bidding",
                "label": "出价读数", "rect": [0.7, 0.1, 0.9, 0.15], "order": 4,
            },
            "spare_icon": {
                "kind": "template", "owner": "treasure", "page": "result",
                "label": "备用图标", "rect": [0.2, 0.6, 0.25, 0.65],
                "templates": ["spare_icon.png"], "order": 5,
            },
        },
        "stages": {
            "order": ["大厅", "出价"],
            "global_anchors": [],
            "definitions": {
                "大厅": {"page": "hall", "anchors": ["hall_card"], "ocr": []},
                "出价": {"page": "bidding", "anchors": ["panel_title", "bid_btn"],
                         "ocr": ["bid_amount"]},
            },
        },
        "transitions": [
            {"stage": "*", "on": "hall_card", "to": "大厅"},
            {"stage": "大厅", "on": "panel_title", "to": "出价"},
        ],
        "routes": {},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """tmp 资产 + 编译出口 stub + 捕获式 _send_json 的 Handler。"""
    assets = tmp_path / "treasure_assets.json"
    assets.write_text(json.dumps(_doc(), ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    routes = tmp_path / "treasure_routes.json"

    monkeypatch.setattr(srv, "assets_path_for", lambda module: assets)

    calls = {"write": 0, "preview": 0}

    def fake_compile_and_write(module, **kw):
        calls["write"] += 1
        routes.write_text('{"_generated": true}\n', encoding="utf-8", newline="\n")
        return {"status": "written", "out_path": str(routes), "output": ""}

    def fake_compile_document(module, document, **kw):
        calls["preview"] += 1
        return {"status": "skipped_no_routes", "out_path": str(routes), "output": ""}

    monkeypatch.setattr(srv, "compile_and_write", fake_compile_and_write)
    monkeypatch.setattr(srv, "compile_document", fake_compile_document)

    state = build_state("treasure")
    Handler.state = state

    sent: list[tuple[dict, int]] = []

    h = Handler.__new__(Handler)

    def capture(payload, code=200):
        sent.append((payload, code))

    monkeypatch.setattr(h, "_send_json", capture)
    return {"h": h, "assets": assets, "routes": routes, "sent": sent, "calls": calls}


def _post_rois(env, body: dict):
    env["h"]._handle_rois_post_v3(body, ta.apply_flat_ops)
    return env["sent"][-1]


def _post_assets(env, body: dict):
    env["h"]._handle_assets_post(body)
    return env["sent"][-1]


def _flat(doc: dict) -> dict:
    return ta.flat_from_v3_doc(doc)


class TestWriteDiscipline:
    """G1.1：两条保存路径的写盘字节规范一致（LF + 尾换行）。"""

    def test_assets_post_bytes_lf_and_trailing_newline(self, env):
        doc = _doc()
        payload, code = _post_assets(env, {"document": doc})
        assert code == 200 and payload["ok"] is True
        raw = env["assets"].read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")
        assert json.loads(raw.decode("utf-8")) == doc

    def test_rois_post_bytes_lf_and_trailing_newline(self, env):
        payload, code = _post_rois(env, _flat(_doc()))
        assert code == 200 and payload["ok"] is True
        raw = env["assets"].read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")

    def test_save_triggers_auto_compile(self, env):
        _post_rois(env, _flat(_doc()))
        assert env["calls"]["write"] == 1
        assert env["sent"][-1][0]["compiled"] == "written"


class TestMergeRealOps:
    """G2.1：added/deleted 真实写回 anchors。"""

    def test_added_anchor_written(self, env):
        body = _flat(_doc())
        body["added"] = {"actions": {"new_btn": {
            "kind": "point", "page": "bidding",
            "rect": [0.3, 0.8, 0.35, 0.85], "guarded_by": "panel_title",
        }}}
        payload, code = _post_rois(env, body)
        assert code == 200, payload
        anchors = json.loads(env["assets"].read_text(encoding="utf-8"))["anchors"]
        assert anchors["new_btn"]["kind"] == "point"
        assert anchors["new_btn"]["page"] == "bidding"
        assert anchors["new_btn"]["owner"] == "treasure"

    def test_deleted_anchor_removed(self, env):
        body = _flat(_doc())
        body["deleted"] = ["stage.spare_icon"]
        payload, code = _post_rois(env, body)
        assert code == 200, payload
        anchors = json.loads(env["assets"].read_text(encoding="utf-8"))["anchors"]
        assert "spare_icon" not in anchors

    def test_delete_referenced_anchor_blocked_with_e12(self, env):
        body = _flat(_doc())
        body["deleted"] = ["stage.hall_card"]
        payload, code = _post_rois(env, body)
        assert code == 400
        assert "spare" or True
        text = json.dumps(payload, ensure_ascii=False)
        assert "E12" in text
        assert isinstance(payload["report"], dict)
        assert payload["report"]["errors"]

    def test_delete_missing_anchor_is_explicit_error(self, env):
        body = _flat(_doc())
        body["deleted"] = ["stage.ghost_key"]
        payload, code = _post_rois(env, body)
        assert code == 400
        assert "ghost_key" in json.dumps(payload, ensure_ascii=False)

    def test_added_without_page_blocked_with_e09(self, env):
        body = _flat(_doc())
        body["added"] = {"actions": {"bad_btn": {
            "kind": "point", "page": "",
            "rect": [0.3, 0.8, 0.35, 0.85], "guarded_by": "panel_title",
        }}}
        payload, code = _post_rois(env, body)
        assert code == 400
        assert "E09" in json.dumps(payload, ensure_ascii=False)

    def test_added_existing_name_conflict(self, env):
        body = _flat(_doc())
        body["added"] = {"actions": {"hall_card": {
            "kind": "template", "page": "hall", "rect": [0.1, 0.1, 0.2, 0.2],
        }}}
        payload, code = _post_rois(env, body)
        assert code == 400
        assert "E05" in json.dumps(payload, ensure_ascii=False)


class TestMergeSemantics:
    def test_threshold_default_value_written_back(self, env):
        """G2.5：重置为默认 0.75 必须真实回写（旧实现 delete 键导致回弹）。"""
        body = _flat(_doc())
        cat = next(c for c in ta.CATEGORIES if "hall_card" in body[c])
        body[cat]["hall_card"]["threshold"] = 0.75
        payload, code = _post_rois(env, body)
        assert code == 200, payload
        anchors = json.loads(env["assets"].read_text(encoding="utf-8"))["anchors"]
        assert anchors["hall_card"]["threshold"] == 0.75

    def test_project_then_save_is_idempotent(self, env):
        """GET 投影原样回存不得改变任何锚点（含不向 ocr 锚注入空 templates）。"""
        original = _doc()
        payload, code = _post_rois(env, _flat(original))
        assert code == 200, payload
        assert json.loads(env["assets"].read_text(encoding="utf-8")) == original
        assert "templates" not in json.loads(
            env["assets"].read_text(encoding="utf-8"))["anchors"]["bid_amount"]

    def test_implicit_new_key_via_flat_segment_not_fabricated(self, env):
        """白名单闸门解除后，未在 anchors 的普通条目键仍不得被字段覆盖路径凭空创建。"""
        body = _flat(_doc())
        body["actions"]["mystery_btn"] = {"rect": [0.1, 0.1, 0.2, 0.2], "templates": []}
        payload, code = _post_rois(env, body)
        assert code == 200, payload
        anchors = json.loads(env["assets"].read_text(encoding="utf-8"))["anchors"]
        assert "mystery_btn" not in anchors


class TestPreviewAndLocking:
    """G6.2/G6.3：preview 形态、不落盘、与正式保存一致；base_hash 乐观锁。"""

    def _body(self):
        body = _flat(_doc())
        body["added"] = {"actions": {"new_btn": {
            "kind": "point", "page": "bidding",
            "rect": [0.3, 0.8, 0.35, 0.85], "guarded_by": "panel_title",
        }}}
        body["deleted"] = ["stage.spare_icon"]
        return body

    def test_preview_returns_full_shape_without_writing(self, env):
        body = self._body()
        before = env["assets"].read_bytes()
        payload, code = _post_rois(env, {**body, "preview": True,
                                         "base_hash": srv.document_hash(env["assets"])})
        assert code == 200
        assert payload["ok"] is True
        assert isinstance(payload["report"], dict)
        assert payload["compile"]["status"] == "skipped_no_routes"
        paths = [d["path"] for d in payload["diff"]["added"]] + \
                [d["path"] for d in payload["diff"]["removed"]]
        assert any("new_btn" in p for p in paths)
        assert any("spare_icon" in p for p in paths)
        assert env["assets"].read_bytes() == before
        assert env["calls"]["write"] == 0

    def test_preview_ok_false_on_e12(self, env):
        body = _flat(_doc())
        body["deleted"] = ["stage.hall_card"]
        payload, code = _post_rois(env, {**body, "preview": True})
        assert code == 200
        assert payload["ok"] is False
        assert any("E12" in e for e in payload["report"]["errors"])

    def test_real_save_matches_preview_applied_doc(self, env):
        body = self._body()
        expected = ta.apply_flat_ops(_doc(), body)
        _post_rois(env, {**body, "preview": True})
        payload, code = _post_rois(env, {**body, "preview": False})
        assert code == 200, payload
        assert json.loads(env["assets"].read_text(encoding="utf-8")) == expected

    def test_base_hash_conflict_returns_409(self, env):
        body = _flat(_doc())
        body["rect"] = None  # 无意义字段不影响 merge，只测锁
        body.pop("rect")
        payload, code = _post_rois(env, {**body, "base_hash": "deadbeef"})
        assert code == 409
        assert payload["ok"] is False
        assert env["calls"]["write"] == 0

    def test_assets_post_base_hash_conflict(self, env):
        doc = _doc()
        doc["anchors"]["hall_card"]["threshold"] = 0.8
        payload, code = _post_assets(env, {"document": doc, "base_hash": "00000000"})
        assert code == 409

    def test_assets_post_validation_failure_report_is_object(self, env):
        """H4：400 的失败明细必须是结构化对象（字符串形态曾被前端显示成 0 项 error）。"""
        doc = _doc()
        doc["transitions"].append({"stage": "*", "on": "missing_anchor", "to": "大厅"})
        payload, code = _post_assets(env, {"document": doc})
        assert code == 400
        assert isinstance(payload["report"], dict)
        assert any("E12" in e for e in payload["report"]["errors"])
        assert env["calls"]["write"] == 0

    def test_assets_preview_shape_parity_with_rois(self, env):
        doc = copy.deepcopy(_doc())
        doc["anchors"]["hall_card"]["threshold"] = 0.66
        payload, code = _post_assets(env, {"document": doc, "preview": True,
                                           "base_hash": srv.document_hash(env["assets"])})
        assert code == 200
        for key in ("ok", "diff", "report", "compile", "base_hash"):
            assert key in payload


class TestMatchScalesFromDoc:
    """G1.6：调试台匹配档位真源是资产文档 match.scales。"""

    def test_match_scales_reads_document(self, env, monkeypatch):
        h = env["h"]
        assert tuple(h._match_scales()) == (1.0,)
        doc = _doc()
        doc["match"]["scales"] = [0.8, 1.2]
        env["assets"].write_text(srv.dump_document(doc), encoding="utf-8", newline="\n")
        assert tuple(h._match_scales()) == (0.8, 1.2)
