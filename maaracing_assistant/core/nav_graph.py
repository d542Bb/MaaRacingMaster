#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跳转图：把「大厅 → 模块入口页」这类页面跳转交给 MAA Pipeline 跑。

设计只有三条：
  1. 地图（节点/边/阈值/ROI）写在 pipeline JSON 里，不写在 Python 里；
  2. 框架负责跑图循环（截图-识别-动作-走边-超时重试），我们只提供两个桥：
       MRA_Template  识别：在宿主帧上找模板，把命中框交给框架
       MRA_Click     动作：把命中框中心交给 core.clicker.Clicker（自动按
                     click_mode 分派 前台鼠标 / 后台手柄导航+A / 意图不确认）
  3. 目标坐标永远来自识别框，不来自代码里写死的百分比。

于是新增一个活动模块的导航 = 写一份 pipeline 真源（见下），不需要再写一遍
导航匹配代码；游戏把入口挪了位置 = 换模板图或改 roi，不需要动 Python。

导航真源（v4，P4b 收口形态）：
    页面/锚点/入口链/出价策略等一切导航地图 → 三份 v4 真源
        core/resources/pipeline/global.json                    跨模块共用段（大厅骨架）
        plugins/<id>/resources/pipeline/<id>.json              模块图节点
        plugins/<id>/resources/policy/<id>.policy.json         感知规格+决策规则+执行资产
    图节点经 MaaFW Resource 原生执行；数据面经 `core/navkit/v4_source.load_nav_source`
    在 Python 侧直读（detector/决策栈/模板装载器共用）。校验走 `tools/navkit/check_truth.py`。

本文件提供 MAA Pipeline 执行通路（`Resource.post_pipeline` + 自定义识别/动作桥）
与 `NavKitV4` 常驻图宿主（P2a 接线，treasure v4 通路的执行底座）；
新模块接入导航请走上面的 v4 真源，不要再另建一套手写入口链。

资源一律留在程序目录内（便携包解压在哪资源就在哪），不往 C 盘复制。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from maa.controller import CustomController
from maa.resource import Resource
from maa.tasker import Tasker

from maaracing_assistant.core.clicker import Clicker
from maaracing_assistant.core.logger import logger
from maaracing_assistant.core.pipeline_logger import PipelineLogger
from maaracing_assistant.core.window_utils import is_foreground
from maaracing_assistant.core.template_match import (
    DEFAULT_SCALES,
    color_assert_ok,
    cursor_box_norm,
    find_any,
    find_any_cs,
    occlusion_ratio,
    strip_ext,
)

# core 自带资源根（stick_speed_model.json 也在这，不新开目录约定）
CORE_RES_DIR = Path(__file__).resolve().parent / "resources"

RECOGNIZER_NAME = "MRA_Template"
ACTION_NAME = "MRA_Click"


def _parse(raw: str) -> dict:
    """框架传进来的节点参数是 JSON 字符串，坏数据按空参数处理（图会走 on_error）。"""
    try:
        val = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


