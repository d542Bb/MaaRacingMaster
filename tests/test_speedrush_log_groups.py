# -*- coding: utf-8 -*-
"""speedrush 组归属行为验收（与 treasure 同构，A 案 + 维护者裁定边界）。

锁三件事：
1. _tlog 开组期间归当前组，组终态按组内 ERROR/WARNING 自动推导；
2. logger 无 group()（测试桩）时 _open_grp 回退 legacy 锚点行——桩环境零破坏；
3. 停止路径显式 incomplete 与组内错误并存（中断归中断、错误照留痕）；
4. 线性组链：每一行 group_start 都被下一个开组或 finally 收口，正常序列不留 running。

结构性归属锁（INFO+ 裸日志必须带组或登记）已统一到
`tests/test_log_attribution.py` + `tests/log_sites.py`，本文件不再保留逐文件白名单。
"""
from types import SimpleNamespace

import pytest

try:
    import maaracing_master.plugins.speedrush.module as smod
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖（maa/…）：{_ERR}")

from maaracing_master.core.logger import Logger  # noqa: E402


def _recs(lg):
    return [r for _s, r in lg._lines]


def test_tlog_routes_to_open_group_and_derives_warning(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(smod, "logger", lg)
    m = SimpleNamespace()
    smod._open_grp(m, "[极速狂飙] 驾驶阶段 1（录制采集）", "phase")
    smod._tlog(m, "已进入驾驶页 frame=100 age=12ms")
    smod._tlog(m, "录制器启动失败: RuntimeError()", "WARNING")
    smod._end_grp(m)
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
    monkeypatch.setattr(smod, "logger", rec)
    m = SimpleNamespace()
    smod._open_grp(m, "[极速狂飙] 模块启动（录制开·感知关）", "session")
    assert rec.lines == [("INFO", "[极速狂飙] 模块启动（录制开·感知关）")]  # legacy 锚点行
    assert getattr(m, "_log_grp", None) is None                            # 不持有句柄
    smod._tlog(m, "普通行", "WARNING")
    assert ("WARNING", "普通行") in rec.lines


def test_stop_path_incomplete_keeps_error_flag(tmp_path, monkeypatch):
    lg = Logger(tmp_path)
    monkeypatch.setattr(smod, "logger", lg)
    m = SimpleNamespace()
    smod._open_grp(m, "驾驶阶段 1", "phase")
    smod._tlog(m, "感知初始化失败", "ERROR")
    smod._end_grp(m, "incomplete")   # 停止路径（start finally / _drive finally）
    end = [r for r in _recs(lg) if r["event_type"] == "group_end"][-1]
    assert end["outcome"] == "incomplete" and end["has_error"] is True


def test_linear_group_chain_no_dangling(tmp_path, monkeypatch):
    """线性组链（启动 session → 场次 session → 阶段 phase）：每一行 group_start
    都被下一个开组或 finally 收口，桩里走一遍真实边界序列不得留 running。"""
    lg = Logger(tmp_path)
    monkeypatch.setattr(smod, "logger", lg)
    m = SimpleNamespace()
    smod._open_grp(m, "模块启动", "session")
    smod._open_grp(m, "第 1 场开始", "session")   # 自动收口"模块启动"
    smod._open_grp(m, "驾驶阶段 1", "phase")      # 自动收口"第 1 场"
    smod._tlog(m, "已离开对局")
    smod._end_grp(m)                              # _drive finally 收口 phase
    smod._end_grp(m, "incomplete")                # start finally：槽已空，必须 no-op
    gend = [r for r in _recs(lg) if r["event_type"] == "group_end"]
    assert [r["title"] for r in gend] == ["模块启动", "第 1 场开始", "驾驶阶段 1"]
    assert [r["outcome"] for r in gend] == ["success", "success", "success"]
    assert len(gend) == 3                          # 无重复 end / 无迟到 incomplete 落组
