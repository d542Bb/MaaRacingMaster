# -*- coding: utf-8 -*-
"""日志归属与内容定界的机检锁（N1 / N2 / N3 / N9）。

三条硬判定：

1. **归属登记完备**：`maaracing_master/**` 内 INFO 及以上的直接日志调用，要么显式
   传 `group_id`，要么落在 `tests/log_sites.py` 的站点登记表里；`group_id=None`
   显式传参一律红。登记表与调用点按四元组多重集相等——新增调用点不登记、旧条目
   失配都会红。
2. **白名单按站点登记**：每条登记必须带理由、内容类别与归属；`(级别, 消息模板)`
   在登记表内唯一（运行时身份是级别 + 模板，歧义由静态锁拦截）；`pending_migration`
   必须清零。包装器条目与 `Logger.WRAPPER_FUNCS` 共享同一份跳过清单。
3. **组只由业务层两处开**：core 仅 controller 的运行生命周期组可调 `logger.group()`。

另有运行时兜底与降级分项计数的行为锁；静态锁是站点身份的真源，运行时只证「面板
上层无组可见行都在册」。
"""
from __future__ import annotations

import ast
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from maaracing_master.core.logger import Logger
from tests import log_sites as L


# ---------------------------------------------------------------------------
#  N1 / N2 / N9：静态登记完备性
# ---------------------------------------------------------------------------

def test_info_plus_sites_are_registered():
    calls = L.scan_package()
    explicit_none = [c for c in calls if c.group == "none"]
    assert not explicit_none, (
        "显式传 group_id=None 等于绕开归属（写了关键字就是声明了归属）：\n"
        + "\n".join(f"{c.file}:{c.func} {c.template[:50]}" for c in explicit_none))

    need = [c for c in calls if L.needs_registration(c)]
    got = Counter(c.key for c in need)
    want = L.registry_keys(L.SITES)
    missing, stale = got - want, want - got
    detail = []
    for key, n in missing.items():
        detail.append("未登记：" + f"{key[0]}::{key[1]} [{key[3]}] {key[2][:60]}" + (f" ×{n}" if n > 1 else ""))
    for key, n in stale.items():
        detail.append("失效条目：" + f"{key[0]}::{key[1]} [{key[3]}] {key[2][:60]}" + (f" ×{n}" if n > 1 else ""))
    assert not detail, "日志站点登记表与代码不一致：\n" + "\n".join(detail)


def test_registry_entries_are_complete_and_runtime_key_unique():
    for site in L.SITES:
        assert site.reason.strip(), f"{site.file}::{site.func} 登记缺理由"
        assert site.klass in L.CLASSES, f"{site.file}::{site.func} 类别非法: {site.klass}"
        assert site.attribution in L.ATTRIBUTIONS, \
            f"{site.file}::{site.func} 归属非法: {site.attribution}"
        assert (L.REPO / site.file).is_file(), f"登记文件不存在: {site.file}"
    assert not [s for s in L.SITES if s.klass == L.PENDING], "pending_migration 必须清零"

    literal = [(s.level, s.template) for s in L.SITES if s.template != "<dynamic>"]
    dup = [k for k, n in Counter(literal).items() if n > 1]
    assert not dup, f"同一 (级别, 消息模板) 对应多个站点，运行时身份歧义：{dup}"


def test_wrapper_entries_share_logger_skip_list():
    wrappers = {s.func.rsplit(".", 1)[-1] for s in L.SITES
                if s.attribution == "wrapper"}
    assert wrappers, "包装器回退站点必须登记（Scanner 的包装器清单不能为空）"
    assert wrappers <= Logger.WRAPPER_FUNCS, (
        "登记处的包装器与 Logger.WRAPPER_FUNCS 不是同一份清单："
        f"{sorted(wrappers - Logger.WRAPPER_FUNCS)}")


