# -*- coding: utf-8 -*-
"""模块封面的资源归属契约：插件声明 → 核心解析 → 前端消费。

锁住四件事（对应 2026-10-31 的架构裁定「封面走彻底路线」）：

1. **声明权归模块**：封面在插件自己的 ``resources/`` 下，由模块类声明文件名；
   核心 ``core/covers.py`` 负责校验与解析成 URL。前端不出现任何插件目录路径。
2. **缺省即回落**：未声明、文件不存在、声明非法（绝对路径 / ``..`` 越界）一律 None，
   前端据此回落到 ``core/resources/cover_default.jpg``——封面是装饰，不阻断模块运行。
3. **下发行齐备**：任务列表每项带 ``cover``，初始状态带 ``cover_default``。
4. **发布包带得走**：封面必须落在插件目录内（装配脚本整目录 robocopy），
   不能只在开发目录有效。

为什么前端那部分用源码静态锁：仓库没有 DOM 测试链（同 test_frontend_remote_render.py
的口径），但「三级回退 + 不硬编码插件路径」是能从源码文本判定的写法回归。
它只证明写法没退回旧样式，不证明运行时行为。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from maaracing_master.core import covers
from maaracing_master.core.builtin_steps import BUILTIN_STEPS
from maaracing_master.core.registry import MODULE_REGISTRY

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "MaaRacingMaster.Shell" / "frontend"
ASSEMBLE = REPO / "scripts" / "release" / "assemble.ps1"


def _url_to_path(url: str) -> Path:
    """file:// URI → 本地路径（Windows 上去掉前导斜杠）。"""
    assert url.startswith("file:///"), f"封面 URL 应为 file URI，实际: {url}"
    return Path(url[len("file:///"):].replace("/", "\\"))


# ---------- 1. 声明权归模块：解析落在模块自己的 resources/ ----------

def test_plugin_cover_resolves_inside_its_own_resources():
    """有专属封面的插件：URL 指向 plugins/<id>/resources/ 下，且文件真实存在。"""
    url = covers.module_cover_url("speedrush")
    assert url, "speedrush 声明了 COVER，应解析出 URL"
    path = _url_to_path(url)
    plugin_dir = (REPO / "maaracing_master" / "plugins" / "speedrush").resolve()
    assert path.is_file()
    assert plugin_dir in path.parents, f"封面必须落在插件自己的目录内: {path}"


def test_builtin_step_cover_resolves_inside_core_resources():
    """内置步骤属于核心，封面在 core/resources/ 下（不进任何插件的 resources/）。"""
    url = covers.builtin_cover_url("launch_game")
    assert url, "启动游戏声明了 COVER"
    path = _url_to_path(url)
    assert path.is_file()
    assert path.parent == covers.CORE_RESOURCES, f"内置步骤封面应在核心资源目录: {path}"


# ---------- 2. 缺省即回落 ----------

def test_module_without_cover_yields_none():
    """未声明封面的模块（巅峰鉴宝没有专供图）返回 None，由前端回落默认封面。"""
    assert MODULE_REGISTRY.get("treasure") is not None, "treasure 未注册，测试前提不成立"
    assert covers.module_cover_url("treasure") is None


def test_default_cover_exists_and_is_a_file_uri():
    """通用默认封面必须真实存在——它缺失时前端会一路退到纯渐变，是静默降级。"""
    url = covers.default_cover_url()
    assert url, f"默认封面缺失: {covers.CORE_RESOURCES / covers.DEFAULT_COVER}"
    assert _url_to_path(url).is_file()


def test_unknown_id_is_none_not_raise():
    """未注册的 id 不抛异常（列表里可能出现已剥离模块的残留 id）。"""
    assert covers.cover_url_for("no_such_module") is None


@pytest.mark.parametrize("declared", [
    "../../secret.jpg",          # 越界
    "..\\..\\secret.jpg",        # 越界（Windows 分隔符）
    "/etc/passwd",               # 绝对路径（POSIX 写法）
    "C:\\Windows\\win.ini",      # 绝对路径（Windows 写法）
])
def test_illegal_declaration_is_rejected(declared, tmp_path, monkeypatch):
    """声明必须是 resources/ 内的相对路径；越界或绝对路径一律拒绝（不静默读别的文件）。"""
    base = tmp_path / "resources"
    base.mkdir()
    assert covers._declared_file(base, declared, "fake") is None


def test_missing_file_falls_back_to_none(tmp_path):
    """声明了但文件丢了：返回 None（记 WARNING），不抛——模块照常运行。"""
    base = tmp_path / "resources"
    base.mkdir()
    assert covers._declared_file(base, "cover.jpg", "fake") is None


def test_declared_file_inside_resources_is_accepted(tmp_path):
    """正例：resources/ 内的相对路径（含子目录）能解析到真实文件。"""
    base = tmp_path / "resources"
    (base / "image").mkdir(parents=True)
    target = base / "image" / "cover.jpg"
    target.write_bytes(b"\xff\xd8\xff")
    assert covers._declared_file(base, "image/cover.jpg", "fake") == target


