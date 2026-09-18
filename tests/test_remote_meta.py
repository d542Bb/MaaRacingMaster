# -*- coding: utf-8 -*-
"""远程元数据信任边界的回归锁（公告 / 版本标记 / 外链目标）。

**判据（为什么这么测）**：公告与版本标记来自 CDN / 镜像，属不可信输入；本模块是
「这些字段能不能被采信」的唯一判据处，因此按字段逐条锁：

- 必填字段坏 → 整条作废；可选字段坏 → 丢字段（口径写在 `remote_meta` 模块头）；
- 动作型字段（`url` / `download_url`）不合规必须被清空——它是「可信程序替谁打开浏览器」
  的开关，且这条路径不依赖 XSS 就能成立；
- 形状合规但**内容像 HTML** 的文本必须原样放行：防 XSS 靠渲染层 `textContent`，不靠
  内容过滤。在这里加一道「过滤尖括号」的假防线，只会掩盖真防线失效（真防线由
  `tests/test_frontend_remote_render.py` 的静态锁看护）。

本文件只依赖标准库（含被测模块），因此在 CI 的轻量依赖集下始终真跑，不 SKIP。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maaracing_master.core.remote_meta import (
    EXTERNAL_TARGETS,
    is_date,
    is_expired,
    is_official_url,
    is_openable_url,
    parse_announcement,
    parse_release,
)

REPO = Path(__file__).resolve().parent.parent
ANNOUNCEMENT = REPO / "docs" / "announcement.json"
LATEST_RELEASE = REPO / "docs" / "latest_release.json"

VALID_ANNOUNCEMENT = {
    "id": "2026-09-01-v2",
    "level": "info",
    "title": "巅峰鉴宝全链路已上线",
    "body": "第一行\n第二行",
    "date": "2026-09-01",
    "url": "https://github.com/d542Bb/MaaRacingMaster/releases",
    "url_text": "查看发布说明",
    "effective_until": "2026-09-30",
}

VALID_RELEASE = {
    "tag": "v0.23.0-dev.1",
    "version": "0.23.0-dev.1",
    "published_at": "2026-09-15",
    "download_url": "https://cnb.cool/MaaRacingMaster/MAIN/-/releases",
}


# --------------------------------------------------------------------------
# 官方地址白名单
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://github.com/d542Bb/MaaRacingMaster",
    "https://github.com/d542Bb/MaaRacingMaster/releases",
    "https://github.com/d542Bb/MaaRacingMaster/blob/master/docs/CODE_WIKI.md",
    "https://cnb.cool/MaaRacingMaster/MAIN/-/releases",
])
def test_official_url_accepts_project_pages(url):
    assert is_official_url(url)


@pytest.mark.parametrize("url", [
    "",                                                          # 空
    "github.com/d542Bb/MaaRacingMaster",                          # 无 scheme
    "http://github.com/d542Bb/MaaRacingMaster",                   # 非 https
    "javascript:alert(1)",
    "file:///C:/Windows/System32/calc.exe",
    "https://github.com/attacker/evil",                           # 同主机、非本项目路径
    "https://github.com.evil.tld/d542Bb/MaaRacingMaster",         # 主机名后缀伪装
    "https://github.com@evil.tld/d542Bb/MaaRacingMaster",         # userinfo 伪装
    "https://github.com:8443/d542Bb/MaaRacingMaster",             # 非默认端口
    "https://cnb.cool/MaaRacingMaster-evil/x",                    # 同前缀异路径（段边界）
    "https://raw.githubusercontent.com/d542Bb/MaaRacingMaster/master/x.json",  # 非白名单主机
    "https://evil.tld/https://github.com/d542Bb/MaaRacingMaster",
])
def test_official_url_rejects_lookalikes(url):
    assert not is_official_url(url)


def test_external_targets_are_openable():
    """静态目标表里的每个地址都必须过最终放行判据（否则按钮点了打不开）。"""
    assert EXTERNAL_TARGETS
    for target, url in EXTERNAL_TARGETS.items():
        assert is_openable_url(url), target


def test_openable_accepts_vigembus_target_only_as_static_target():
    """ViGEmBus 下载页是第三方仓库地址：只允许作为**静态目标**打开，不得作为远程数据
    携带的链接被放行（否则远程数据可以拿它当跳板把白名单撑开）。"""
    vigem = EXTERNAL_TARGETS["vigembus"]
    assert is_openable_url(vigem)
    assert not is_official_url(vigem)


# --------------------------------------------------------------------------
# 公告
# --------------------------------------------------------------------------


def _announcement(**overrides):
    payload = dict(VALID_ANNOUNCEMENT)
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def test_valid_announcement_normalized():
    ann = parse_announcement(VALID_ANNOUNCEMENT)
    assert ann is not None
    assert ann["title"] == VALID_ANNOUNCEMENT["title"]
    assert ann["url"] == VALID_ANNOUNCEMENT["url"]


@pytest.mark.parametrize("payload", [
    ["not", "a", "dict"],                                        # 顶层不是对象
    "2026-09-01",                                                # 顶层是字符串
    _announcement(level=None),                                   # 缺 level
    _announcement(level="urgent"),                               # level 值域外
    _announcement(title=None),                                   # 缺 title
    _announcement(title=""),                                     # 空 title
    _announcement(title=["x"]),                                  # title 非字符串
    _announcement(title="字" * 201),                             # title 超上界
    _announcement(date=None),
    _announcement(date="2026/09/01"),                            # 日期形状错
    _announcement(date="20260901"),
    _announcement(date="2026-02-30"),                            # 形状对但不是真日期
    _announcement(effective_until=None),                         # 必填字段缺失
    _announcement(effective_until="2026/09/30"),
    _announcement(id=None),
])
def test_announcement_invalid_required_field_voids_entry(payload):
    assert parse_announcement(payload) is None


def test_announcement_bad_url_drops_link_keeps_notice():
    """可选字段坏 = 丢字段：公告正文仍然可信，没有连坐作废的理由。"""
    ann = parse_announcement(_announcement(url="javascript:alert(1)"))
    assert ann is not None
    assert ann["url"] == ""
    assert ann["title"] == VALID_ANNOUNCEMENT["title"]


@pytest.mark.parametrize("bad_url", [
    "https://evil.tld/payload",
    "http://github.com/d542Bb/MaaRacingMaster",   # 非 https
    "https://github.com/attacker/evil",
])
def test_announcement_off_allowlist_url_dropped(bad_url):
    assert parse_announcement(_announcement(url=bad_url))["url"] == ""


def test_announcement_oversized_optional_fields_dropped():
    ann = parse_announcement(_announcement(body="字" * 4001, url_text="字" * 61))
    assert ann["body"] == ""
    assert ann["url_text"] == "查看详情"


def test_announcement_missing_url_text_defaults():
    assert parse_announcement(_announcement(url_text=None))["url_text"] == "查看详情"


@pytest.mark.parametrize("payload_text", [
    '<img src=x onerror=alert(1)>',
    '<script>mra.call("open_external_url",{target:"home"})</script>',
    '"><svg onload=alert(1)>',
])
def test_announcement_html_like_text_passes_through_verbatim(payload_text):
    """内容像 HTML 不是本层的判据——本层只管形状；渲染层用 textContent 消化它。
    若这里开始过滤尖括号，等于把防线搬到错的地方，且会让真防线失效时无人察觉。"""
    ann = parse_announcement(_announcement(title=payload_text))
    assert ann is not None and ann["title"] == payload_text


# --------------------------------------------------------------------------
# 版本标记
# --------------------------------------------------------------------------


def test_valid_release_normalized():
    rel = parse_release(VALID_RELEASE)
    assert rel == {
        "tag": "0.23.0-dev.1",
        "published_at": "2026-09-15",
        "download_url": "https://cnb.cool/MaaRacingMaster/MAIN/-/releases",
    }


def test_release_reads_github_api_field_names():
    """GitHub API 用 tag_name / assets，字段名不同但语义同源，归一必须等价。"""
    rel = parse_release({"tag_name": "v1.2.3", "published_at": "2026-01-02T03:04:05Z"})
    assert rel == {"tag": "1.2.3", "published_at": "2026-01-02", "download_url": ""}


@pytest.mark.parametrize("payload", [
    ["not", "a", "dict"],
    {},
    {"tag": ""},
    {"tag": "v"},                                        # 去 v 后为空
    {"tag": '<img src=x onerror=alert(1)>'},             # 非版本号形状
    {"tag": "0.23.0 已发布"},                             # 混入自由文本
    {"tag": "字" * 33},                                  # 超上界
])
def test_release_bad_tag_marks_source_unusable(payload):
    """tag 不合规 = 该源不可用（调用方继续 fallback），而不是渲染一个可疑版本号。"""
    assert parse_release(payload) is None


@pytest.mark.parametrize("bad_url", [
    "https://evil.tld/setup.exe",
    "http://cnb.cool/MaaRacingMaster/MAIN/-/releases",
    "https://github.com/attacker/evil/releases",
])
def test_release_off_allowlist_download_url_cleared(bad_url):
    """清空 → 调用方回退官方 release 页；绝不放行远程指定的第三方站点。"""
    rel = parse_release({"tag": "1.0.0", "download_url": bad_url})
    assert rel is not None and rel["download_url"] == ""


def test_release_bad_published_at_omitted_without_voiding_source():
    """发布日期只作展示：格式不对就省略，不牵连整条更新提示。"""
    rel = parse_release({"tag": "1.0.0", "published_at": "not-a-date"})
    assert rel is not None and rel["published_at"] == ""


# --------------------------------------------------------------------------
# 日期与过期
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("2026-09-18", True),
    ("2026-02-30", False),
    ("2026-9-8", False),
    ("20260918", False),
    ("", False),
])
def test_is_date(value, expected):
    assert is_date(value) is expected


@pytest.mark.parametrize("until,today,expected", [
    ("2026-09-17", "2026-09-18", True),
    ("2026-09-18", "2026-09-18", False),   # 当天仍显示
    ("2026-09-19", "2026-09-18", False),
])
def test_is_expired(until, today, expected):
    assert is_expired(until, today) is expected


# --------------------------------------------------------------------------
# 仓库内实际投放的数据必须过校验
# --------------------------------------------------------------------------


def test_shipped_announcement_passes_validator():
    """锁住「校验器收紧后线上公告静默消失」这类事故：仓库里的公告必须能过校验。

    公告被清空（写成 `{}`）是合法的「撤下公告」状态，此时跳过本断言。
    """
    if not ANNOUNCEMENT.exists():
        pytest.skip("公告文件不存在（已撤下）")
    data = json.loads(ANNOUNCEMENT.read_text(encoding="utf-8"))
    if not data:
        pytest.skip("公告为空对象（已撤下）")
    assert parse_announcement(data) is not None, f"{ANNOUNCEMENT} 不符合公告 schema"


def test_shipped_release_marker_passes_validator():
    if not LATEST_RELEASE.exists():
        pytest.skip("版本标记文件不存在（尚未发版）")
    data = json.loads(LATEST_RELEASE.read_text(encoding="utf-8"))
    if not data:
        pytest.skip("版本标记为空对象")
    assert parse_release(data) is not None, f"{LATEST_RELEASE} 不符合版本标记 schema"