def test_trace_migrations_are_declared_and_backed_by_sites():
    """迁到流水的站点必须同时出现在登记表与迁移表（去向/判别字段/阶段齐备）。"""
    keys = {s.key for s in L.SITES}
    migrated = {s.key for s in L.SITES if s.attribution == "migrated"}
    assert migrated == set(L.TRACE_MIGRATIONS), (
        "attribution=migrated 的站点与迁移表不一致："
        f"缺去向 {sorted(migrated - set(L.TRACE_MIGRATIONS))}，"
        f"失效条目 {sorted(set(L.TRACE_MIGRATIONS) - keys)}")
    for key, (module, field, stage) in L.TRACE_MIGRATIONS.items():
        assert module and field and stage, f"迁移去向不完整: {key}"


def test_loop_sites_are_state_or_cross_not_phase():
    by_key: dict = {}
    for site in L.SITES:
        by_key.setdefault(site.key, []).append(site)
    offenders = []
    for call in L.scan_package():
        if not L.needs_registration(call) or not call.in_loop:
            continue
        entries = by_key.get(call.key)
        assert entries, f"循环体内的日志调用未登记：{call.file}::{call.func}"
        if any(e.klass == "phase" for e in entries):
            offenders.append(f"{call.file}::{call.func} {call.template[:50]}")
    assert not offenders, "循环体内不得登记为 phase（逐帧细节应 state/cross 或迁流水）：\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
#  N3：core 不得自己开组
# ---------------------------------------------------------------------------