# ---------- 3. 下发行齐备 ----------

def test_module_list_payload_carries_cover_fields():
    """任务列表每项带 cover，初始状态带 cover_default——前端只消费这两个字段。"""
    try:
        from maaracing_master.core.sidecar import SidecarService
    except Exception as exc:  # noqa: BLE001 —— 完整运行时依赖缺失时整项跳过
        pytest.skip(f"sidecar 需要完整运行时依赖：{exc}")

    svc = object.__new__(SidecarService)  # _module_list 不碰实例状态
    items = SidecarService._module_list(svc)
    assert items, "模块列表不应为空"
    for item in items:
        assert "cover" in item, f"{item['id']} 缺少 cover 字段"
        assert item["cover"] is None or item["cover"].startswith("file:///")

    by_id = {it["id"]: it for it in items}
    # 有图的与没图的同时在列表里，才能同时覆盖「显示专属封面」与「回落默认」
    assert by_id["launch_game"]["cover"], "启动游戏应有封面"
    assert by_id["treasure"]["cover"] is None, "巅峰鉴宝未供图，应为 None（回落默认）"


def test_initial_state_carries_default_cover():
    """默认封面随初始状态下发：前端拿不到它就无法做第二级回退。"""
    src = (REPO / "maaracing_master" / "core" / "sidecar.py").read_text(encoding="utf-8")
    assert '"cover_default": default_cover_url()' in src
    assert "from maaracing_master.core.covers import cover_url_for, default_cover_url" in src


# ---------- 4. 发布包带得走 ----------

def test_every_cover_lives_under_a_directory_the_assembler_copies():
    """封面必须位于装配脚本整目录复制的子树内（plugins/ 与 core/ 都在 maaracing_master/ 下）。

    装配脚本对 ``maaracing_master`` 是 ``robocopy /E``（含所有子目录与扩展名），
    所以「图在插件目录内」等价于「图随插件进包」——不依赖任何扩展名白名单。
    """
    script = ASSEMBLE.read_text(encoding="utf-8")
    line = next(
        (l for l in script.splitlines() if "robocopy" in l and "'maaracing_master'" in l),
        None,
    )
    assert line, "装配脚本里找不到 maaracing_master 的复制行，抽取规则已失效"
    assert "/E" in line, f"maaracing_master 必须整目录复制: {line.strip()}"
    assert "/XF" not in line, f"复制行不得带扩展名过滤（会漏掉封面）: {line.strip()}"

    root = (REPO / "maaracing_master").resolve()
    checked = 0
    for eid in list(BUILTIN_STEPS) + list(MODULE_REGISTRY):
        url = covers.cover_url_for(eid)
        if not url:
            continue
        assert root in _url_to_path(url).parents, f"{eid} 的封面不在随包复制范围内"
        checked += 1
    assert checked >= 3, f"应至少校验到 3 张封面，实际 {checked}（抽取规则可能失效）"


# ---------- 5. 前端：只消费 URL，不感知插件目录 ----------

def _strip_js_comments(src: str) -> str:
    """去掉整行 // 注释与 /* */ 块注释：契约说明里提到 plugins/<id>/ 是允许的，
    违规的是代码里真的拼了这条路径。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))


def test_frontend_never_hardcodes_plugin_paths():
    """前端不得出现 plugins/<id>/resources 这类内部路径——那是核心的解析职责。"""
    for rel in ("js/run.js", "js/gallery-shape.js", "js/rpc.js", "app.js"):
        src = _strip_js_comments((FRONTEND / rel).read_text(encoding="utf-8"))
        assert "plugins/" not in src and "plugins\\\\" not in src, f"{rel} 里出现了插件目录路径"
        assert "resources/" not in src, f"{rel} 里出现了插件资源路径"


def test_gallery_renders_cover_with_three_level_fallback():
    """三级回退：专属封面 → 通用默认封面 → 纯渐变底（封面加载失败也要降到下一级）。"""
    src = (FRONTEND / "js" / "run.js").read_text(encoding="utf-8")
    body_start = src.index("function galleryCellHtml(")
    body_end = src.index("function renderGallery(", body_start)
    body = src[body_start:body_end]
    assert "m.cover || state.coverDefault" in body, "封面取值应走 cover → coverDefault"
    assert 'class="task-gallery-art"' in body, "封面应是 img（background-image 无法感知加载失败）"

    modal_start = src.index("function openManageModal(")
    modal = src[modal_start:modal_start + 4000]
    assert "classList.contains('task-gallery-art')" in modal, "缺少封面加载失败的兜底处理"
    assert "task-gallery-cell--noart" in modal, "末级回退（纯渐变底）未接上"
    assert "ro.disconnect()" in modal, "尺寸观察器必须随弹窗关闭断开（否则每开一次漏一个）"


def test_gallery_shape_module_is_loaded_before_run_js():
    """几何模块必须先于 run.js 加载：run.js 在渲染时直接调用它。"""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    shape = html.index('src="js/gallery-shape.js"')
    run = html.index('src="js/run.js"')
    assert shape < run, "gallery-shape.js 必须在 run.js 之前加载"
