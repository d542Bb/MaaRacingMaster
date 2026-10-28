"""仓库卫生检查（check_repo_hygiene）的回归锁。

两类用例都要有（守则：回归锁不得只覆盖失败例）：
  - 正常态：干净树绿、真实仓库在基线内绿；
  - 失败态：缺 README / 缺 status / 正式文档引用实验路径 / 死引用 / plan 缺头部三行，
    各被对应规则抓到。

本文件自身是 R4 的扫描对象——测试夹具里的实验路径一律用 `_EP` 拼接构造，不留字面量。
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


def test_dead_reference_flagged(tmp_path):
    _mk(tmp_path)
    rd = tmp_path / "tools" / "experiments" / "demo-topic" / "README.md"
    rd.write_text(f"status: active\n\n续作见 {_EP}gone-topic/。\n", encoding="utf-8")
    viol, _ = chk.Checker(tmp_path).scan()
    assert f"R5|{_EP}demo-topic/README.md|gone-topic" in viol


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


def test_real_repo_violations_all_baselined():
    """真实仓库当前态：不得有基线外违规（CI 同口径，防本地绿/CI 红漂移）。"""
    viol, baseline = chk.Checker(ROOT).scan()
    fresh = viol - baseline
    assert not fresh, "新增违规（基线只减不增）：\n  " + "\n  ".join(sorted(fresh))


def test_baseline_file_exists_and_key_format():
    """基线文件在树、键格式合法（R1~R6|对象|标识可选）；空基线=全部退役完成，也合法。"""
    bl = ROOT / "tools" / "repo_hygiene_baseline.txt"
    assert bl.exists()
    keys = [ln.strip() for ln in bl.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert all(k.split("|")[0] in {"R1", "R2", "R3", "R4", "R5", "R6"} and "|" in k for k in keys)