class TemplateRecognizer(CustomRecognition):
    """识别桥（v4 参数面）：MRA_Template 的引擎实现，编排 template_match 引擎件。

    节点参数（迁移器产出，契约见 tools/navkit/schema/custom.recognition.schema.json）：
        mode           template | point（point 识别走 guard 模板，点击走 target）
        rect           [x0,y0,x1,y1] 归一化搜索区
        templates      ["x.png"] 主模板（point 无此键）
        threshold      匹配下限（缺省 0.75）
        arbitration    {"template_thresholds": {名: 阈}} 逐模板阈值（互斥模板族）
        guard          {"templates","rect","threshold"?} 保险丝（主命中后守卫必须同帧命中）
        colorspace     rgb | gray | rgb_strict（默认 rgb；strict = 分通道 NCC 取最低分）
        color_assert   {"rect": 命中框内归一化子矩形, "hue": [lo,hi]} 色相校验
        mask_cursor    true 时启用光标遮挡过滤（P4c 起 cursor_pos_provider 已由
                       桥宿主接线：手柄导航器最近识别位；手柄未绑定/real 模式
                       返回 None 即按无光标处理。各节点是否开启按遮挡证据标定）
        max_occlusion  命中框被光标覆盖占比上限（默认 0.4）
        critical/_park L2 驻留握手字段（P2b 接导航 ACK 通道，当前不激活）

    帧源：优先 argv.image（WgcapController 注入帧，BGR）；缺省回退
    graph.frame()（ctx.capture 直读）。
    """

    def __init__(self, graph: "NavGraph", *, cursor_pos_provider=None):
        super().__init__()
        self._graph = graph
        self._cursor_pos_provider = cursor_pos_provider  # () -> (cx, cy) 归一化 | None

    def analyze(self, context, argv):
        p = _parse(argv.custom_recognition_param)
        frame = getattr(argv, "image", None)
        if frame is None:
            frame = self._graph.frame()
        if frame is None:
            return self.AnalyzeResult(box=None, detail={"error": "截图失败"})
        import cv2
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # 注入帧为 BGR（框架契约）
        H, W = rgb.shape[:2]

        names = [n for n in p.get("templates", []) if isinstance(n, str) and n]
        guard = p.get("guard") or {}
        if not names:
            if not guard.get("templates"):
                logger.log(f"[v4] 节点「{argv.node_name}」无 templates 且无 guard，判为识别失败", "ERROR")
                return self.AnalyzeResult(box=None, detail={"error": "无识别依据"})
            names = [strip_ext(n) for n in guard["templates"]]  # point：识别 = guard 模板
        g_rect = guard.get("rect")
        # 搜索区：point 模式用 guard 区（识别对象是守卫模板）；template 模式用 rect
        s_rect = (g_rect if (g_rect and not p.get("templates")) else p.get("rect")) or (0, 0, 1, 1)

        x1, y1, x2, y2 = s_rect
        roi = (int(x1 * W), int(y1 * H), int((x2 - x1) * W), int((y2 - y1) * H))
        arb = (p.get("arbitration") or {}).get("template_thresholds") or None
        box, score, hit_name = find_any_cs(
            rgb, names, self._graph.image_dirs,
            colorspace=p.get("colorspace", "rgb"),
            threshold=float(p.get("threshold", 0.75)),
            thresholds=arb, roi=roi)
        if box is None:
            logger.log(f"[v4] 「{argv.node_name}」识别未命中（ROI 内最高分 "
                       f"{score:.3f}，模板 {names}）", "DEBUG")
            return self.AnalyzeResult(box=None, detail={"score": round(score, 3)})

        if guard.get("templates") and p.get("templates"):
            # template+guard：主命中后守卫必须同帧命中（保险丝）；point 的识别
            # 本身就是 guard 模板，无需重复校验。
            gx1, gy1, gx2, gy2 = g_rect or (0, 0, 1, 1)
            g_roi = (int(gx1 * W), int(gy1 * H), int((gx2 - gx1) * W), int((gy2 - gy1) * H))
            g_box, _gs, _gn = find_any_cs(
                rgb, [strip_ext(t) for t in guard["templates"]], self._graph.image_dirs,
                colorspace=p.get("colorspace", "rgb"),
                threshold=float(guard.get("threshold", 0.75)), roi=g_roi)
            if g_box is None:
                logger.log(f"[v4] 「{argv.node_name}」主命中但守卫未同帧出现（保险丝）", "DEBUG")
                return self.AnalyzeResult(box=None, detail={"blocked_by": "guard"})

        if p.get("color_assert"):
            ca = p["color_assert"]
            if not color_assert_ok(rgb, box, ca["rect"], ca["hue"]):
                logger.log(f"[v4] 「{argv.node_name}」命中但色相断言未过", "DEBUG")
                return self.AnalyzeResult(box=None, detail={"blocked_by": "color_assert"})

        if p.get("mask_cursor") and self._cursor_pos_provider is not None:
            pos = self._cursor_pos_provider()
            if pos is not None:
                cbox = cursor_box_norm(pos[0], pos[1], frame_w=W, frame_h=H)
                ratio = occlusion_ratio(box, cbox)
                if ratio > float(p.get("max_occlusion", 0.4)):
                    logger.log(f"[v4] 「{argv.node_name}」命中框被光标遮挡 {ratio:.0%}，拒绝", "DEBUG")
                    return self.AnalyzeResult(box=None, detail={"blocked_by": "cursor",
                                                                "occlusion": round(ratio, 2)})

        return self.AnalyzeResult(
            # MaaFW rect 契约 = (x, y, w, h)（框架按此归一/裁剪，越界会 clip）；
            # 内部匹配全程 (x1,y1,x2,y2)，只在框架边界转换。
            box=(box[0], box[1], box[2] - box[0], box[3] - box[1]),
            detail={"template": hit_name, "score": round(score, 3), "name": argv.node_name})


