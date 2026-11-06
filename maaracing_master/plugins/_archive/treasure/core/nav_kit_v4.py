# -*- coding: utf-8 -*-
"""v4 常驻图宿主（原 core/nav_graph.py 的 NavKitV4；随鉴宝插件归档）。

与 NavGraph（按需跑一段的跳转图）共享桥宿主接口（frame/click/ensure_clicker），
差异：①Tasker 绑 WgcapController（帧注入）而非 app 控制器；②图目录 =
pipeline/ 真源；③dwell 图常驻不退出（post_task 后框架自驱，桥内决策）。
业务桥（MaaRM_Policy 等）由 plugin 经 bridges 参数注入（宪法 3）。

本模块是归档消费者：合并 post、等待收摊等助手复用 core 的现役实现；
core 演进导致失效时，归档测试不在默认门禁，不反向约束现役玩法。
"""
from __future__ import annotations

from pathlib import Path

from maa.custom_action import CustomAction
from maa.resource import Resource
from maa.tasker import Tasker

from maaracing_master.core.nav_graph import (
    ACTION_NAME,
    INPUT_ACTION_NAME,
    RECOGNIZER_NAME,
    STALE_FRAME_MS,
    ClickAction,
    InputAction,
    NavGraph,
    TemplateRecognizer,
    WgcapController,
    _post_pipeline_merged,
    _wait_job_settled,
)
from maaracing_master.core.pipeline_logger import PipelineLogger


class NavKitV4:
    """v4 执行通路：真源图（pipeline/ 节点 JSON）常驻跑在 MaaFW Tasker 上。

    与 NavGraph（按需跑一段的跳转图）共享桥宿主接口（frame/click/ensure_clicker），
    差异：①Tasker 绑 WgcapController（帧注入）而非 app 控制器；②图目录 =
    pipeline/ 真源；③dwell 图常驻不退出（post_task 后框架自驱，桥内决策）。
    业务桥（MaaRM_Policy 等）由 plugin 经 bridges 参数注入（宪法 3）。
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
        self._resource.register_custom_action(INPUT_ACTION_NAME,
                                              InputAction(self._graph))
        for name, inst in bridges:
            if isinstance(inst, CustomAction):
                self._resource.register_custom_action(name, inst)
            else:
                self._resource.register_custom_recognition(name, inst)
        self._loaded = False
        self._job = None

    def set_log_group(self, group_id: str | None) -> None:
        """转发到内部 NavGraph（动作桥日志挂靠同一业务组）。"""
        self._graph.set_log_group(group_id)

    def load(self) -> bool:
        """加载 v4 真源目录并把 Tasker 绑到帧注入控制器。重复调用无副作用。

        真源分居 core/plugin 两处且互指，经 _post_pipeline_merged 合并单次 post。
        """
        if self._loaded:
            return True
        # 同 NavGraph.load：句柄只在 shutdown 里置空，而 _loaded 置真后不复位。
        assert self._resource is not None and self._tasker is not None \
            and self._controller is not None
        if not _post_pipeline_merged(self._resource, self._pipeline_dirs, "v4",
                                     group_id=self._graph._log_gid):
            return False
        self._tasker.add_context_sink(PipelineLogger())
        self._tasker.bind(self._resource, self._controller)
        self._loaded = True
        self._graph._log(f"[v4] 已加载 {len(self._pipeline_dirs)} 个真源目录（帧注入控制器）")
        return True

    def start(self, entry: str) -> bool:
        """起跑常驻图（dwell 循环由框架自驱）。已起跑时幂等返回 True。"""
        if not self.load():
            return False
        assert self._tasker is not None  # load() 成功即执行器已就位且未被销毁
        if self._job is not None and not self._job.status.done:
            return True
        self._graph._log(f"[v4] 起跑「{entry}」")
        self._job = self._tasker.post_task(entry)
        return True

    def poll(self) -> bool:
        """常驻图健康轮询：True = 在跑；False = 已结束/失败（调用方决定重启或告警）。"""
        return self._job is not None and not self._job.status.done

    def stop(self) -> None:
        """中断常驻图并等到框架真正收摊。幂等。"""
        if self._tasker is None:  # 已 shutdown：停止路径幂等
            return
        try:
            stop_job = self._tasker.post_stop()
        except Exception:  # noqa: BLE001 —— 未起跑时停止是正常路径
            stop_job = None
        # 停止请求完成 ≠ 任务结束：两个句柄都确认，否则后续销毁 C 句柄会撞上
        # 仍在活动的框架 worker（0xC0000005，MaaFramework.dll）。等待带上限。
        if not _wait_job_settled(stop_job):
            self._graph._log("[v4] 停止请求超时未完成，仍继续收尾", "WARNING")
        if not _wait_job_settled(self._job):
            self._graph._log("[v4] 任务句柄超时未结束，仍继续收尾", "WARNING")

    def shutdown(self) -> None:
        """停止常驻图并显式销毁全部框架句柄（模块 cleanup 里调用）。

        与跳转图同一纪律：销毁时刻与顺序由这里定，不交给垃圾回收——C 句柄
        的 destroy 不等框架 worker 收摊，回收器决定的时刻与顺序会撞上活动
        回调（0xC0000005）。顺序固定：任务执行器 → 帧注入控制器 → 资源。
        """
        self.stop()
        self._graph.shutdown()
        self._job = None
        tasker, self._tasker = self._tasker, None
        del tasker
        controller, self._controller = self._controller, None
        del controller
        resource, self._resource = self._resource, None
        del resource
