# -*- coding: utf-8 -*-
"""日志站点登记处与全仓扫描器（归属 / 内容定界的机检真源）。

**登记单元 = 日志站点**：`(文件, 限定函数, 消息模板, 级别)` 四元组。白名单放行的
是**这个调用点**，不是这类文字——同一消息模板出现在同一函数的多个调用点，登记表
必须逐条对应（多重集相等），新增一处而不登记即红。

**覆盖口径**（与 `Logger.WRAPPER_FUNCS` 共享）：

- 直接调用 `logger.log(...)` 与它的别名（`lg = logger`、`log = logger.log`）；
- 跳过 `logger` 自身内部的转发帧；
- 级别下界取 `Logger.GUI_MIN_LEVEL`，动态级别（`level` 变量 / 条件表达式）按
  「可能达到 INFO」处理，必须登记；
- `group_id=None` 显式传参一律红（写了关键字就是绕开归属，不接受登记豁免）；
- 经「有组走组」包装（`_tlog` / `_mlog` / `_clog` / `_lg` / `_run_group`）的调用
  不在此表逐条登记——包装器自身的无组回退是登记项。

本模块只做静态判定与登记数据；测试与断言在 `tests/test_log_attribution.py`。
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from maaracing_master.core.logger import Logger

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "maaracing_master"

# 内容类别：阶段边界 / 状态变化 / 跨切面。`pending_migration` 是 S0 的过渡类别，
# 定界完成即清零，登记表里出现它即红。
CLASSES = ("phase", "state", "cross")
PENDING = "pending_migration"
ATTRIBUTIONS = ("cross", "wrapper", "migrated")

# 日志中「组」由业务层两处开：controller 运行生命周期 + 插件 session/phase。
ALLOWED_CORE_GROUP_CALLERS = {("core/controller.py", "_run_group")}


def rel_path(path: str | Path) -> str:
    """仓库相对 posix 路径（从仓库根解析，不依赖 cwd）。"""
    p = Path(path)
    try:
        return p.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return p.as_posix()


@dataclass(frozen=True)
class CallSite:
    file: str
    func: str
    template: str
    level: str
    group: str          # "group" | "none" | "missing"
    in_loop: bool

    @property
    def key(self) -> tuple:
        return (self.file, self.func, self.template, self.level)


@dataclass(frozen=True)
class LogSite:
    file: str
    func: str
    template: str
    level: str
    klass: str
    attribution: str
    reason: str

    @property
    def key(self) -> tuple:
        return (self.file, self.func, self.template, self.level)


def _level_of(call: ast.Call) -> str:
    node = None
    if len(call.args) >= 2:
        node = call.args[1]
    for kw in call.keywords:
        if kw.arg == "level":
            node = kw.value
    if node is None:
        return "INFO"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "<dynamic>"


def _template_of(call: ast.Call) -> str:
    if not call.args:
        return "<dynamic>"
    node = call.args[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            else:
                out.append("{}")
        return "".join(out)
    return "<dynamic>"


def _group_of(call: ast.Call) -> str:
    for kw in call.keywords:
        if kw.arg != "group_id":
            continue
        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
            return "none"
        return "group"
    return "missing"


def _qualified_name(node: ast.AST, parents: dict) -> str:
    names = []
    cur = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(cur.name)
        elif isinstance(cur, ast.ClassDef):
            names.append(cur.name)
        cur = parents.get(id(cur))
    return ".".join(reversed(names)) or "<module>"


def _in_loop(node: ast.AST, parents: dict) -> bool:
    cur = parents.get(id(node))
    while cur is not None and not isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(cur, (ast.For, ast.AsyncFor, ast.While)):
            return True
        cur = parents.get(id(cur))
    return False


def _logger_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """返回 (logger 名集合, logger.log 别名集合)。

    覆盖 `lg = logger`、`log = logger.log` 以及它们的传递赋值；不追踪跨模块重导出
    （本仓没有这种形态，出现时扫描会因「不是 Name.log」而漏，测试的注入矩阵会报警）。
    """
    loggers = {"logger"}
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                value = node.value
                if isinstance(value, ast.Attribute) and value.attr == "log" \
                        and isinstance(value.value, ast.Name) \
                        and value.value.id in loggers:
                    if target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
                elif isinstance(value, ast.Name) and value.id in loggers:
                    if target.id not in loggers:
                        loggers.add(target.id)
                        changed = True
    return loggers, aliases


def iter_calls(path: Path) -> list[CallSite]:
    """解析单个文件，返回全部直接日志调用点（含 DEBUG 与已归组，由调用方筛选）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    loggers, aliases = _logger_bindings(tree)
    out: list[CallSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_log = False
        if isinstance(func, ast.Attribute) and func.attr == "log" \
                and isinstance(func.value, ast.Name):
            name = func.value.id
            if name in loggers or name in aliases:
                is_log = True
        elif isinstance(func, ast.Name) and func.id in aliases:
            is_log = True
        if not is_log:
            continue
        out.append(CallSite(
            file=rel_path(path),
            func=_qualified_name(node, parents),
            template=_template_of(node),
            level=_level_of(node),
            group=_group_of(node),
            in_loop=_in_loop(node, parents),
        ))
    return out


def scan_package(root: Path = PACKAGE) -> list[CallSite]:
    """按目录发现（插件增删自动伸缩），不依赖 cwd。"""
    out: list[CallSite] = []
    for path in sorted(Path(root).rglob("*.py")):
        out.extend(iter_calls(path))
    return out


def needs_registration(site: CallSite) -> bool:
    """N1 的范围：INFO 及以上、未显式带组。动态级别按可能 ≥ INFO 处理。"""
    if site.group == "group":
        return False
    if site.level == "<dynamic>":
        return True
    return Logger.LEVELS.get(site.level, 2) >= Logger.LEVELS[Logger.GUI_MIN_LEVEL]


def registry_keys(sites) -> Counter:
    return Counter(s.key for s in sites)


def compile_template(template: str) -> re.Pattern:
    parts = [re.escape(p) for p in template.split("{}")]
    return re.compile(".*".join(parts))


def runtime_violations(records) -> list[str]:
    """运行时兜底：INFO+ 且 group_id=None 的记录必须落在登记模板上。

    记录本身不含调用点（热路径不做帧捕获），故运行时身份是「级别 + 消息模板」；
    模板歧义由静态锁的（级别, 模板）唯一性拦截。动态模板站点无法做运行时匹配，
    由静态锁负责。
    """
    literal = [(s, compile_template(s.template)) for s in SITES
               if s.template != "<dynamic>"]
    dynamic_levels = {s.level for s in SITES if s.template == "<dynamic>"}
    out = []
    for rec in records:
        if rec.get("event_type") not in (None, "log"):
            continue
        if rec.get("group_id") is not None:
            continue
        level = rec.get("level") or "INFO"
        if Logger.LEVELS.get(level, 2) < Logger.LEVELS[Logger.GUI_MIN_LEVEL]:
            continue
        if level in dynamic_levels and not any(
                s.level == level and s.template != "<dynamic>" for s in SITES):
            continue
        msg = rec.get("message") or ""
        if not any(s.level == level and rx.fullmatch(msg) for s, rx in literal):
            out.append(f"{level} {msg[:60]}")
    return out


# ---------------------------------------------------------------------------
#  站点登记处
# ---------------------------------------------------------------------------
# 每条 = (文件, 限定函数, 消息模板, 级别, 内容类别, 归属, 理由)。
# 归属：cross=真的没有归属可挂的跨切面事件；wrapper=「有组走组」包装器的无组回退；
# migrated=已迁流水（必须同时登记到 TRACE_MIGRATIONS 的去向）。逐帧站点降级为
# 「首次可见、其后 DEBUG」的，仍按 cross 登记并在理由里写明降级去向。
# 理由必须写清「为什么这行无组仍属状态/跨切面」，不能只写"有意无组"。

def _s(file, func, template, level, klass, attribution, reason) -> LogSite:
    if not file.startswith("maaracing_master/"):
        file = "maaracing_master/" + file
    return LogSite(file=file, func=func, template=template, level=level,
                   klass=klass, attribution=attribution, reason=reason)


SITES: list[LogSite] = [
    # ---- core/clicker.py ----
    _s('core/clicker.py', 'Clicker.auto_shoo', '[鉴宝点击] 避让空转：已连续 {} 次导航到 ({},{}) 光标仍压住 [{}]（当前位 ({},{})）——游戏可能不接受该落点，需人工核查', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/covers.py ----
    _s('core/covers.py', '_declared_file', '[covers] {}: COVER 应为字符串，已忽略: {}', 'WARNING', 'state', 'cross', '封面声明非法属插件配置错误：WARNING 不静默但封面是装饰、不阻断模块；解析在 RPC 线程，无业务组可挂。'),
    _s('core/covers.py', '_declared_file', '[covers] {}: COVER 必须是 resources/ 内的相对路径，已忽略: {}', 'WARNING', 'state', 'cross', '封面声明越界（绝对路径 / ..）：WARNING 不静默并拒绝解析，避免读到插件目录外的文件；解析在 RPC 线程，无业务组可挂。'),
    _s('core/covers.py', '_declared_file', '[covers] {}: 封面文件不存在，回落默认封面: {}', 'WARNING', 'state', 'cross', '声明了封面但文件丢失：降级到通用默认封面，WARNING 让「图没打进包」可见；解析在 RPC 线程，无业务组可挂。'),

    # ---- core/game_process.py ----
    _s('core/game_process.py', 'close_game', '[game] 按窗口关闭失败，改按进程名兜底: {}', 'WARNING', 'state', 'cross', '关闭降级路径：按窗口失败改按进程名，WARNING 不静默；工具函数无组上下文。'),

    # ---- core/controller.py ----
    _s('core/controller.py', '_NullGroup.log', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('core/controller.py', '_run_group', '<dynamic>', 'INFO', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('core/controller.py', '_clog', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('core/controller.py', 'MaaRacingMasterController.set_emergency_stop', '紧急停止快捷键已启用：同时按下任意 2 个及以上按键将立即停止逻辑', 'INFO', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/controller.py', 'MaaRacingMasterController.start_module', '模块执行异常: {}', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/controller.py', 'MaaRacingMasterController.stop', '停止模块异常: {}', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/controller.py', 'MaaRacingMasterController._emergency_stop_loop', '紧急停止：检测到同时按下 ≥2 个按键，立即停止逻辑', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/gamepad_cursor.py ----
    _s('core/gamepad_cursor.py', 'GamepadClicker.watchdog', '[手柄导航] 任务槽已占用 {}s（{}，导航仍在推进：{}s 前更新过进度）——期间点击与避让都会被跳过', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/gamepad_cursor.py', 'GamepadClicker.watchdog', '[手柄导航] 导航线程已退出但任务仍在（{}）——槽已回收，线程重启{}；该次点击未获确认，由提交方下帧重试', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/gamepad_cursor.py', 'GamepadClicker.watchdog', '[手柄导航] 导航进度停滞 {}s（{}，进度阶段={} seq={}）——判定导航卡住，取消本次任务（提交方下帧重试）', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/gamepad_cursor.py', 'GamepadClicker.watchdog', '[手柄导航] 任务槽占用 {}s 超过硬上限 {}s（{}）——判定导航空耗，取消本次任务（提交方下帧重试）', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/gamepad_cursor.py', 'GamepadClicker._nav_loop', '[手柄导航] 结果槽未消费即被覆盖（协议违例）：丢弃本次导航结果，保留待消费结果交主链路（槽已释放）', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/ocr.py ----
    _s('core/ocr.py', '_pin_to_p_cores', '[OCR] CPU 亲和性未生效({})——E-core 漂移防护缺位，OCR 尾部耗时可能偏高', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),
    _s('core/ocr.py', 'RapidOcrEngine._get_engine', '[OCR] RapidOCR 加载失败({})，OCR 已禁用', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),

    # ---- core/perf_contract.py ----
    _s('core/perf_contract.py', 'rows_for', '[perf] {} 契约版本 {} 不认识（期望 {}），领域面整体不显示', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/perf_contract.py', 'rows_for', '[perf] {} 声明行 {} 被拒：{}', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/perf_sampler.py ----
    _s('core/perf_sampler.py', 'PerfSampler._warn_slow_domain', '[perf] 领域行取值耗时 {}ms（阈值 {}ms）：模块取值口应纯读、常数时间', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/perf_sampler.py', 'PerfSampler.on_session_closing', '[perf] 线程标签声明未兑现（整场没匹配到线程）：{}', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/pipeline_logger.py ----
    _s('core/pipeline_logger.py', 'PipelineLogger.on_node_action', '[Pipeline] {}({}) → 动作❌失败', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/registry.py ----
    _s('core/registry.py', '_parse_validity', '[registry] 插件 {}: {}，按未声明处理', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/registry.py', '_discover_plugins', '[registry] 插件 {}: {} 非 ActivityModule 子类，跳过', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/registry.py', '_discover_plugins', '[registry] 插件 {} 加载失败，跳过: {}', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/sidecar.py ----
    # set_run_queue 的任务列表回显已降为 DEBUG（面板降噪，见 test_panel_noise_lines_are_file_only）。
    _s('core/sidecar.py', 'SidecarService.set_registry_optimization', '注册表优化项 {} {}（{}={}）', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_debug_mode', '调试截图模式: {}', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_file_logging', '日志记录(写盘): {}', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_peep', 'PEEP 实时预览: {}', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_click_mode', '点击方式: {}', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_intent_mode', '仅显示意图: {}', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_mute_game', '运行时静音游戏: {}', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_boost_priority', '性能优先({}): {}', '<dynamic>', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService._migrate_legacy_shutdown', '<dynamic>', 'INFO', 'cross', 'cross', '启动期一次性迁移回显：旧「运行结束后」开关折成任务步骤，发生在构造阶段、无业务组。'),
    _s('core/sidecar.py', '_save_profile', '[sidecar] 偏好落盘失败: {}', 'WARNING', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_start_from', '断点模式: {} 将从「{}」开始运行', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.start', '断点模式: 从 {} 开始运行', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService._check_sampler_stall', '[perf] 采样器已 {}s 未更新（采样线程可能卡死），面板数字为陈旧值', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/sidecar.py', 'SidecarService.set_registry_optimization', '注册表优化项 {} {}（protocol_command={}）', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_registry_optimization', '注册表优化项 {} {}（标记 {}={}）', 'INFO', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService._dispatch', 'sidecar dispatch 异常: {}', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/sidecar.py', 'SidecarService._check_sampler_stall', '[perf] 采样器恢复更新', 'INFO', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/sidecar.py', 'SidecarService.set_registry_optimization', '[sidecar] 数值优化项 {} 写入失败: {}', 'WARNING', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService._make_perf_reader.read', '[perf] 模块返回了未声明的键 {}，已丢弃', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('core/sidecar.py', 'SidecarService.set_registry_optimization', '[sidecar] 协议优化项 {} 写入失败: {}', 'WARNING', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService.set_registry_optimization', '[sidecar] 标记优化项 {} 写入失败: {}', 'WARNING', 'cross', 'cross', 'RPC 设置回显/派发异常：GUI 请求线程的跨切面事件，无业务组。'),
    _s('core/sidecar.py', 'SidecarService._worker_main', '运行异常: {}', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/sidecar.py', 'SidecarService.close', '[sidecar] 关闭时模块 worker 在 {}s 内未退出，按有界排空继续收尾（未收尾组记 incomplete）', 'WARNING', 'state', 'cross', '关闭排空超时的跨切面告警：关闭路径无业务组可挂；必须在关日志器前留下故障原因。'),

    # ---- core/template_match.py ----
    _s('core/template_match.py', 'load_template', '模板不存在: {}.png/.jpg（搜索 {} 个目录）', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/wgcap.py ----
    _s('core/wgcap.py', 'WgcCapture.start.on_closed', 'WGC 捕获窗口已关闭', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/wgcap.py', 'WgcCapture._compute_client_rect', 'WGC 客户区裁剪计算失败（整窗采集兜底）: {}', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/wgcap.py', 'WgcCapture._on_frame', 'WGC 帧处理异常（累计 {}）: {}', '<dynamic>', 'cross', 'cross', '逐帧重复已降级：首次 WARNING/ERROR，其后 DEBUG（文件仍全量，面板不刷屏）。'),
    _s('core/wgcap.py', 'WgcCapture._crop_client', 'WGC 客户区裁剪越界 offset={} size={} frame={}x{}（整窗下传）', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- core/window_utils.py ----
    _s('core/window_utils.py', 'verify_frame_client', '[坐标] 截图帧尺寸({}x{}) ≠ 客户区物理尺寸({}x{})，归一化→屏幕映射可能偏移（本链路应 1:1）', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('core/window_utils.py', 'find_game_hwnd._lg', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('core/window_utils.py', 'resize_game_window_720p._lg', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：未接组时回落散行，调用方决定内容。'),

    # ---- core/nav_graph.py ----
    _s('core/nav_graph.py', '_post_pipeline_merged._lg', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：启动段未接组时回落散行。'),
    _s('core/nav_graph.py', 'NavGraph._log', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：图未被插件接组时（测试/独立使用）回落散行。'),

    # ---- core/yolo_detector.py ----
    # __init__ 的设备/缓存加载判定已降为 DEBUG（面板降噪，见 test_panel_noise_lines_are_file_only）。
    _s('core/yolo_detector.py', 'YOLODetector.__call__', '模型输出类别维 {} ≠ 声明类别数 {}，请核对 classes 参数', 'WARNING', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),

    # ---- plugins/speedrush/hud.py ----
    _s('plugins/speedrush/hud.py', 'HudObserver._write_meta', '[极速狂飙] HUD meta 写入失败: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/speedrush/hud.py', 'HudObserver._write_worker', '[极速狂飙] HUD 落盘失败（累计 {}）: {}', '<dynamic>', 'cross', 'cross', '逐帧重复已降级：首次 WARNING/ERROR，其后 DEBUG（文件仍全量，面板不刷屏）。'),

    # ---- plugins/speedrush/module.py ----
    _s('plugins/speedrush/module.py', '_open_grp', '<dynamic>', 'INFO', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('plugins/speedrush/module.py', '_tlog', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('plugins/speedrush/module.py', '_fresh_session_dir', '[极速狂飙] 会话目录 {} 已存在且非空，改写到 {}', 'WARNING', 'cross', 'cross', '建目录先于业务组开启：撞名换名是异常但可恢复的降级，WARNING 不静默；工具函数无组上下文。'),
    _s('plugins/speedrush/module.py', '_tlog', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('plugins/speedrush/module.py', 'SpeedRushModule.start', '[极速狂飙] 窗口连接失败，模块终止', 'ERROR', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/speedrush/module.py', 'SpeedRushModule.start', '游戏窗口不是 16:9 比例（模板与识别区域均按 720p(16:9) 设计，其他比例会识别错位）。请调整为 16:9 后重新开始，模块已终止', 'ERROR', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/speedrush/module.py', 'SpeedRushModule.start', '[极速狂飙] 录制模式提示：当前点击方式为「后台手柄」，导航步骤会创建虚拟手柄，可能与你的物理手柄冲突（驾驶时手柄可能不响应）。建议在设置页改为「前台鼠标」再录。', 'WARNING', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),

    # ---- plugins/speedrush/recorder.py ----
    _s('plugins/speedrush/recorder.py', 'DriveRecorder._write_meta', '[极速狂飙] 录制 meta 写入失败: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/speedrush/recorder.py', 'DriveRecorder._frame_worker', '[极速狂飙] 录制写帧返回失败（累计 {}） seq={}', '<dynamic>', 'cross', 'cross', '逐帧重复已降级：首次 WARNING/ERROR，其后 DEBUG（文件仍全量，面板不刷屏）。'),
    _s('plugins/speedrush/recorder.py', 'DriveRecorder._frame_worker', '[极速狂飙] 录制写帧失败（累计 {}） seq={}: {}', '<dynamic>', 'cross', 'cross', '逐帧重复已降级：首次 WARNING/ERROR，其后 DEBUG（文件仍全量，面板不刷屏）。'),

    # ---- plugins/treasure/detector.py ----
    _s('plugins/treasure/detector.py', 'TreasureStageDetector.__init__', '[鉴宝检测器] 无可用 v4 DetectionPlan（policy.json 缺失/损坏），阶段检测跳过', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),
    _s('plugins/treasure/detector.py', 'TreasureStageDetector.__init__', '[鉴宝检测器] v4 DetectionPlan 加载失败: {}', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),
    _s('plugins/treasure/detector.py', 'TreasureStageDetector._match_score', '[鉴宝检测器] ROI 尺寸不足（{}/{} 搜索区 {}×{}），该模板永远无法命中，请调大 ROI', 'WARNING', 'state', 'cross', '配置缺陷（ROI 过小）按节流告警：是状态变化而非逐帧读数；检测 worker 无组可挂。'),

    # ---- plugins/treasure/module.py ----
    _s('plugins/treasure/module.py', '_open_grp', '<dynamic>', 'INFO', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('plugins/treasure/module.py', '_load_action_centers', '[鉴宝] policy 感知锚点 rect 读取失败: {}', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),
    _s('plugins/treasure/module.py', '_tlog', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('plugins/treasure/module.py', '_tlog', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('plugins/treasure/module.py', 'TreasureModule.start', '[鉴宝] 窗口连接失败，模块终止', 'ERROR', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/treasure/module.py', 'TreasureModule.start', '游戏窗口不是 16:9 比例（模板与识别区域均按 720p(16:9) 设计，其他比例会识别错位）。请将游戏窗口调整为 16:9 后重新开始，模块已终止', 'ERROR', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/treasure/module.py', 'TreasureModule.start', '[鉴宝] 未加载到动作按钮 rect，准星模式将不可用', 'WARNING', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/treasure/module.py', 'TreasureModule.start', '[鉴宝] 未加载任何鉴宝师头像模板（选择鉴宝师阶段将用点中心兜底）', 'WARNING', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/treasure/module.py', 'TreasureModule.start', '[鉴宝] 未加载「开始匹配」按钮模板（降级：始终先点目标场次 badge，再点开始匹配位置）', 'WARNING', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/treasure/module.py', 'TreasureModule.start', '[鉴宝] 未加载智能出价按钮模板（面板已开判定降级：依赖主按钮 OCR 兜底）', 'WARNING', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/treasure/module.py', 'TreasureModule.start', '[鉴宝] 未加载放弃确认弹窗模板（pass 二级确认将无法自动处置）', 'WARNING', 'cross', 'cross', '启动前提判定先于模块业务组开启：无归属可挂；失败即终止，必须留在面板上。'),
    _s('plugins/treasure/module.py', 'TreasureModule._init_policy_stack', '[鉴宝] policies 编译失败：{}', 'ERROR', 'state', 'cross', '状态跃迁/可恢复降级：WARNING 不静默；发生在组外线程，无归属可挂。'),
    _s('plugins/treasure/module.py', 'TreasureModule.cleanup', '[鉴宝][v4] 跑图器释放异常: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/treasure/module.py', 'TreasureModule._io_worker_loop', '[鉴宝] IO worker 异常: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/treasure/module.py', 'TreasureModule._ocr_worker_loop', '[鉴宝] OCR worker 异常: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),

    # ---- plugins/treasure/ocr.py ----
    _s('plugins/treasure/ocr.py', 'TreasureOcr._load_regions', '[鉴宝OCR] policy.json 真源不可用，识别区为空', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),

    # ---- plugins/treasure/store.py ----
    _s('plugins/treasure/store.py', 'TreasureStore._mlog', '<dynamic>', '<dynamic>', 'state', 'wrapper', '「有组走组」包装器的无组回退：测试桩/未开组路径才走到这里，归属与内容由调用方决定。'),
    _s('plugins/treasure/store.py', 'read_today_stats', '读取今日看板数据失败: {}', 'WARNING', 'cross', 'cross', 'GUI 今日看板读侧的跨切面事件（RPC 请求线程），无业务组。'),
    _s('plugins/treasure/store.py', 'TreasureStore._conn', '[鉴宝落盘] SQLite 初始化失败（数据不记录，不影响自动化）: {}', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),
    _s('plugins/treasure/store.py', 'TreasureStore.close_db', '[鉴宝落盘] 关闭连接失败: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/treasure/store.py', 'TreasureStore.flush_game_record', '[鉴宝落盘] 写入失败: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/treasure/store.py', 'TreasureStore.record_egg_claim', '[鉴宝落盘] 领取数含负值，拒写: {} coin={} score={}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/treasure/store.py', 'TreasureStore.record_egg_claim', '[鉴宝落盘] 领取蛋数写入失败: {}', 'WARNING', 'cross', 'cross', '常驻 worker/回调线程的跨切面事件：无稳定业务组可挂。'),
    _s('plugins/treasure/store.py', 'TreasureStore.log_session_summary', '<dynamic>', 'INFO', 'state', 'cross', '测试桩无 _open_grp 时的 legacy 逐行回退：正式模块走组句柄，不走这里。'),
    _s('plugins/treasure/store.py', 'TreasureStore._conn', '[鉴宝落盘] games 表迁移失败（strategy_mode 列缺失）: {}', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),
    _s('plugins/treasure/store.py', 'TreasureStore._conn', '[鉴宝落盘] daily_summary 迁移失败（egg_coin/egg_score 列）: {}', 'WARNING', 'cross', 'cross', '初始化/加载期判定：业务组尚未开启，属跨切面。'),
]

# 迁到玩法流水的站点：key（四元组）→ (流水模块, 判别字段, 所属阶段)。
# 站点的日志行必须已移除；结构测试据此断言「每次发生都产生带判别字段的流水记录」。
# 当前审计未发现仍需从日志迁往流水的 INFO+ 站点（逐帧读数已由 ADR-0010 的
# trace/hud/frames 流水承载），故表为空；新增迁移必须连同站点一起登记。
TRACE_MIGRATIONS: dict[tuple, tuple[str, str, str]] = {}