class ClickAction(CustomAction):
    """动作桥：识别框中心 → Clicker 点击（前台鼠标 / 后台手柄导航+A / 意图）。

    节点参数：
        wait_after_ms  800    点击后的停顿（页面动画）
        timeout_s      20.0   手柄导航到位的等待上限（超时算失败，走 on_error）
    """

    def __init__(self, graph: "NavGraph"):
        super().__init__()
        self._graph = graph

    def run(self, context, argv):
        p = _parse(argv.custom_action_param)
        rx, ry, rw, rh = argv.box  # MaaFW rect 契约 (x, y, w, h)
        W, H = self._graph.frame_size()
        if W <= 0 or H <= 0:
            # v4 识别走 argv.image（注入帧），从不触 graph.frame()，_last_frame
            # 无更新方——这里补取一帧只为尺寸（WGC 缓存同源同尺寸，归一化不偏移）。
            self._graph.frame()
            W, H = self._graph.frame_size()
        if W <= 0 or H <= 0:
            logger.log(f"[跳转图] 节点「{argv.node_name}」拿不到帧尺寸，点击放弃", "WARNING")
            return False
        cx, cy = (rx + rw / 2) / W, (ry + rh / 2) / H
        box_norm = (rw / W, rh / H)
        logger.log(f"[v4] 「{argv.node_name}」点击 rect=({rx},{ry},{rw},{rh}) "
                   f"帧 {W}x{H} 归一化=({cx:.3f},{cy:.3f})", "DEBUG")

        ok = self._graph.click(cx, cy, box_norm,
                               timeout_s=float(p.get("timeout_s", 20.0)))
        self._graph.ctx.lifecycle.sleep(float(p.get("wait_after_ms", 800)) / 1000.0)
        if not ok:
            logger.log(f"[跳转图] 节点「{argv.node_name}」点击未到位", "WARNING")
        return ok