def _core_group_callers() -> list[tuple[str, str]]:
    callers = []
    for path in sorted((L.PACKAGE / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node
        loggers, _aliases = L._logger_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "group" \
                    and isinstance(func.value, ast.Name) and func.value.id in loggers:
                rel = L.rel_path(path).split("maaracing_master/", 1)[-1]
                callers.append((rel, L._qualified_name(node, parents)))
    return callers


def test_core_only_controller_opens_groups():
    callers = _core_group_callers()
    assert callers, "controller 的运行生命周期组必须被扫到（扫描规则失效？）"
    extra = [c for c in callers if c not in L.ALLOWED_CORE_GROUP_CALLERS]
    assert not extra, f"core 除 controller 生命周期组外不得开组：{extra}"


# ---------------------------------------------------------------------------
#  扫描器注入矩阵（双向验证：漏报与误报都要红）
# ---------------------------------------------------------------------------

_SAMPLE = '''
from maaracing_master.core.logger import logger

def direct():
    logger.log("裸直调")

def aliased():
    log = logger.log
    log("别名调用")

def explicit_none():
    logger.log("显式 None", group_id=None)

def log():
    logger.log("函数名叫 log 也不放过")

def looped():
    for _ in range(2):
        logger.log("循环体内直调")

def grouped(gid):
    logger.log("有组", group_id=gid)

def dynamic_level(level):
    logger.log("动态级别", level)
'''


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_scanner_injection_matrix(tmp_path):
    path = _write(tmp_path, "maaracing_master/plug/x.py", _SAMPLE)
    calls = L.iter_calls(path)
    by_func = {}
    for call in calls:
        by_func.setdefault(call.func, []).append(call)

    assert "direct" in by_func and by_func["direct"][0].group == "missing"
    assert "aliased" in by_func, "别名 log = logger.log 必须被追踪"
    assert by_func["explicit_none"][0].group == "none"
    assert "log" in by_func, "函数名叫 log 时不得漏报"
    assert by_func["looped"][0].in_loop is True
    assert by_func["grouped"][0].group == "group"
    assert by_func["dynamic_level"][0].level == "<dynamic>"
    assert L.needs_registration(by_func["dynamic_level"][0]), "动态级别按可能 INFO 处理"


def test_scanner_discovers_new_directories(tmp_path):
    _write(tmp_path, "maaracing_master/plugins/newpkg/mod.py",
           'from maaracing_master.core.logger import logger\n'
           'def f():\n    logger.log("新插件目录")\n')
    _write(tmp_path, "maaracing_master/core/x.py",
           'from maaracing_master.core.logger import logger\n'
           'def g():\n    logger.log("core 新文件")\n')
    files = {c.file.split("/")[-1] for c in L.scan_package(tmp_path / "maaracing_master")}
    assert {"mod.py", "x.py"} <= files


# ---------------------------------------------------------------------------
#  运行时兜底：面板上层无组可见行必须落在登记模板上
# ---------------------------------------------------------------------------

def test_runtime_ungrouped_records_match_registered_template():
    site = next(s for s in L.SITES if s.template != "<dynamic>" and s.level == "WARNING")
    msg = site.template.replace("{}", "42")
    ok = {"event_type": "log", "group_id": None, "level": "WARNING", "message": msg}
    assert not L.runtime_violations([ok])
    bad = {"event_type": "log", "group_id": None, "level": "WARNING", "message": "未登记的消息"}
    assert L.runtime_violations([bad])
    grouped = {"event_type": "log", "group_id": "g-1", "level": "WARNING", "message": "未登记的消息"}
    assert not L.runtime_violations([grouped])
    debug = {"event_type": "log", "group_id": None, "level": "DEBUG", "message": "未登记的消息"}
    assert not L.runtime_violations([debug])


# ---------------------------------------------------------------------------
#  N8④：降级分项计数按调用点归因
# ---------------------------------------------------------------------------

def test_unknown_group_is_counted_per_business_call_site(tmp_path):
    lg = Logger(tmp_path)

    def business():
        lg.log("迟到行", group_id="g-missing")

    def _tlog(msg, gid):
        lg.log(msg, group_id=gid)

    def wrapped():
        _tlog("包装器转发", "g-missing")

    def business_end():
        lg.end_group("g-missing")

    business()
    wrapped()
    business_end()
    assert lg.internal_counters["unknown_group_id"] == 2
    assert lg.internal_counters["unknown_group_end"] == 1
    sites = lg.degraded_sites
    assert any(k.startswith("log@") and k.endswith(":business") for k in sites), sites
    assert any(k.startswith("log@") and k.endswith(":wrapped") for k in sites), (
        "包装器帧必须跳过，落到首个业务调用点", sites)
    assert not any(":_tlog" in k for k in sites), sites
    assert any(k.startswith("end@") and k.endswith(":business_end") for k in sites), sites


# ---------------------------------------------------------------------------
#  N8 关闭时序：拒绝新任务 → 停止 → 有界 join → 关日志
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alive_after", [True, False])
def test_sidecar_close_drains_worker_before_logger_close(monkeypatch, alive_after):
    sc = pytest.importorskip("maaracing_master.core.sidecar")
    events = []

    class _Worker:
        def __init__(self):
            self._alive = True

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            events.append(("join", timeout))
            self._alive = alive_after

    class _Controller:
        def stop(self):
            events.append(("stop",))

    class _Logger:
        def log(self, msg, level="INFO", **kw):
            events.append(("log", msg, level))

        def close(self):
            events.append(("close",))

    svc = object.__new__(sc.SidecarService)
    svc._lock = threading.RLock()
    svc._closed = False
    svc._worker = _Worker()
    svc._controller = _Controller()
    monkeypatch.setattr(sc, "logger", _Logger())
    svc.close({})
    assert svc._closed is True
    assert events[0] == ("stop",)
    assert events[1] == ("join", svc.WORKER_JOIN_TIMEOUT_S)
    assert events[-1] == ("close",), "logger 必须在 worker 排空之后关"
    assert 0 < svc.WORKER_JOIN_TIMEOUT_S <= 5.0, "join 必须有界"
    warnings = [e for e in events if e[0] == "log" and e[2] == "WARNING"]
    if alive_after:
        assert warnings and "未退出" in warnings[0][1], \
            "join 超时必须留下明确的 worker 未退出告警"
    else:
        assert not warnings


# ---------------------------------------------------------------------------
#  N4（生产侧）：正常结束的运行不得留悬空 running 组
# ---------------------------------------------------------------------------

def _controller_env(tmp_path, monkeypatch, start):
    ctl = pytest.importorskip("maaracing_master.core.controller")
    lg = Logger(tmp_path)

    class _Module:
        STAGE_ORDER = ["待机"]

        def required_capabilities(self, config):
            return set()

        def start(self, start_from):
            start()

        def cleanup(self):
            pass

    monkeypatch.setattr(ctl, "logger", lg)
    monkeypatch.setattr(ctl, "create_module", lambda mid, ctx: _Module())
    c = object.__new__(ctl.MaaRacingMasterController)
    c._lifecycle_lock = threading.Lock()
    c.stop_event = threading.Event()
    c._active_module = None
    c._running = False
    c._manual_stop = False
    c._mute_game_enabled = False
    c._last_run_natural = False
    c._hwnd = 12345
    c._ctx = None
    c._wgc_capture = None
    c.controller = None
    c._destroy_gpad = lambda grp=None: None
    c._stop_wgc_capture = lambda: None
    c._start_wgc_capture = lambda grp=None: None
    monkeypatch.setattr(type(c), "ctx",
                        property(lambda self: SimpleNamespace(capabilities=set())))
    return c, lg


def _assert_runtime_records_registered(lg):
    records = [rec for _seq, rec in lg._lines]
    assert not L.runtime_violations(records), (
        "面板上层出现了未登记的裸 INFO+ 记录："
        f"{L.runtime_violations(records)}")
    assert lg.degraded_sites == {}, f"正常路径出现组号降级: {lg.degraded_sites}"
    assert "unknown_group_id" not in lg.internal_counters
    assert "unknown_group_end" not in lg.internal_counters


def test_normal_run_leaves_no_dangling_group(tmp_path, monkeypatch):
    c, lg = _controller_env(tmp_path, monkeypatch, start=lambda: None)
    c.start_module("fake")
    assert lg._groups == {}, f"正常结束仍留悬空组: {lg._groups}"
    starts = [r for _s, r in lg._lines if r["event_type"] == "group_start"]
    ends = [r for _s, r in lg._lines if r["event_type"] == "group_end"]
    assert len(starts) == len(ends) and starts
    _assert_runtime_records_registered(lg)
    lg.close()
    assert not [r for _s, r in lg._lines
                if r["event_type"] == "group_end" and r["outcome"] == "incomplete"], \
        "正常结束的运行不得靠 close() 补 incomplete"


def test_file_projection_unchanged_by_grouping():
    """N7：归组只新增 ::group:: / ::endgroup:: 标记，日志行字节不变。"""
    base = {"ts": "12:00:00", "level": "INFO", "message": "同一行",
            "event_type": "log", "group_id": None, "title": None, "outcome": None}
    assert Logger._project_file(base) == "[12:00:00] [INFO] 同一行"
    assert Logger._project_file({**base, "group_id": "g-1"}) == "[12:00:00] [INFO] 同一行"
    assert Logger._project_file({**base, "event_type": "group_start",
                                 "title": "阶段"}) == "::group:: 阶段"
    assert Logger._project_file({**base, "event_type": "group_end",
                                 "outcome": "success"}) == "::endgroup:: success"


def test_module_exception_is_registered_at_runtime(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    c, lg = _controller_env(tmp_path, monkeypatch, start=boom)
    c.start_module("fake")
    assert lg._groups == {}
    _assert_runtime_records_registered(lg)


# ---------------------------------------------------------------------------
#  S1：逐帧重复降级（首次可见，其后只留文件）
# ---------------------------------------------------------------------------

def test_wgcap_frame_error_is_edge_triggered(monkeypatch):
    wg = pytest.importorskip("maaracing_master.core.wgcap")

    class _Rec:
        def __init__(self):
            self.lines = []

        def log(self, msg, level="INFO", channel=None, **kw):
            self.lines.append((level, str(msg)))

    rec = _Rec()
    monkeypatch.setattr(wg, "logger", rec)
    cap = object.__new__(wg.WgcCapture)
    cap._stopped = False
    cap._max_interval_ns = 0
    cap._last_accept_ts_ns = None
    cap._frame_errors = 0

    class _BadFrame:
        @property
        def frame_buffer(self):
            raise RuntimeError("boom")

    cap._on_frame(_BadFrame())
    cap._on_frame(_BadFrame())
    assert [lv for lv, _m in rec.lines] == ["ERROR", "DEBUG"], \
        "帧回调异常必须首次 ERROR、其后 DEBUG（否则 60fps 刷满面板）"
