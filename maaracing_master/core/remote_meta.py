# -*- coding: utf-8 -*-
"""远程元数据的信任边界：公告 / 版本标记 / 外链目标的校验与归一化。

**为什么单独成模块**：sidecar 的导入链会拉 cv2 / maa / vgamepad 等重依赖，挂在它
下面的逻辑在 Linux CI 上只能整文件 SKIP；本模块只用标准库，校验因此在 CI 里始终真跑。

**职责边界**：只判定「这条数据能不能信、能信到什么程度」，不做网络、不碰渲染。
渲染侧的对偶约束是「远程字段一律走 textContent」——两层互不替代：本模块管字段能不能
被采信，前端管采信之后不会被当成标记解析。

**校验失败的处置口径**（判据 = 该字段在 `docs/announcement.md` 里是否必填）：
- 必填字段不合规 → 整条数据作废（宁可不显示，也不显示半条）；
- 可选字段不合规 → 丢掉该字段，其余照常（例：`url` 不合规就不给详情按钮）；
- **动作型字段**（`url` / `download_url`）一律从严——它们一旦被采信就有副作用
  （交给默认浏览器打开），且这条路径不依赖 XSS 就能成立。

字段形状的权威定义在 `docs/announcement.md`（L1 规范）；本模块只实现它，不另立一份。
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

#: 仓库 slug。官方地址表、远程链接白名单、更新源 URL 共用这一处定义（sidecar 引用它，
#: 不再自留副本）——两处各写一遍，改一处漏一处就是白名单静默失效。
GITHUB_REPO = "d542Bb/MaaRacingMaster"
# CNB 是本仓库的镜像，项目名按构造等于仓库名——不另抄一个字面量。
_CNB_PROJECT = GITHUB_REPO.split("/")[-1]

# --------------------------------------------------------------------------
# 官方地址
# --------------------------------------------------------------------------

# 外链逻辑目标 → 官方地址。前端只报「要打开哪个目标」，地址一律由本表给出：
# 前端与远程数据都不持有可打开的 URL，XSS 也就无法把「打开浏览器」用在别处。
EXTERNAL_TARGETS: dict[str, str] = {
    "home": f"https://github.com/{GITHUB_REPO}",
    "issue": f"https://github.com/{GITHUB_REPO}/issues",
    "docs": f"https://github.com/{GITHUB_REPO}/blob/master/docs/CODE_WIKI.md",
    "release": f"https://github.com/{GITHUB_REPO}/releases/latest",
    "vigembus": "https://github.com/nefarius/ViGEmBus/releases/latest",
}

# 允许**远程数据**携带的链接：主机 → 路径段前缀白名单。
# 只收本项目自有页面——远程数据能把用户带去任意站点，等价于把「打开外链」交了出去。
_REMOTE_URL_PATHS: dict[str, tuple[str, ...]] = {
    "github.com": (f"/{GITHUB_REPO}",),
    "cnb.cool": (f"/{_CNB_PROJECT}",),
}


def _path_matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """按**段边界**比对：`== 前缀` 或 `前缀 + "/"` 开头。

    纯字符串 startswith 会把 `/MaaRacingMaster-evil` 这类同前缀异路径放行。
    """
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def is_official_url(url: str) -> bool:
    """远程数据携带的链接是否落在官方域白名单内（https + 主机 + 路径段前缀）。

    明确拒绝：非 https、带用户名/密码、非默认端口、主机名后缀伪装
    （`github.com.evil.tld`）、同主机但非本项目路径（`github.com/attacker/x`）。
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:  # 畸形 URL（如非法 IPv6 字面量）
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.port not in (None, 443):
        return False
    if parsed.username or parsed.password:
        return False
    prefixes = _REMOTE_URL_PATHS.get(parsed.hostname.lower())
    if not prefixes:
        return False
    return _path_matches(parsed.path or "", prefixes)


def is_openable_url(url: str) -> bool:
    """真正交给默认浏览器之前的最终放行判据：静态目标地址，或通过远程链接白名单。

    纵深防御——不管地址是从哪条路径攒出来的（逻辑目标表、远程元数据、将来新增的
    来源），过不了这里就不打开。
    """
    if url in EXTERNAL_TARGETS.values():
        return True
    return is_official_url(url)


# --------------------------------------------------------------------------
# 公告（docs/announcement.json）
# --------------------------------------------------------------------------

