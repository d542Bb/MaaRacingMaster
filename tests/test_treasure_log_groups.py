# -*- coding: utf-8 -*-
"""treasure 组归属验收锁（契约 §8 第 7 步，A 案 + 裁定边界规则）。

锁五件事：
1. _tlog 开组期间归当前组，组终态按组内 ERROR/WARNING 自动推导；
2. logger 无 group()（测试 recorder 桩）时 _open_grp 回退 legacy 锚点行——现网
   行为在桩环境零破坏；
3. 停止路径显式 incomplete 与 has_error 并存（中断归中断、错误照留痕）；
4. 结构性锁：module.py / store.py 中 INFO+ 的裸 logger.log 只允许出现在
   跨切面无组白名单（初始化/绑定/worker 生命周期/落盘失败）——
   新日志想上 GUI 卡片必须走 _tlog/_open_grp，防止绕过归属机制回潮。
"""
from types import SimpleNamespace

import pytest

try:
    import maaracing_master.plugins.treasure.module as tmod
    import maaracing_master.plugins.treasure.store as tstore
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖（maa/…）：{_ERR}")

from maaracing_master.core.logger import Logger  # noqa: E402


def _recs(lg):
    return [r for _s, r in lg._lines]


def test_tlog_routes_to_open_group_and_derives_warning(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(tmod, "logger", lg)
    m = SimpleNamespace()
    tmod._open_grp(m, "[鉴宝] 进入阶段: 观察 [x]", "phase")
    tmod._tlog(m, "识别到目标")
    tmod._tlog(m, "点击未生效，重试 1/3", "WARNING")
    tmod._end_grp(m)
    recs = _recs(lg)
    types = [r["event_type"] for r in recs]
    assert types == ["group_start", "log", "log", "group_end"]
    gid = recs[0]["group_id"]
    assert recs[1]["group_id"] == gid and recs[2]["group_id"] == gid
    assert recs[0]["kind"] == "phase" and recs[0]["outcome"] == "running"
    end = recs[-1]
    assert end["outcome"] == "warning" and end["has_warning"] is True


def test_open_grp_falls_back_without_group_api(monkeypatch):
    class _Recorder:
        def __init__(self):
            self.lines = []

        def log(self, msg, level="INFO", channel=None):
            self.lines.append((level, str(msg)))

    rec = _Recorder()
    monkeypatch.setattr(tmod, "logger", rec)
    m = SimpleNamespace()
    tmod._open_grp(m, "模块启动", "session")
    assert rec.lines == [("INFO", "模块启动")]        # 回退 legacy 锚点行
    assert getattr(m, "_log_grp", None) is None      # 不持有句柄
    tmod._tlog(m, "普通行", "WARNING")
    assert ("WARNING", "普通行") in rec.lines


def test_stop_path_incomplete_keeps_error_flag(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(tmod, "logger", lg)
    m = SimpleNamespace()
    tmod._open_grp(m, "阶段A", "phase")
    tmod._tlog(m, "帧异常", "ERROR")
    tmod._end_grp(m, "incomplete")   # finally 停止路径
    end = [r for r in _recs(lg) if r["event_type"] == "group_end"][-1]
    assert end["outcome"] == "incomplete" and end["has_error"] is True


# ---------- 结构性锁：无组白名单 ----------
# 跨切面日志有意保持无组（裁定规则 4）；除此以外的 INFO+ 行必须走 _tlog/_open_grp。
_MODULE_UNGROUPED_PREFIXES = (
    "[鉴宝] policy 感知锚点", "[鉴宝] 窗口连接失败", "游戏窗口不是 16:9",
    "[鉴宝] 未加载", "[鉴宝] 未加载到动作按钮", "[鉴宝] 未加载任何鉴宝师",
    "[鉴宝] policies 编译失败", "[鉴宝点击] 手柄绑定失败", "[鉴宝点击] 光标长时间丢失",
    "[鉴宝点击] 虚拟手柄重建失败",  # 自愈失败留痕：test_capabilities_gamepad 锁 WARNING，有意无组
    "[鉴宝] IO worker", "[鉴宝] 观察线程", "[鉴宝] OCR worker",
)
_STORE_UNGROUPED_PREFIXES = (
    "[鉴宝落盘] SQLite 初始化失败", "[鉴宝落盘] 关闭连接失败", "[鉴宝落盘] 写入失败",
    "[鉴宝落盘] 领取数含负值", "[鉴宝落盘] 领取蛋数写入失败", "[鉴宝落盘] games 表迁移失败",
    "[鉴宝落盘] daily_summary 迁移失败", "读取今日看板数据失败",
)


# 机制函数自身合法地直调 logger.log 做无组回退（回退路径就是它们的职责）。
_MECH_FUNCS = {"_tlog", "_open_grp", "_end_grp", "_mlog", "log_session_summary"}


def _info_plus_bare_logger_calls(mod):
    import ast
    import pathlib
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def enclosing_func(node):
        cur = node
        while cur is not None:
            if isinstance(cur, ast.FunctionDef):
                return cur.name
            cur = parents.get(id(cur))
        return None

    out = []
    for node in ast.walk(tree):
        if enclosing_func(node) in _MECH_FUNCS:
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
           and node.func.attr == "log" and isinstance(node.func.value, ast.Name) \
           and node.func.value.id == "logger":
            lvl = "INFO"
            msg = ""
            if node.args and isinstance(node.args[0], (ast.JoinedStr, ast.Constant)):
                a0 = node.args[0]
                if isinstance(a0, ast.Constant):
                    msg = str(a0.value)
                else:
                    msg = "".join(v.value if isinstance(v, ast.Constant) else "{" for v in a0.values)
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                lvl = node.args[1].value
            if Logger.LEVELS.get(lvl, 2) >= Logger.LEVELS["INFO"]:
                out.append((node.lineno, msg))
    return out


@pytest.mark.parametrize("mod,allowed", [
    pytest.param(tmod, _MODULE_UNGROUPED_PREFIXES, id="module"),
    pytest.param(tstore, _STORE_UNGROUPED_PREFIXES, id="store"),
])
def test_no_bare_info_plus_outside_whitelist(mod, allowed):
    offenders = []
    for lineno, msg in _info_plus_bare_logger_calls(mod):
        if not msg.startswith(allowed):
            offenders.append((lineno, msg[:40]))
    assert not offenders, (
        "INFO+ 裸 logger.log 逃逸出无组白名单（应走 _tlog/_open_grp 归组，"
        f"或显式加入白名单并说明为何无组）：{offenders}")
