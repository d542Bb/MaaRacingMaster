# -*- coding: utf-8 -*-
"""外链 RPC 与远程元数据接入 sidecar 后的行为锁（端到端到「谁被打开」为止）。

**为什么测到这一层**：静态锁只能证明写法没退回旧样式；本文件直接跑 handler，断言
「构造出的恶意数据最终有没有把某个 URL 交给浏览器」——这正是 issue 的验收项
（XSS 场景不得借助 `open_external_url` / `open_vigembus_download` 打开攻击者 URL）。

构造方式沿用 `test_rpc_allowlist.py`：`__new__` 绕过 `__init__`（避免拉起控制器），
只注入本文件用到的最小状态；`_http_get` 与 `webbrowser.open` 都是运行期查找，替换即生效。

重依赖缺失的机器整文件 SKIP（同 rpc_allowlist 口径）。
"""
from __future__ import annotations

import json
import threading
import webbrowser
from urllib.error import URLError

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.remote_meta import EXTERNAL_TARGETS
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")

RELEASE_FALLBACK = EXTERNAL_TARGETS["release"]

GOOD_ANNOUNCEMENT = {
    "id": "2026-09-01-v2",
    "level": "info",
    "title": "正常公告",
    "body": "正文",
    "date": "2026-09-01",
    "url": "https://github.com/d542Bb/MaaRacingMaster/releases",
    "url_text": "查看详情",
    "effective_until": "2099-12-31",
}


def _service(bodies: dict[str, str]):
    """只注入本文件用到的最小状态；bodies 为 URL → 响应体。

    未登记的源抛 URLError —— 与真实网络失败同型（handler 只吞网络/解析类异常，
    抛别的类型会直接穿透 handler，测出来的是测试脚本的错而不是被测代码的行为）。
    """
    def _fake_get(url: str) -> str:
        if url not in bodies:
            raise URLError("offline")
        return bodies[url]

    svc = sc.SidecarService.__new__(sc.SidecarService)
    svc._lock = threading.RLock()
    svc._remote_urls = {}
    svc._http_get = _fake_get
    return svc


@pytest.fixture
def opened(monkeypatch):
    """记录「被交给浏览器」的地址；有记录即等价于浏览器真的开了这个站。"""
    seen: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: seen.append(url))
    return seen


# --------------------------------------------------------------------------
# 更新检查：远程给的下载页
# --------------------------------------------------------------------------


def test_check_update_drops_offsite_download_url():
    svc = _service({sc.SidecarService._RELEASE_URLS[0]: json.dumps({
        "tag": "v9.9.9", "published_at": "2026-09-15",
        "download_url": "https://evil.tld/setup.exe",
    })})
    ok, data, err = svc.check_update({})
    assert ok and err is None
    assert data["latest_tag"] == "9.9.9"
    assert data["download_url"] == RELEASE_FALLBACK
    assert svc._remote_urls["download"] == RELEASE_FALLBACK


def test_check_update_keeps_official_download_url():
    """官方域（CNB 镜像）仍要放行，否则国内下载入口被这次修复顺手砍掉。"""
    cnb = "https://cnb.cool/MaaRacingMaster/MAIN/-/releases"
    svc = _service({sc.SidecarService._RELEASE_URLS[0]: json.dumps(
        {"tag": "v9.9.9", "download_url": cnb})})
    assert svc.check_update({})[1]["download_url"] == cnb


def test_check_update_falls_back_to_next_source_on_bad_tag():
    """校验失败 = 该源不可用：继续走下一个源，而不是渲染可疑版本号。"""
    urls = sc.SidecarService._RELEASE_URLS
    svc = _service({
        urls[0]: json.dumps({"tag": '<img src=x onerror=alert(1)>'}),
        urls[1]: json.dumps({"tag": "v1.2.3"}),
    })
    ok, data, _ = svc.check_update({})
    assert ok and data["latest_tag"] == "1.2.3"


def test_check_update_reports_network_failure_when_no_source_usable():
    svc = _service({})
    ok, data, _ = svc.check_update({})
    assert ok and data["status"] == "network" and data["error"] and not data["has_update"]


# --------------------------------------------------------------------------
# 公告：多源 fallback 与过期
# --------------------------------------------------------------------------