class NavGraph:
    """一个模块的跳转图实例：装资源、注册桥、跑一段图并返回成败。

    用法（模块内）：
        self.graph = NavGraph(self.ctx)
        self.graph.add_plugin(RES_DIR / "pipeline", RES_DIR / "image")
        self.graph.run("<模块名>_从大厅进入", reached="已到达<模块>页")  # 公共段
        self.graph.run("<模块名>_开始挑战", reached="已到达<下一目标页>")    # 模块段
    """

    CLICK_POLL_S = 0.05   # 点击结果轮询间隔
    FOREGROUND_WARN_S = 5.0  # 前台校验告警节流（dwell 重试 600ms 一次，防空降）

    def __init__(self, ctx):
        self.ctx = ctx
        self._last_fg_warn = 0.0
        self.image_dirs = [CORE_RES_DIR / "image"]
        # core 侧公共图目录（global 真源）按存在性纳入：目录不存在时
        # 不该让 load() 对着不存在的路径去 post_pipeline。
        core_pipeline = CORE_RES_DIR / "pipeline"
        self._pipeline_dirs = [core_pipeline] if core_pipeline.is_dir() else []
        self._resource = Resource()
        self._tasker = Tasker()
        self._resource.register_custom_recognition(
            RECOGNIZER_NAME, TemplateRecognizer(self, cursor_pos_provider=self.cursor_pos))
        self._resource.register_custom_action(ACTION_NAME, ClickAction(self))
        self._clicker: Clicker | None = None
        self._loaded = False
        self._last_frame = None   # 最近一次识别帧（只为动作桥换算框中心提供尺寸）

    # ---------- 装配 ----------

    def add_plugin(self, pipeline_dir: Path, image_dir: Path) -> None:
        """登记一个插件的图与模板目录（在 load() 之前调用）。

        pipeline_dir 可以是目录，也可以是单个 json/jsonc 文件（框架两种都支持）；
        传单个文件可避免把插件里其他用途的 pipeline（如对局回合链）一起装进跳转图。
        """
        if Path(pipeline_dir).exists():
            self._pipeline_dirs.append(Path(pipeline_dir))
        if Path(image_dir).exists():
            self.image_dirs.append(Path(image_dir))

    def load(self) -> bool:
        """加载公共图 + 各模块图，绑定 Tasker。重复调用无副作用。"""
        if self._loaded:
            return True
        for d in self._pipeline_dirs:
            job = self._resource.post_pipeline(str(d)).wait()
            if job.failed:
                logger.log(f"[跳转图] pipeline 加载失败: {d}", "ERROR")
                return False
        self._tasker.add_context_sink(PipelineLogger())
        self.ctx.bind_tasker(self._tasker, self._resource)
        self._loaded = True
        logger.log(f"[跳转图] 已加载 {len(self._pipeline_dirs)} 个图目录、"
                   f"{len(self.image_dirs)} 个模板目录")
        return True

    # ---------- 桥要用的宿主能力（截图/点击都收口到这里）----------

    def frame(self):
        """取一帧供识别。WGC 后端本身就是读中心采集器的缓存帧（零阻塞），
        所以这里不再自设 TTL 缓存——缓存会和框架的节点重试节奏打架：
        反复喂同一张旧帧，页面真切换了也看不出来，只能干等到 timeout。"""
        self._last_frame = self.ctx.capture.screenshot()
        return self._last_frame

    def frame_size(self) -> tuple[int, int]:
        """识别帧尺寸：动作桥用它把像素框换算成 Clicker 要的归一化坐标。"""
        if self._last_frame is None:
            return (0, 0)
        H, W = self._last_frame.shape[:2]
        return (W, H)

    def cursor_pos(self) -> tuple[float, float] | None:
        """游戏光标在截图帧中的归一化位置（MRA_Template mask_cursor 遮挡过滤用）。

        P4c 接线（宪法 §5 L1 防线的数据源）：真值来自手柄导航器对游戏渲染
        圆盘的最近识别位；real 鼠标模式 WGC 不采 OS 光标、手柄未绑定或从未
        识别到时返回 None（识别节点按无光标处理）。
        """
        clicker = self._clicker
        if clicker is None:
            return None
        pos = clicker.gamepad_cursor_pos()
        if not pos:
            return None
        W, H = self.frame_size()
        if W <= 0 or H <= 0:
            self.frame()  # v4 注入帧路径 _last_frame 无更新方：补读一次缓存帧拿尺寸
            W, H = self.frame_size()
        if W <= 0 or H <= 0:
            return None
        return (pos[0] / W, pos[1] / H)

    def click(self, cx: float, cy: float, box_norm, timeout_s: float) -> bool:
        """执行一次点击并等到位。模式与意图每次同步（设置页可热切）。"""
        clicker = self._ensure_clicker()
        clicker.set_mode(self.ctx.click_mode)
        clicker.set_intent(self.ctx.intent_mode)
        if clicker.mode == "gamepad" and not clicker.gamepad_bound:
            # v4 手柄租约只在模块启动时按当时模式绑定；运行中热切到 gamepad
            # 不重绑——不拦会静默 submit False，这里给可诊断的告警。
            logger.log("[v4] 点击方式已热切为 gamepad 但手柄未绑定：请重启模块生效", "WARNING")
            return False
        if clicker.need_foreground and not is_foreground(self.ctx.hwnd):
            # 前台(鼠标)模式不抢前台护栏：游戏不在
            # 前台时 SendInput 会点到该屏幕位置最上的其他窗口（落点看似「完全
            # 不对」）。取消本次点击，节点走 on_error 回 dwell 按 rate_limit 重试。
            now = time.monotonic()
            if now - self._last_fg_warn >= self.FOREGROUND_WARN_S:
                self._last_fg_warn = now
                logger.log("[v4] 游戏窗口非前台，取消本次点击（安全策略：不抢前台；"
                           "可切后台(手柄)方式或把游戏带到前台）", "WARNING")
            return False
        if not clicker.submit_click(cx, cy, box=box_norm):
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.ctx.lifecycle.running:
                clicker.cancel()
                return False
            res = clicker.consume_result()
            if res is not None:
                return bool(res.get("ok"))
            time.sleep(self.CLICK_POLL_S)
        clicker.cancel()
        return False

    def _ensure_clicker(self) -> Clicker:
        if self._clicker is None:
            self._clicker = Clicker(self.ctx.hwnd, self.ctx.click_mode)
        return self._clicker

    # ---------- 跑图 ----------

    def run(self, entry: str, reached: str | None = None) -> bool:
        """跑一条以 entry 为起点的图，阻塞到图结束（终点节点无 next）或失败。

        reached：终点确认节点名。图"跑完"不等于"跑对"——中间节点超时后框架
        也可能正常收尾，所以要求那个确认页面的识别节点真的 completed 才算到位。

        手柄租约只在跑图期间持有：跑图结束立刻归还，模块外层的
        reset_device()（销毁手柄复位）才不会被租约不变量挡住。
        """
        if not self.load():
            return False
        # 手柄租约只在跑图期间持有：结束立刻归还，模块外层的 reset_device()
        # （销毁手柄复位）才不会被「有活跃租约禁止断开设备」这条不变量挡住。
        with ExitStack() as stack:
            if self.ctx.click_mode == "gamepad":
                from maaracing_assistant.core.capabilities import BUTTON_A
                gpad = stack.enter_context(self.ctx.gamepad.acquire())
                self._ensure_clicker().bind_gamepad(
                    self.ctx.capture, gpad, confirm_button=BUTTON_A)
            logger.log(f"[跳转图] 起跑「{entry}」")
            job = self._tasker.post_task(entry)
            while not job.status.done:
                if not self.ctx.lifecycle.running:
                    self._tasker.post_stop().wait()
                    logger.log("[跳转图] 收到停止信号，已中断跑图")
                    return False
                time.sleep(0.2)
            ok = bool(job.succeeded)
            if ok and reached:
                node = self._tasker.get_latest_node(reached)
                ok = bool(node and node.completed)
                if not ok:
                    logger.log(f"[跳转图] 终点确认节点「{reached}」未命中，判为未到位", "WARNING")
            logger.log(f"[跳转图] 「{entry}」{'完成' if ok else '失败'}")
            return ok

    def stop(self) -> None:
        """中断在跑的图（模块 stop 里调用）。"""
        try:
            self._tasker.post_stop()
        except Exception:  # noqa: BLE001 —— 未起跑时停止是正常路径
            pass

    def shutdown(self) -> None:
        """释放点击器（停后台导航线程），模块 cleanup 里调用。"""
        if self._clicker is not None:
            self._clicker.shutdown()
            self._clicker = None


