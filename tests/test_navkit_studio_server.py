# -*- coding: utf-8 -*-
"""ROI Studio 读面与服务契约（v4 重写基线：v3 `tools/navkit/server.py` 集成测试）。

覆盖：
- `GET /api/rois` flat 投影形状（spec 三组 20/15/18 + nodes 36 + tuning 1 + `_meta`）；
- 镜像锚点清单与「按 rect 值匹配」的实际判定一致（防清单漂移）；
- 两面 colorspace 不同值如实透传（不得静默归一）；
- `template_status` 命名锁定（unassigned / dangling）；
- 路由与静态服务安全（白名单扩展名 + 目录穿越拒绝）。

读面测试只读真源；服务测试起本机临时端口，不落盘、不依赖真实截图目录。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.navkit import studio_server as s

SERVER_SCRIPT = Path(s.__file__)

# ----------------------------------------------------------------------
# 读投影
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def proj():
    return s.project_rois()


class TestProjection:
    def test_spec_group_counts(self, proj):
        """分类取 kind：template 32 / point 15 / ocr 18（五分类制废除）。
        template 32 = 原 22 + 彩蛋收尾链专用锚点 9（不进 transitions，§10）
        + pass 二级确认弹窗惰性锚点 1。"""
        assert len(proj["template"]) == 32
        assert len(proj["point"]) == 15
        assert len(proj["ocr"]) == 18

    def test_nodes_and_tuning_counts(self, proj):
        """nodes = pipeline 两文件逐处 rect（28 处：起跑汇聚识别改按名引用后不再持有
        rect，原 36 减去 boot 的 11 处，再加待机/控制器弹窗链的 3 处图侧独有 rect）；
        tuning = 第 54 个 rect。"""
        assert len(proj["nodes"]) == 28
        assert len(proj["tuning"]) == 1
        assert "appraiser_search_roi" in proj["tuning"]

    def test_meta_shape(self, proj):
        meta = proj["_meta"]
        for key in ("match", "pages", "stage_anchors", "base_hash", "mirror_anchors"):
            assert key in meta, key
        assert set(meta["base_hash"]) == {"policy", "treasure", "entry"}
        # pages 与 stages.definitions 的 page 去重一致
        policy = s.read_policy()
        defs = policy["perception"]["stages"]["definitions"]
        assert meta["pages"] == sorted({d.get("page") for d in defs.values() if d.get("page")})
        # stage_anchors = global_anchors 原样
        assert meta["stage_anchors"] == list(policy["perception"]["stages"]["global_anchors"])

    def test_entry_fields_transparent(self, proj):
        """templates / colorspace / arbitration / _comment 透传显示。"""
        banner = proj["template"]["round_big_banner"]
        assert banner["templates"] == [f"round{i}_banner.png" for i in range(1, 6)]
        assert banner["colorspace"] == "gray"
        assert banner["arbitration"]["margin"] == 0.03
        assert "_comment" in banner
        smart = proj["template"]["smart_bid_btn"]
        # 缺省 = rgb，字段不落盘也不投影（不得凭空注入 colorspace）
        assert "colorspace" not in smart
        assert smart["order"] == 920

    def test_two_sides_colorspace_not_normalized(self, proj):
        """节点面 colorspace 逐处如实透传（有值原样带出，不替节点补默认、不做归一）。

        两面同图闸（check_truth.anchor_face_checks）已把 colorspace 锁成 error 级、
        盘上两面统一为「默认 gray，灰度拉不开差距才转 rgb」；本测试盯的是投影侧——
        台内一旦归一，人看到的和跑的就不是同一份。
        """
        cs = [v.get("colorspace") for v in proj["nodes"].values()]
        assert set(cs) <= {None, "gray", "rgb"}, cs
        assert sum(1 for c in cs if c == "gray") == 9   # 5 回合横幅 + 1 胜负横幅
        # + 3 处图侧独有规格（待机聊天框、控制器弹窗面板与 X）
        gray = {k for k, v in proj["template"].items() if v.get("colorspace") == "gray"}
        assert gray == {
            "appraiser_p1_caroline", "appraiser_p2_shotaro", "appraiser_selected_check",
            "session_start_match_btn", "round_big_banner", "result_banner",
            "hall_chat_left", "hall_controller_popup",
            # 彩蛋收尾链专用锚点（不进 transitions，§10）
            "egg_claim_title", "egg_panel_tabbar", "egg_task_tab3",
            "hall_back_btn", "hall_home_btn",
        }

    def test_nodes_key_shape_and_fields(self, proj):
        for key, item in proj["nodes"].items():
            node, sep, idx = key.rpartition("#")
            assert sep and idx.isdigit() and node
            assert len(item["rect"]) == 4
            assert item["file"] in ("treasure.json", "treasure.entry.json")
            assert item["node"] == node


class TestMirrorAnchors:
    def test_mirror_list_matches_actual_pairs(self, proj):
        """清单必须是「实际两面同值锚点」的精确集合——漂移即联动对象出错。"""
        spec = s.read_policy()["perception"]["spec"]
        dynamic = set()
        for item in proj["nodes"].values():
            for name, anchor in spec.items():
                if s._rect_key(anchor["rect"]) == s._rect_key(item["rect"]):
                    dynamic.add(name)
        assert dynamic == set(s.MIRROR_ANCHORS)
        assert len(s.MIRROR_ANCHORS) == 13

    def test_every_node_has_mirror(self, proj):
        """每处 rect 要么是镜像锚点的副本，要么显式声明了 _graph_only。

        _graph_only = 只在图侧跑的识别规格（页面/障碍态判据），检测面本就没有对应物，
        无从镜像；豁免靠声明，不靠"恰好查无同值 rect"。
        """
        graph_only = {n for doc in s.read_pipelines().values()
                      for n, d in doc.items()
                      if isinstance(d, dict) and (d.get("attach") or {}).get("_graph_only")}
        assert all(item["mirrors"] in s.MIRROR_ANCHORS or item["node"] in graph_only
                   for item in proj["nodes"].values())

    def test_mirror_lookup_uses_whitelist(self):
        """白名单外的同值锚点不算镜像（防偶然同值被误当联动对象）。"""
        spec = {"not_in_list": {"rect": [0.1, 0.2, 0.3, 0.4]}}
        assert s._mirror_of([0.1, 0.2, 0.3, 0.4], spec) is None


# ----------------------------------------------------------------------
# 模板状态 / 引用检查
# ----------------------------------------------------------------------


class TestTemplateStatus:
    def test_status_shape_and_naming(self):
        st = s.api_template_status()
        assert set(st) == {"listed", "referenced", "unassigned", "dangling"}
        assert st["unassigned"] == sorted(set(st["listed"]) - set(st["referenced"]))
        assert st["dangling"] == sorted(set(st["referenced"]) - set(st["listed"]))
        assert st["listed"] == s.api_list_templates()

    def test_list_templates_non_empty(self):
        assert len(s.api_list_templates()) > 0


class TestReferenceChecks:
    def test_referenced_anchor_reports_all_sides(self):
        """被引用锚点删除前必须能点名引用方（含 actuators 的 treasure. 前缀口径）。"""
        policy = s.read_policy()
        refs = s.find_references("round_big_banner", policy, s.read_pipelines())
        assert any("active" in r for r in refs)
        refs_act = s.find_references("appraiser_p1_caroline", policy, s.read_pipelines())
        assert any(r.startswith("actuators.treasure.") for r in refs_act)

    def test_unknown_name_has_no_refs(self):
        policy = s.read_policy()
        assert s.find_references("__no_such_anchor__", policy, s.read_pipelines()) == []


# ----------------------------------------------------------------------
# 服务与静态安全（本机临时端口，只读）
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def live():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


class TestRoutes:
    def test_shell_at_root(self, live):
        st, ct, body = _get(live + "/")
        assert st == 200 and "text/html" in ct
        assert all(t.encode("utf-8") in body for t in ("ROI 校准台", "策略表", "模板截取"))

    def test_three_pages(self, live):
        for path in ("/policy", "/roi", "/cropper"):
            st, ct, body = _get(live + path)
            assert st == 200, path
            assert "text/html" in ct

    def test_static_whitelist(self, live):
        for name in ("app.js", "history.js", "style.css", "shell.html"):
            st, _, _ = _get(live + "/static/" + name)
            assert st == 200, name

    @pytest.mark.parametrize("path", [
        "/static/../studio_server.py",
        "/static/../../pyproject.toml",
        "/static/app.py",
        "/static/nope.js",
    ])
    def test_static_rejects_escape_and_ext(self, live, path):
        st, _, _ = _get(live + path)
        assert st == 404, path

    def test_policy_api(self, live):
        st, ct, body = _get(live + "/api/policy")
        assert st == 200 and ct.startswith("application/json")
        assert len(json.loads(body)["policy"]["rules"]) == 24

    def test_rois_api(self, live):
        st, _, body = _get(live + "/api/rois")
        assert st == 200
        data = json.loads(body)
        assert len(data["nodes"]) == 28 and len(data["tuning"]) == 1

    def test_image_api_rejects_bad_session(self, live):
        st, _, _ = _get(live + "/api/image?session=../x&name=0001_raw.png")
        assert st == 404


# ----------------------------------------------------------------------
# 空闲退出（SSE 保活）：关掉页签即断连 → 延迟自动收摊
# ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def _spawn(port: int, idle: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT), "--port", str(port), "--idle-exit", str(idle)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"端口 {port} 未在 {timeout}s 内监听")


def _sse_connect(port: int) -> socket.socket:
    """建立 SSE 长连接并读掉响应头（保持 socket 打开 = 页面还活着）。"""
    sk = socket.create_connection(("127.0.0.1", port), timeout=10)
    sk.sendall(
        f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Accept: text/event-stream\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sk.recv(4096)
        if not chunk:
            raise AssertionError("SSE 响应头读取中断")
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0]
    assert b"200" in head.split(b"\r\n")[0], head[:200]
    assert b"text/event-stream" in head, head[:200]
    return sk


def _wait_exit(proc: subprocess.Popen, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.25)
    return False


class TestIdleExit:
    def test_no_page_never_exits(self):
        """从未有页面连过（脚本 / curl / CI 场景）→ 常驻，不被误杀。"""
        port = _free_port()
        proc = _spawn(port, idle=2)
        try:
            _wait_port(port)
            time.sleep(3.5)
            assert proc.poll() is None, "无页面连接时不应退出"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_exits_after_last_page_closes(self):
        """最后一个 SSE 连接断开后，超过 idle 秒自动退出。"""
        port = _free_port()
        proc = _spawn(port, idle=2)
        try:
            _wait_port(port)
            sk = _sse_connect(port)
            time.sleep(0.6)
            assert proc.poll() is None, "页面在线时不应退出"
            sk.close()
            assert _wait_exit(proc, timeout=15), "SSE 断开后未自动退出"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_reconnect_cancels_pending_exit(self):
        """idle 窗口内重连 → 取消退出计时。"""
        port = _free_port()
        proc = _spawn(port, idle=4)
        try:
            _wait_port(port)
            first = _sse_connect(port)
            time.sleep(0.4)
            first.close()
            time.sleep(1.5)                      # 仍在 4s 窗口内
            second = _sse_connect(port)
            try:
                time.sleep(4.5)                  # 越过原计时点
                assert proc.poll() is None, "重连后不应退出"
            finally:
                second.close()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_idle_exit_zero_is_persistent(self):
        """--idle-exit 0 → 即使连过页面也常驻。"""
        port = _free_port()
        proc = _spawn(port, idle=0)
        try:
            _wait_port(port)
            sk = _sse_connect(port)
            time.sleep(0.4)
            sk.close()
            time.sleep(2.5)
            assert proc.poll() is None, "--idle-exit 0 应常驻"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