def test_fetch_announcement_skips_invalid_source_and_uses_next():
    urls = sc.SidecarService._ANNOUNCEMENT_URLS
    svc = _service({
        urls[0]: json.dumps({"title": "缺 level 等必填字段"}),
        urls[1]: json.dumps(GOOD_ANNOUNCEMENT),
    })
    ok, data, err = svc.fetch_announcement({})
    assert ok and err is None
    assert data["title"] == "正常公告"
    assert svc._remote_urls["announcement"] == GOOD_ANNOUNCEMENT["url"]


def test_fetch_announcement_skips_expired_source():
    urls = sc.SidecarService._ANNOUNCEMENT_URLS
    svc = _service({
        urls[0]: json.dumps({**GOOD_ANNOUNCEMENT, "effective_until": "2000-01-01"}),
        urls[1]: json.dumps({**GOOD_ANNOUNCEMENT, "title": "未过期公告"}),
    })
    assert svc.fetch_announcement({})[1]["title"] == "未过期公告"


def test_fetch_announcement_all_sources_invalid_reports_none():
    svc = _service({sc.SidecarService._ANNOUNCEMENT_URLS[0]: json.dumps(["不是对象"])})
    ok, data, err = svc.fetch_announcement({})
    assert ok and err is None and data["level"] == "none"


def test_fetch_announcement_offsite_url_cleared_before_reaching_frontend():
    urls = sc.SidecarService._ANNOUNCEMENT_URLS
    svc = _service({urls[0]: json.dumps(
        {**GOOD_ANNOUNCEMENT, "url": "https://evil.tld/phish"})})
    ok, data, _ = svc.fetch_announcement({})
    assert ok and data["url"] == ""
    assert svc._remote_urls["announcement"] == ""


# --------------------------------------------------------------------------
# 外链 RPC：谁能被打开
# --------------------------------------------------------------------------


def test_open_external_url_opens_only_known_targets(opened):
    svc = _service({})
    for target in ("home", "issue", "docs", "release"):
        ok, data, err = svc.open_external_url({"target": target})
        assert ok and err is None
        assert data["opened"] == EXTERNAL_TARGETS[target]
    assert opened == [EXTERNAL_TARGETS[t] for t in ("home", "issue", "docs", "release")]


def test_open_external_url_rejects_caller_supplied_url(opened):
    """旧调用形态（直接传 url）必须彻底失效——这是「可信程序替攻击者开站」的入口。"""
    svc = _service({})
    ok, data, err = svc.open_external_url({"url": "https://evil.tld/phish"})
    assert not ok and data is None and "外部目标" in err
    assert opened == []


@pytest.mark.parametrize("params", [
    {}, {"target": "evil"}, {"target": "announcement"},   # 未取到公告时无地址可回退
    {"target": 123}, {"target": "https://evil.tld"},
])
def test_open_external_url_rejects_unknown_or_unavailable_target(opened, params):
    svc = _service({})
    ok, data, err = svc.open_external_url(params)
    assert not ok and data is None
    assert opened == []


def test_open_external_url_announcement_uses_validated_address(opened):
    """公告详情按钮：前端只报目标名，地址取自已校验的远程数据。"""
    urls = sc.SidecarService._ANNOUNCEMENT_URLS
    svc = _service({urls[0]: json.dumps(GOOD_ANNOUNCEMENT)})
    svc.fetch_announcement({})
    ok, data, err = svc.open_external_url({"target": "announcement"})
    assert ok and err is None
    assert data["opened"] == GOOD_ANNOUNCEMENT["url"]


def test_open_external_url_download_falls_back_to_release_page(opened):
    """未跑过更新检查（或远程没给合规地址）时，下载按钮回退官方 release 页。"""
    svc = _service({})
    ok, data, _ = svc.open_external_url({"target": "download"})
    assert ok and data["opened"] == RELEASE_FALLBACK


def test_open_vigembus_download_ignores_caller_url(opened):
    svc = _service({})
    ok, data, err = svc.open_vigembus_download({"url": "https://evil.tld/driver.exe"})
    assert ok and err is None
    assert data["opened"] == EXTERNAL_TARGETS["vigembus"]
    assert opened == [EXTERNAL_TARGETS["vigembus"]]
