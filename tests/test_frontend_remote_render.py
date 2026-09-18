# -*- coding: utf-8 -*-
"""「远程数据 → WebView → RPC」信任边界的静态锁。

**为什么是静态锁**：前端没有 DOM 测试设施（无 node 测试链），但这条边界的回归是
**可机检**的——远程字段名与 `innerHTML` 出现在同一处、外链 RPC 又收回了调用方传的
URL，这两件事都能从源码文本判定。因此不引第三方依赖，直接用源码断言锁死。

**局限（必须写明，免得当成运行时保证）**：正则/切片只能证明「写法没退回旧样式」，
不能证明运行时行为。真正的结构保证来自两处实现本身——渲染走 `textContent`（DOM API
天然不解析标记），地址走 sidecar 白名单。本文件的价值是让「有人又把它写回去」在 CI 即红。

**空过防护**：每处抽取都断言抽到锚点串，抽取规则失效时测试报错而不是静默通过。
"""
from __future__ import annotations

import re
from pathlib import Path

from maaracing_master.core.remote_meta import EXTERNAL_TARGETS

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "app.js"
INDEX_HTML = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend" / "index.html"
SIDECAR = REPO / "maaracing_master" / "core" / "sidecar.py"

# 来自远程元数据的字段名（公告 / 版本标记）。它们一律不得参与 HTML 拼接。
REMOTE_FIELDS = (
    "d.title", "d.date", "d.url_text", "d.url", "d.body",
    "d.latest_tag", "d.published_at", "d.download_url", "d.error",
)

# 外链 RPC 可接受的逻辑目标名：静态目标表 + 两个「取自已校验远程数据」的目标
TARGET_NAMES = set(EXTERNAL_TARGETS) | {"announcement", "download"}


def _js_function_body(src: str, signature: str) -> str:
    """按大括号配对取出 JS 函数体（含首尾大括号）。

    起始大括号必须落在签名同一行——否则会误取函数体里第一个字符串中的 `{`。
    """
    start = src.index(signature)
    line_end = src.index("\n", start)
    brace = src.index("{", start)
    assert brace < line_end, f"{signature} 的开体大括号不在签名行，抽取规则已失效"
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError(f"找不到 {signature} 的函数体结尾（源码结构变了？）")


def _python_function_body(src: str, signature: str) -> str:
    """按缩进取出 Python 函数体（Python 没有大括号，只能靠缩进定界）。"""
    lines = src.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != signature:
            continue
        indent = len(line) - len(line.lstrip())
        body = []
        for following in lines[index + 1:]:
            if following.strip() and (len(following) - len(following.lstrip())) <= indent:
                break
            body.append(following)
        return "\n".join(body)
    raise AssertionError(f"找不到 {signature}（源码结构变了？）")


def test_render_paths_contain_no_innerHTML():
    """公告与更新两处渲染不得使用 innerHTML —— 这是本 issue 的核心回归锁。

    （文件里其他 innerHTML 属本地数据渲染，不在本锁范围；远程渲染路径上唯一允许的
    innerHTML 是 appendIcon，它只喂本仓库静态 SVG 串。）
    """
    src = APP_JS.read_text(encoding="utf-8")
    cases = (
        ("async function checkUpdate()", "'check_update'"),
        ("async function fetchAnnouncement()", "'fetch_announcement'"),
    )
    for signature, anchor in cases:
        body = _js_function_body(src, signature)
        assert anchor in body and len(body) > 300, f"{signature} 抽取结果可疑，静态锁会空过"
        assert "innerHTML" not in body, (
            f"{signature} 又用 innerHTML 渲染了：远程字段必须走 textContent / DOM API"
        )


def test_no_remote_field_interpolated_into_html():
    """全文件兜底：任何一处 innerHTML 都不得与远程字段出现在同一行。"""
    offenders = []
    for lineno, line in enumerate(APP_JS.read_text(encoding="utf-8").splitlines(), 1):
        if "innerHTML" in line and any(f in line for f in REMOTE_FIELDS):
            offenders.append(f"app.js:{lineno}: {line.strip()}")
    assert not offenders, "远程字段进了 HTML 拼接：" + "; ".join(offenders)


def test_open_external_url_callers_pass_target_not_url():
    """前端调用外链 RPC 时只能给逻辑目标名，不得再传 URL。"""
    src = APP_JS.read_text(encoding="utf-8")
    calls = re.findall(r"mra\.call\(\s*'open_external_url'\s*,\s*\{([^}]*)\}", src)
    assert calls, "未抽到任何 open_external_url 调用点，静态锁会空过"
    for args in calls:
        assert "target" in args, f"open_external_url 调用未给逻辑目标名: {{{args.strip()}}}"
        assert "url" not in args, f"open_external_url 调用又传了 URL: {{{args.strip()}}}"

    vigem = re.findall(r"mra\.call\(\s*'open_vigembus_download'\s*,\s*\{([^}]*)\}", src)
    assert vigem, "未抽到 open_vigembus_download 调用点，静态锁会空过"
    for args in vigem:
        assert "url" not in args, f"open_vigembus_download 调用又传了 URL: {{{args.strip()}}}"


def test_frontend_target_names_exist_in_sidecar_map():
    """前端用到的目标名必须都在 sidecar 的目标表里（含 data-link 属性）。

    与 RPC 方法白名单同理：名字对不上时按钮点了没反应，属「静默失效」，让它在 CI 就红。
    """
    used = set(re.findall(r"openTarget\(\s*'([a-z_]+)'\s*\)", APP_JS.read_text(encoding="utf-8")))
    used |= set(re.findall(r'data-link="([a-z_]+)"', INDEX_HTML.read_text(encoding="utf-8")))
    assert used, "未抽到任何外部目标名，静态锁会空过"
    unknown = used - TARGET_NAMES
    assert not unknown, f"前端引用了 sidecar 不认识的外部目标：{sorted(unknown)}"


def test_sidecar_external_rpc_ignores_caller_supplied_url():
    """sidecar 侧：外链 RPC 不得从参数里读 URL，且最终放行必须过白名单闸门。"""
    src = SIDECAR.read_text(encoding="utf-8")
    for signature in (
        "def open_external_url(self, params):",
        "def open_vigembus_download(self, params):",
    ):
        body = _python_function_body(src, signature)
        assert "webbrowser.open(" in body, f"{signature} 抽取结果可疑，静态锁会空过"
        for pattern in ('params.get("url")', "params['url']", 'params["url"]', 'params.get("url",'):
            assert pattern not in body, f"{signature} 又读了调用方传的 URL（{pattern}）"

    open_body = _python_function_body(src, "def open_external_url(self, params):")
    assert "is_openable_url(" in open_body, (
        "open_external_url 缺少最终白名单闸门：不论地址从哪条路径攒出来，放行前都要过它"
    )
