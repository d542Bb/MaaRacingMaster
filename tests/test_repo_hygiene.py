"""仓库卫生检查（check_repo_hygiene）的回归锁。

两类用例都要有（守则：回归锁不得只覆盖失败例）：
  - 正常态：干净树绿、真实仓库在基线内绿；
  - 失败态：缺 README / 缺 status / 正式文档引用实验路径 / 死引用，各被对应规则抓到。

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


def _mk(tmp: Path) -> Path:
    """最小合法树：一个带 status: active 的主题 + 只含占位符引用的 AGENTS.md。"""
    topic = tmp / "tools" / "experiments" / "demo-topic"
    topic.mkdir(parents=True)
    (topic / "README.md").write_text(
        "# demo\n\nstatus: active\n\n结论：成立。\n", encoding="utf-8")
    (tmp / "AGENTS.md").write_text(
        f"实验记录 → `{_EP}<主题>/`。\n", encoding="utf-8")
    return tmp


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


def test_real_repo_violations_all_baselined():
    """真实仓库当前态：不得有基线外违规（CI 同口径，防本地绿/CI 红漂移）。"""
    viol, baseline = chk.Checker(ROOT).scan()
    fresh = viol - baseline
    assert not fresh, "新增违规（基线只减不增）：\n  " + "\n  ".join(sorted(fresh))


def test_baseline_file_exists_and_key_format():
    """基线文件在树、键格式合法（R1~R5|对象|标识可选）；空基线=全部退役完成，也合法。"""
    bl = ROOT / "tools" / "repo_hygiene_baseline.txt"
    assert bl.exists()
    keys = [ln.strip() for ln in bl.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert all(k.split("|")[0] in {"R1", "R2", "R3", "R4", "R5"} and "|" in k for k in keys)