# ==================================================================
#  v4 执行通路（P2a-Q4）：v4 真源图跑在 MaaFW Tasker 上，帧注入控制器
# ==================================================================

STALE_FRAME_MS = 500.0  # plan §4.2 帧新鲜度守卫阈值


class FrameStaleError(RuntimeError):
    """帧缺失或超过新鲜度阈值——框架按节点 on_error 处理，绝不拿旧帧识别。"""


class WgcapController(CustomController):
    """v4 帧注入控制器（宪法 6：帧只从中心缓存来，引擎永不自截帧）。

    screencap() 读 CaptureAdapter 的 WGC 缓存帧（RGB→BGR 适配框架契约），
    帧龄超过 stale_ms 抛 FrameStaleError。点击/按键等动作一律成功返回——
    真实动作走 MRA_Click → Clicker（手柄导航协议），不经框架输入通道。
    """

    def __init__(self, capture, *, stale_ms: float = STALE_FRAME_MS):
        super().__init__()
        self._capture = capture
        self._stale_ms = float(stale_ms)
        self.last_frame_id = 0

    def screencap(self):
        import numpy as np

        frame, fid, _ts, age = self._capture.frame_with_age()
        if frame is None or age > self._stale_ms:
            raise FrameStaleError(f"帧缺失或过期：age={age:.0f}ms > {self._stale_ms:.0f}ms")
        self.last_frame_id = int(fid)
        return np.ascontiguousarray(frame[:, :, ::-1])  # RGB → BGR（框架契约）

    # ---- 框架要求的输入接口：真实动作走 MRA_Click，这里全部成功占位 ----

    def connect(self):
        return True

    def connected(self):
        return True

    def request_uuid(self):
        return "mra-navkit-v4"

    def start_app(self, intent):
        return True

    def stop_app(self, intent):
        return True

    def click(self, x, y):
        return True

    def swipe(self, x1, y1, x2, y2, duration):
        return True

    def touch_down(self, contact, x, y, pressure):
        return True

    def touch_move(self, contact, x, y, pressure):
        return True

    def touch_up(self, contact):
        return True

    def click_key(self, keycode):
        return True

    def input_text(self, text):
        return True

    def key_down(self, keycode):
        return True

    def key_up(self, keycode):
        return True


