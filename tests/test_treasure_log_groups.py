# -*- coding: utf-8 -*-
"""treasure 组归属行为验收（契约 §8 第 7 步，A 案 + 裁定边界规则）。

锁三件事：
1. _tlog 开组期间归当前组，组终态按组内 ERROR/WARNING 自动推导；
2. logger 无 group()（测试 recorder 桩）时 _open_grp 回退 legacy 锚点行——现网
   行为在桩环境零破坏；
3. 停止路径显式 incomplete 与 has_error 并存（中断归中断、错误照留痕）。

结构性归属锁（INFO+ 裸日志必须带组或登记）已统一到
`tests/test_log_attribution.py` + `tests/log_sites.py`，本文件不再保留逐文件白名单。
"""
from types import SimpleNamespace

import pytest

try:
    import maaracing_master.plugins.treasure.module as tmod
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001 —— CI 轻依赖环境缺 maa/… 时整文件跳过
    _OK, _ERR = False, str(exc)

pytestmark = [pytest.mark.retired, pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖（maa/…）：{_ERR}")]

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