# 安全上界（非风格建议）：超限一律按字段不合规处理，避免一条畸形数据把界面撑爆。
# 风格建议（title ≤30 字等）留在 docs/announcement.md，不在这里硬拦——风格不是判据。
_LIMITS = {"id": 64, "title": 200, "body": 4000, "url_text": 60}
_LEVELS = ("info", "warn")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _text(value: object) -> str:
    """只接受 str（其余类型视为缺失）。

    远程 JSON 里 `title` 是数组/对象/数字都属 schema 违规；旧实现用 `str()` 强转，
    会把 `{"a": 1}` 渲染成字面量文本，属于「尽可能渲染」，与本节口径相反。
    """
    return value.strip() if isinstance(value, str) else ""


def is_date(value: str) -> bool:
    """严格 `YYYY-MM-DD`，且必须是真实存在的日期（`2026-02-30` 不算）。"""
    if not _DATE_RE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def is_expired(effective_until: str, today: str) -> bool:
    """过期判据：字符串比较（两侧均已由 `is_date` 保证同形状，字典序即时间序）。"""
    return effective_until < today


def parse_announcement(data: object) -> dict | None:
    """公告 payload → 归一化 dict；不合规返回 None（调用方按「本条无效」处理）。

    必填（id / level / title / date / effective_until）任一不合规 → None。
    `level` 只认 `info` / `warn`：它决定告警配色，值域封闭且由本项目定义，
    出现第三值说明数据不是本项目产的，没有「猜一个渲染」的理由。
    """
    if not isinstance(data, dict):
        return None

    ann_id = _text(data.get("id"))
    level = _text(data.get("level"))
    title = _text(data.get("title"))
    date_str = _text(data.get("date"))
    until = _text(data.get("effective_until"))

    if not ann_id or len(ann_id) > _LIMITS["id"]:
        return None
    if level not in _LEVELS:
        return None
    if not title or len(title) > _LIMITS["title"]:
        return None
    if not is_date(date_str) or not is_date(until):
        return None

    body = _text(data.get("body"))
    if len(body) > _LIMITS["body"]:
        body = ""
    # 地址不合规 = 丢字段（详情按钮不渲染），不是整条作废：公告正文本身仍然可信。
    url = _text(data.get("url"))
    if url and not is_official_url(url):
        url = ""
    url_text = _text(data.get("url_text")) or "查看详情"
    if len(url_text) > _LIMITS["url_text"]:
        url_text = "查看详情"

    return {
        "id": ann_id,
        "level": level,
        "title": title,
        "body": body,
        "date": date_str,
        "url": url,
        "url_text": url_text,
        "effective_until": until,
    }


# --------------------------------------------------------------------------
# 版本标记（docs/latest_release.json / GitHub releases API）
# --------------------------------------------------------------------------

# 版本号形状：至少一段数字，允许 `-dev.1` / `+local` 后缀。上限防超长串进 DOM。
_TAG_RE = re.compile(r"^\d+(?:\.\d+){0,3}(?:-[0-9A-Za-z.]+)?(?:\+[0-9A-Za-z.]+)?$")
_TAG_MAX = 32


def parse_release(data: object) -> dict | None:
    """版本标记 payload → `{tag, published_at, download_url}`；tag 不合规返回 None。

    三个源的字段名不同（CNB / GitHub raw 用 `tag`·`version`，GitHub API 用 `tag_name`），
    归一集中在这里。tag 进 DOM 也进版本比较，不合规即该源不可用（调用方继续 fallback）；
    `published_at` 只作展示，格式不对就省略，不牵连整条更新提示。
    """
    if not isinstance(data, dict):
        return None

    tag = _text(data.get("tag_name")) or _text(data.get("version")) or _text(data.get("tag"))
    if tag[:1] in ("v", "V"):  # 只去一个前导 v：lstrip("v") 会把 "vv1.0" 也吃掉
        tag = tag[1:]
    if not tag or len(tag) > _TAG_MAX or not _TAG_RE.match(tag):
        return None

    published = _text(data.get("published_at"))[:10]
    if not is_date(published):
        published = ""
    # 不合规即留空 → 调用方回退官方 release 页；绝不放行远程指定的第三方站点。
    download = _text(data.get("download_url"))
    if download and not is_official_url(download):
        download = ""

    return {"tag": tag, "published_at": published, "download_url": download}
