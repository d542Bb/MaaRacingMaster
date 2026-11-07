"""仓库卫生检查（check_repo_hygiene）的回归锁。

两类用例都要有（守则：回归锁不得只覆盖失败例）：
  - 正常态：干净树绿、真实仓库在基线内绿；
  - 失败态：缺 README / 缺 status / 正式文档引用实验路径 / 死引用 / plan 缺头部三行 /
    隐私（本机绝对路径、本地词表命中），各被对应规则抓到。

R3~R10 的引用完整性按「成对」写：每条新判据至少一条红用例 + 一组合法书写不红的用例
（包根相对、外部信源、运行期落点、通配与占位符、会话产物样式）——只有失败例的闸门
会把误报洗成「大家都习惯忽略的那条红」。

本文件在 R4 的扫描对象之外（`CODE_EXCLUDE`）：`_mk` 要在 tmp_path 里搭出真实形状的
假树，路径必然是分段拼的。夹具仍沿用 `_EP` 拼接，免得哪天排除失效时满屏自指。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_repo_hygiene", ROOT / "tools" / "check_repo_hygiene.py")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

_EP = "tools/" + "experiments/"          # 防 R4 自引用

_GOOD_PLAN = (
    "# 演示方案\n\n"
    "> **状态**：active\n"
    "> **更新**：2026-10-28\n"
    "> **归属去向**：docs/adr/ADR-9999\n"
    "> **退役条件**：实现合入且 ADR 落地后删除\n\n"
    "## 行为契约\n"
)


def _mk(tmp: Path) -> Path:
    """最小合法树：一个带 status: active 的主题 + 只含占位符引用的 AGENTS.md。"""
    topic = tmp / "tools" / "experiments" / "demo-topic"
    topic.mkdir(parents=True)
    (topic / "README.md").write_text(
        "# demo\n\nstatus: active\n\n结论：成立。\n", encoding="utf-8")
    (tmp / "AGENTS.md").write_text(
        f"实验记录 → `{_EP}<主题>/`。\n", encoding="utf-8")
    return tmp


def _plan(tmp: Path, body: str, name: str = "demo-plan.md") -> Path:
    d = tmp / "docs" / "plan"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def test_clean_tree_passes(tmp_path):
    _mk(tmp_path)
    viol, baseline = chk.Checker(tmp_path).scan()
    assert viol == set()
    assert baseline == set()


def test_missing_readme_flagged(tmp_path):
    _mk(tmp_path)
    (tmp_path / "tools" / "experiments" / "bare-topic").mkdir()
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R1|bare-topic|缺 README.md" in viol


def test_missing_status_line_flagged(tmp_path):
    _mk(tmp_path)
    rd = tmp_path / "tools" / "experiments" / "demo-topic" / "README.md"
    rd.write_text("# demo\n\n结论：成立。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R1|demo-topic|README 缺 status: active 行" in viol


def test_formal_doc_topic_reference_flagged(tmp_path):
    _mk(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        f"证据见 `{_EP}demo-topic/README.md`。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R2|AGENTS.md|demo-topic" in viol


def test_topic_reference_without_trailing_slash_flagged(tmp_path):
    """主题名不带尾斜杠也算引用——只认带斜杠的写法会漏掉一整类引用。"""
    _mk(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        f"实测见 `{_EP}demo-topic` 一节。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R2|AGENTS.md|demo-topic" in viol


def test_area_own_file_reference_not_flagged(tmp_path):
    """指向区内文件（本区 README、索引、读取器）是类别指针，不是主题路径。"""
    _mk(tmp_path)
    (tmp_path / "tools" / "experiments" / "README.md").write_text(
        "# 本区\n", encoding="utf-8")
    (tmp_path / "tools" / "experiments" / "trace_reader.py").write_text(
        '"""区内读取器。"""\n', encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        f"规则见 `{_EP}README.md`，读取器 `{_EP}trace_reader.py`。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R2|AGENTS.md")]


def test_code_segmented_path_flagged(tmp_path):
    """生产/测试代码分段拼出的实验路径也是运行依赖——字面量匹配看不见它。"""
    _mk(tmp_path)
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "runner.py").write_text(
        'REPO = ROOT\nP = REPO / "tools" / "experiments" / "demo-topic" / "probe.py"\n',
        encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R4|scripts/runner.py" in viol


def test_code_category_pointer_not_flagged(tmp_path):
    """类别指针不算依赖：docstring 里写「由 tools/experiments/ 的探针另行判定」是说明
    分工，没有具体主题名。R4 只认「目录 + 名字」，与 R2 同口径（历史误报：
    tests/test_core_ocr.py 的这句被当成运行依赖挂进基线）。"""
    _mk(tmp_path)
    d = tmp_path / "tests"
    d.mkdir()
    (d / "test_x.py").write_text(
        f'"""识别率优劣由 `{_EP}` 的探针另行判定，别在此处顺手纠正。"""\n',
        encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R4|tests/test_x.py" not in viol


def test_dead_reference_flagged(tmp_path):
    _mk(tmp_path)
    rd = tmp_path / "tools" / "experiments" / "demo-topic" / "README.md"
    rd.write_text(f"status: active\n\n续作见 {_EP}gone-topic/。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert f"R5|{_EP}demo-topic/README.md|gone-topic" in viol


# ---- R3 扩面：相对拼写与代码面 ------------------------------------------------
# 立规实例：ADR-0005 三处 `../plan/…` 指向已清空的工作台文件，只认字面 `docs/plan/`
# 的检查器一直是绿的；归档插件代码里的 plan 引用同理从不被计过。

def test_plan_reference_relative_spelling_flagged(tmp_path):
    _mk(tmp_path)
    d = tmp_path / "docs" / "adr"
    d.mkdir(parents=True)
    (d / "0001-choice-x.md").write_text("定案过程见 [方案](../plan/gone-note.md)。\n",
                                        encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert any(v.startswith("R3|docs/adr/0001-choice-x.md|") for v in viol)


def test_plan_reference_in_code_flagged(tmp_path):
    _mk(tmp_path)
    d = tmp_path / "maaracing_master" / "core"
    d.mkdir(parents=True)
    (d / "thing.py").write_text('# 口径见 docs/plan/split-note.md\nX = 1\n', encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R3|maaracing_master/core/thing.py|docs/plan/split-note.md" in viol


# ---- R8 路径可解析（成对：死路径红 / 各种合法书写不红）------------------------

def _doc(tmp: Path, rel: str, body: str) -> Path:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_dead_markdown_link_flagged(tmp_path):
    _mk(tmp_path)
    (tmp_path / "tests").mkdir()
    _doc(tmp_path, "docs/CODE_WIKI.md", "回归锁见 [tests](../tests/test_gone.py)。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R8|docs/CODE_WIKI.md|../tests/test_gone.py" in viol


def test_dead_backtick_path_flagged(tmp_path):
    _mk(tmp_path)
    (tmp_path / "tests").mkdir()
    _doc(tmp_path, "AGENTS.md", "证据在 `tests/test_gone.py`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R8|AGENTS.md|tests/test_gone.py" in viol


def test_dead_asset_type_link_flagged(tmp_path):
    """资产类型也要进判定面：图标与预置 wheel 这类死引用曾因类型不在表内被当成"非声明"。"""
    _mk(tmp_path)
    (tmp_path / "assets").mkdir()
    _doc(tmp_path, "README.md", "图标见 [logo](assets/logo.svg)，包在 `scripts/wheels/x.whl`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R8|README.md|assets/logo.svg" in viol
    assert "R8|README.md|scripts/wheels/x.whl" in viol


def test_relative_link_missing_one_level_flagged(tmp_path):
    """少写一级：换个基准就凑得开，所以显式相对写法只按本文档目录解析。"""
    _mk(tmp_path)
    real = tmp_path / "maaracing_master" / "plugins" / "speedrush"
    real.mkdir(parents=True)
    (real / "CODE_WIKI.md").write_text("# 现状\n", encoding="utf-8")
    _doc(tmp_path, "docs/adr/0009-criteria-x.md",
         "现状见 [CODE_WIKI](../maaracing_master/plugins/speedrush/CODE_WIKI.md)。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R8|docs/adr/0009-criteria-x.md|../maaracing_master/plugins/speedrush/CODE_WIKI.md" in viol


def test_live_and_styled_paths_clean(tmp_path):
    """合法态成对覆盖：现存路径、包根相对写法、外部信源、运行期落点、通配与占位符、
    会话产物样式——都不该被 R8 误伤（误报会把闸门训成噪音源）。"""
    _mk(tmp_path)
    (tmp_path / "tests" / "real.py").parent.mkdir(parents=True)
    (tmp_path / "tests" / "real.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "maaracing_master" / "core").mkdir(parents=True)
    (tmp_path / "maaracing_master" / "core" / "wgcap.py").write_text("X = 2\n", encoding="utf-8")
    _doc(tmp_path, "docs/CODE_WIKI.md",
         "现存 `tests/real.py`；包根相对 `core/wgcap.py`；官方文档 `docs/zh_cn/3.1-协议.md`；"
         "运行期 `framework/config/maa_option.json` 与 `%APPDATA%/MaaRacingMaster/`；"
         "通配 `plugins/*/module.py`；占位符 `plugins/<id>/resources/policy/x.json`；"
         "会话产物 `hud/hud_meta.json` 与 `raw/000001_raw.jpg`；目录种类 `resources/`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R8|")], viol


# ---- R9 本机不入库面：盘上存在也不合法 ----------------------------------------

def test_local_only_dir_reference_flagged(tmp_path):
    """`archive/` 在盘上凑巧存在，存在性检查会放行——这一类要靠字面禁令兜住。"""
    _mk(tmp_path)
    (tmp_path / "archive" / "racing").mkdir(parents=True)
    (tmp_path / "archive" / "racing" / "loop.py").write_text("X = 1\n", encoding="utf-8")
    _doc(tmp_path, "maaracing_master/plugins/speedrush/CODE_WIKI.md",
         "旧实现在 `archive/racing/loop.py` 里。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R9|maaracing_master/plugins/speedrush/CODE_WIKI.md|archive/racing/loop.py" in viol


def test_local_only_dir_flagged_when_absent_from_disk(tmp_path):
    """干净检出里 `build/` 目录不存在，禁令仍然生效：名单取入库声明，不取盘上有什么。
    否则这条只在开发机红、CI 整条空转——已提交的 build/ 类回归恰恰要靠 CI 拦。"""
    _mk(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _doc(tmp_path, "docs/CODE_WIKI.md", "本轮实测数据源：`build/release/report.md`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R9|docs/CODE_WIKI.md|build/release/report.md" in viol


def test_runtime_data_dir_reference_clean(tmp_path):
    """用户数据目录那几类与本机不入库目录同名时说的是运行期落点，不该被 R9 误伤。"""
    _mk(tmp_path)
    (tmp_path / ".gitignore").write_text("logs/\ndata/\n", encoding="utf-8")
    _doc(tmp_path, "docs/CODE_WIKI.md", "会话记录在 `logs/<会话>/x.jsonl`，宏在 `data/y.json`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R9|")], viol


def test_build_dir_as_code_output_clean(tmp_path):
    """代码把不入库目录当输出落点是合法用法（build/ 本就是产物根），R9 不判代码面。"""
    _mk(tmp_path)
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "pack.py").write_text('OUT = REPO / "build" / "release" / "stage"\n', encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R9|")], viol


def test_scripts_doc_local_only_reference_flagged(tmp_path):
    """脚本目录的文档也进 R9 面：那最爱拿本机产物根当证据引用（立规时真实命中）。"""
    _mk(tmp_path)
    _doc(tmp_path, "scripts/release/policy.md", "数据源：`build/release/size-report.md`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R9|scripts/release/policy.md|build/release/size-report.md" in viol


def test_scripts_doc_backslash_spelling_flagged(tmp_path):
    """反斜杠拼写同判：Windows 手写文档就用这个，只认斜杠会让禁令在最该抓的写法上空手。"""
    _mk(tmp_path)
    _doc(tmp_path, "scripts/release/policy.md", "数据源：`build\\_c1_extract\\bench.json`（已清理）。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R9|scripts/release/policy.md|build/_c1_extract/bench.json" in viol


def test_vendored_third_party_docs_not_scanned(tmp_path):
    """第三方 vendored 文档不进 R8/R9 面：纳入的实测代价是一堆别人家 README 的噪音，
    闸门一旦常年响在别人的文件上，就没人再看自己仓库的那几条。"""
    _mk(tmp_path)
    _doc(tmp_path, "tools/pipeline/studio/static/node_modules/debug/README.md",
         "示例见 `examples/node/app.js`，产物在 `build/lib/x.js`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith(("R8|", "R9|"))], viol


# ---- R10 脚手架退役世代：只判插件脚手架 ----------------------------------------

def test_scaffold_retired_generation_flagged(tmp_path):
    _mk(tmp_path)
    _doc(tmp_path, "templates/plugin/README.md",
         "导航真源：`plugins/<id>/resources/config/<id>_assets.json`，运行期经 `Assets.load` 直读。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R10|templates/plugin/README.md|Assets.load" in viol
    assert any(v.startswith("R10|templates/plugin/README.md|_assets.json") for v in viol)


def test_retired_word_in_adr_history_clean(tmp_path):
    """ADR 背景章提退役世代是合法的历史叙述，不在 R10 面内。"""
    _mk(tmp_path)
    _doc(tmp_path, "docs/adr/0002-arch-global-x.md", "## 背景\n\nv3 有 `*_assets.json` 与 `global_assets`。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R10|")], viol


# ---- R1 口径：区内下划线目录不是实验主题 --------------------------------------

def test_intra_area_dir_not_topic(tmp_path):
    """`_probe_data/` 是区内工作目录，没有 README 不该被判缺入口护栏。"""
    _mk(tmp_path)
    (tmp_path / "tools" / "experiments" / "_probe_data").mkdir()
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if "R1|_probe_data" in v], viol


def test_declared_area_dir_clean_off_disk(tmp_path):
    """干净检出里不入库的区内目录不在盘上：区内 .gitignore 声明过它就不算死引用。
    只看盘上有什么会「本地绿、CI 红」——真实踩过：模拟 CI 检出里 R5 报
    speedrush_planning 对 `_probe_data` 的正常引用。"""
    _mk(tmp_path)
    (tmp_path / "tools" / "experiments" / ".gitignore").write_text(
        "_probe_data/\n_probe_scripts/\n", encoding="utf-8")
    (tmp_path / "tools" / "experiments" / "demo-topic" / "README.md").write_text(
        f"status: active\n\n读数落 `{_EP}_probe_data/`，文件可重跑。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R5|")], viol


def test_undeclared_area_dir_still_dead(tmp_path):
    """名字打错仍然红：豁免认的是入库声明，不是「凡是下划线开头都放过」。"""
    _mk(tmp_path)
    (tmp_path / "tools" / "experiments" / ".gitignore").write_text(
        "_probe_data/\n", encoding="utf-8")
    (tmp_path / "tools" / "experiments" / "demo-topic" / "README.md").write_text(
        f"status: active\n\n读数落 `{_EP}_probe_dat/`。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert any(v.startswith("R5|") and "_probe_dat" in v for v in viol), viol


def test_plan_reference_flagged(tmp_path):
    _mk(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CODE_WIKI.md").write_text(
        "方案详见 docs/plan/some-workbench-note.md。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert any(v.startswith("R3|docs/CODE_WIKI.md|") for v in viol)


def test_plan_head_ok_passes(tmp_path):
    """合规头部（状态 / 归属去向 / 退役条件三行齐）不该被 R6 误伤。"""
    _mk(tmp_path)
    _plan(tmp_path, _GOOD_PLAN)
    viol, _ = chk.Checker(tmp_path).scan()
    assert viol == set()


def test_plan_missing_status_flagged(tmp_path):
    _mk(tmp_path)
    _plan(tmp_path, "# 演示\n\n> **归属去向**：docs/adr/ADR-9999\n> **退役条件**：y\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R6|docs/plan/demo-plan.md|缺状态行（> **状态**：active）" in viol


def test_plan_status_out_of_set_flagged(tmp_path):
    """自述「已实施」却留在树里，正是本区要抓的形态——完成不是状态，删除才是。"""
    _mk(tmp_path)
    _plan(tmp_path, _GOOD_PLAN.replace("**状态**：active", "**状态**：已实施（2026-10-21）"))
    viol, _ = chk.Checker(tmp_path).scan()
    assert any(v.startswith("R6|docs/plan/demo-plan.md|状态值不在闭集") for v in viol)


def test_plan_missing_home_and_condition_flagged(tmp_path):
    """只有状态行不够：不知道结论去哪、何时能删，就必然烂在树里。"""
    _mk(tmp_path)
    _plan(tmp_path, "# 演示\n\n> **状态**：active\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert any("缺归属去向行" in v for v in viol)
    assert any("缺退役条件行" in v for v in viol)


def test_plan_placeholder_not_filled_flagged(tmp_path):
    """直接复制模板不填，等于没写——占位符原样留着要红。"""
    _mk(tmp_path)
    _plan(tmp_path, "# 演示\n\n> **状态**：active\n"
                    "> **归属去向**：<结论最终住哪>\n"
                    "> **退役条件**：<一句可判定的话>\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert any("仍是模板占位符" in v for v in viol)


def test_plan_status_value_tolerates_suffix(tmp_path):
    """手写 `retiring（日期）` 这类后缀不该被误判。"""
    _mk(tmp_path)
    _plan(tmp_path, _GOOD_PLAN.replace("**状态**：active", "**状态**：retiring（2026-10-28）"))
    viol, _ = chk.Checker(tmp_path).scan()
    assert viol == set()


def test_plan_ledger_lists_files_with_days(tmp_path):
    """台账只呈现不判红：状态与未动天数都要读得到。"""
    _mk(tmp_path)
    _plan(tmp_path, _GOOD_PLAN)
    rows = chk.Checker(tmp_path).plan_ledger()
    assert len(rows) == 1
    days, st, rel = rows[0]
    assert st == "active" and rel == "docs/plan/demo-plan.md" and days >= 0


def test_plan_ledger_marks_mtime_fallback(tmp_path):
    """无更新日期时回退 mtime，状态带 ~ 提醒这个天数会被补头等动作刷新。"""
    _mk(tmp_path)
    _plan(tmp_path, "# 演示\n\n> **状态**：active\n> **归属去向**：x\n> **退役条件**：y\n")
    rows = chk.Checker(tmp_path).plan_ledger()
    assert rows[0][1] == "active~"


def test_privacy_absolute_path_flagged(tmp_path):
    """R7：入库文件里的盘符绝对路径与会话 ID 都要被抓住。"""
    _mk(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "NOTE.md").write_text(
        "输出在 C:/Users/someone/Downloads 下，帧取自 sess_0123456789abcdef0123 缓存。\n",
        encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R7|docs/NOTE.md|盘符绝对路径" in viol
    assert "R7|docs/NOTE.md|会话ID" in viol


def test_privacy_env_derived_code_clean(tmp_path):
    """R7：路径从环境变量推导（APPDATA 等）不算本机指纹，不该误伤。"""
    _mk(tmp_path)
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "probe.py").write_text(
        'from pathlib import Path\n'
        'import os\n'
        'LOG = Path(os.environ["APPDATA"]) / "MaaRacingMaster" / "logs"\n',
        encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R7|")]


def test_privacy_local_terms_flagged(tmp_path):
    """R7：本地词表（不入库文件）里的正则命中也要红；键只带行号不带词本身。"""
    _mk(tmp_path)
    (tmp_path / "tools" / "repo_privacy_terms.local.txt").write_text(
        "# 注释行\n本地来源词\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("素材来源见 本地来源词 目录。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert any(v.startswith("R7|README.md|本地词表#2") for v in viol)


def test_privacy_canonical_system_path_clean(tmp_path):
    """R7：系统标准安装位置与裸根盘符不是本机指纹，不该误伤（测试素材同款）。"""
    _mk(tmp_path)
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "probe.py").write_text(
        'A = "C:\\Windows\\win.ini"\n'
        'B = "C:/Windows/System32/calc.exe"\n'
        'C = "C:\\Program Files\\Microsoft Visual Studio\\2022\\Community"\n'
        'D = "C:\\"\n',
        encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R7|")]


# ---- R11 契约文档正文腐化（成对：腐化指纹红 / 合法书写不红 / 棘轮）--------------

def _wiki(tmp: Path, body: str, rel: str = "docs/CODE_WIKI.md") -> Path:
    return _doc(tmp, rel, body)


def test_r11_date_flagged(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n模型预加载（2026-10-23）落地。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R11|docs/CODE_WIKI.md|日期|2026-10-23" in viol


def test_r11_session_stamp_flagged(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n取证见 20261006_141159 局。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R11|docs/CODE_WIKI.md|会话戳|20261006_141159" in viol


def test_r11_round_number_flagged(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n第八轮取证换装定案。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R11|docs/CODE_WIKI.md|轮次|第八轮" in viol


def test_r11_phase_code_flagged(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n阶段 B 第二块契约落地。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R11|docs/CODE_WIKI.md|阶段代号|阶段 B" in viol


def test_r11_step_number_flagged(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\nstep 5 离线链路一起接。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R11|docs/CODE_WIKI.md|step编号|step 5" in viol


def test_r11_migration_phase_flagged(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\nP4a 整删批后清干净。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R11|docs/CODE_WIKI.md|迁移期号|P4a" in viol


def test_r11_count_snapshot_flagged(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n全仓 21 passed，21 节点零差异。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert "R11|docs/CODE_WIKI.md|计数快照|21 passed" in viol
    assert "R11|docs/CODE_WIKI.md|计数快照|21 节点" in viol


def test_r11_commit_pointer_and_threshold_clean(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n证据见 commit `9fc13a5`，阈值 conf=0.35、帧预算 33ms。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R11|")]


def test_r11_percentile_and_game_stage_clean(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\nP50/P95 见 perf；驾驶阶段 1 与阶段 2 各跑一段。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R11|")]


def test_r11_archive_wiki_exempt(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 历史\n\n归档（2026-10-23）。\n",
          rel="maaracing_master/plugins/_archive/treasure/CODE_WIKI.md")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R11|")]


def test_r11_non_wiki_doc_exempt(tmp_path):
    _mk(tmp_path)
    _doc(tmp_path, "docs/adr/0001-x.md", "# 决策\n\n日期：2026-10-23。\n")
    _doc(tmp_path, "docs/update_log.md", "## v1\n\n2026-10-23 发布。\n")
    viol, _ = chk.Checker(tmp_path).scan()
    assert not [v for v in viol if v.startswith("R11|")]


def test_r11_new_hit_fails_ci(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n（2026-10-23）落地。\n")
    assert chk.main([], root=tmp_path) == 1


def test_r11_baselined_hit_passes_ci(tmp_path):
    _mk(tmp_path)
    _wiki(tmp_path, "# 现状\n\n（2026-10-23）落地。\n")
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "repo_hygiene_baseline.txt").write_text(
        "R11|docs/CODE_WIKI.md|日期|2026-10-23\n", encoding="utf-8")
    assert chk.main([], root=tmp_path) == 0


def test_real_repo_violations_all_baselined():
    """真实仓库当前态：不得有基线外违规（CI 同口径，防本地绿/CI 红漂移）。"""
    viol, baseline = chk.Checker(ROOT).scan()
    fresh = viol - baseline
    assert not fresh, "新增违规（基线只减不增）：\n  " + "\n  ".join(sorted(fresh))


def test_baseline_file_exists_and_key_format():
    """基线文件在树、键格式合法（R1~R11|对象|标识可选）；空基线=全部退役完成，也合法。"""
    bl = ROOT / "tools" / "repo_hygiene_baseline.txt"
    assert bl.exists()
    keys = [ln.strip() for ln in bl.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert all(k.split("|")[0] in {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11"}
               and "|" in k for k in keys)