class NavKitV4:
    """v4 执行通路：真源图（pipeline/ 节点 JSON）常驻跑在 MaaFW Tasker 上。

    与 NavGraph（按需跑一段的跳转图）共享桥宿主接口（frame/click/ensure_clicker），
    差异：①Tasker 绑 WgcapController（帧注入）而非 app 控制器；②图目录 =
    pipeline/ 真源；③dwell 图常驻不退出（post_task 后框架自驱，桥内决策）。
    业务桥（MRA_Policy 等）由 plugin 经 bridges 参数注入（宪法 3）。
    """

    def __init__(self, ctx, *, pipeline_dirs, image_dirs=(), bridges=(),
                 stale_ms: float = STALE_FRAME_MS):
        self.ctx = ctx
        self._pipeline_dirs = [Path(d) for d in pipeline_dirs]
        # 桥宿主：frame/click/ensure_clicker 复用 NavGraph；模板目录直接
        # 挂到它的 image_dirs（TemplateRecognizer find_any 的搜索根）。
        self._graph = NavGraph(ctx)
        self._graph.image_dirs.extend(Path(d) for d in image_dirs)
        self._resource = Resource()
        self._tasker = Tasker()
        self._controller = WgcapController(ctx.capture, stale_ms=stale_ms)
        self._resource.register_custom_recognition(
            RECOGNIZER_NAME,
            TemplateRecognizer(self._graph, cursor_pos_provider=self._graph.cursor_pos))
        self._resource.register_custom_action(ACTION_NAME, ClickAction(self._graph))
        for name, inst in bridges:
            if isinstance(inst, CustomAction):
                self._resource.register_custom_action(name, inst)
            else:
                self._resource.register_custom_recognition(name, inst)
        self._loaded = False
        self._job = None

    def load(self) -> bool:
        """加载 v4 真源目录并把 Tasker 绑到帧注入控制器。重复调用无副作用。

        真源分居 core/plugin 两处且互指（global 骨架 ↔ 模块图），框架按
        post 校验节点引用存在性——分次 post 时先加载者必然失败。多目录
        时合并到临时目录一次 post；框架在 post 时同步解析为内部节点，
        加载完成即清理临时目录，源目录（MPE 编辑入口）不受影响。
        """
        if self._loaded:
            return True
        for d in self._pipeline_dirs:
            if not d.is_dir():
                logger.log(f"[v4] 真源目录不存在: {d}", "ERROR")
                return False
        merged_tmp: Path | None = None
        try:
            if len(self._pipeline_dirs) == 1:
                target = self._pipeline_dirs[0]
            else:
                merged_tmp = Path(tempfile.mkdtemp(prefix="navkit-pipeline-"))
                seen: dict[str, Path] = {}
                for d in self._pipeline_dirs:
                    for f in d.rglob("*.json*"):
                        rel = f.relative_to(d).as_posix()
                        if rel in seen:
                            logger.log(
                                f"[v4] 真源文件冲突: {rel}（{seen[rel]} 与 {d}）", "ERROR")
                            return False
                        seen[rel] = d
                        dest = merged_tmp / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest)
                target = merged_tmp
            job = self._resource.post_pipeline(str(target)).wait()
        finally:
            if merged_tmp is not None:
                shutil.rmtree(merged_tmp, ignore_errors=True)
        if job.failed:
            logger.log(f"[v4] pipeline 加载失败: {[str(d) for d in self._pipeline_dirs]}", "ERROR")
            return False
        self._tasker.add_context_sink(PipelineLogger())
        self._tasker.bind(self._resource, self._controller)
        self._loaded = True
        logger.log(f"[v4] 已加载 {len(self._pipeline_dirs)} 个真源目录（帧注入控制器）")
        return True

    def start(self, entry: str) -> bool:
        """起跑常驻图（dwell 循环由框架自驱）。已起跑时幂等返回 True。"""
        if not self.load():
            return False
        if self._job is not None and not self._job.status.done:
            return True
        logger.log(f"[v4] 起跑「{entry}」")
        self._job = self._tasker.post_task(entry)
        return True

    def poll(self) -> bool:
        """常驻图健康轮询：True = 在跑；False = 已结束/失败（调用方决定重启或告警）。"""
        return self._job is not None and not self._job.status.done

    def stop(self) -> None:
        try:
            self._tasker.post_stop()
        except Exception:  # noqa: BLE001 —— 未起跑时停止是正常路径
            pass
        if self._job is not None:
            # 等 Tasker 线程真正收摊再返回：对象随模块结束被 GC 时若线程仍在
            # 跑图，C 层回调悬空 → 0xC0000005（binding 句柄生命周期已知坑）。
            try:
                self._job.wait()
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self) -> None:
        self.stop()
        self._graph.shutdown()
