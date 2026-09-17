#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pipeline 日志监听模块：监听 MAA pipeline 事件并输出日志

级别口径：`[Pipeline]` 记的是框架内部**节点生命周期**（识别成功 → 动作开始 →
动作成功），属程序执行步骤，一律 DEBUG——真机上每个节点跳转会连打三行，
按 INFO 记会占掉上报档一半以上篇幅（实测 62 行里 33 行），把真正的业务里程碑
淹没。业务里程碑由业务层自己记（进入阶段 / 点击意图 / 循环上限 / 收尾 / 会话总结）。

唯一例外是**动作失败**：那是「可继续运行的降级」，按 core/CODE_WIKI.md §1.5.1 的约定
必须打 WARNING，不得静默。
"""

from maa.context import ContextEventSink
from maa.event_sink import NotificationType

from maaracing_master.core.logger import logger


class PipelineLogger(ContextEventSink):
    """监听 MAA pipeline 每步的识别和动作事件并打印日志"""

    @staticmethod
    def _task_name(detail) -> str:
        return getattr(detail, "name", str(detail))

    @staticmethod
    def _task_desc(name: str) -> str:
        """给任务名加上中文描述"""
        descs = {
            "回合1比赛": "YOLO 赛车控制",
            "回合1结束": '找"继续"',
            "回合2准备": '找"放弃本轮"',
            "确认放弃": '找"继续放弃"',
        }
        return descs.get(name, name)

    def on_node_recognition(self, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        name = self._task_name(detail)
        desc = self._task_desc(name)
        hit = getattr(detail, "hit", None)
        if ts == "Succeeded" and hit is not None:
            logger.log(f"[Pipeline] {name}({desc}) → 识别{'✅命中' if hit else '❌未找到'}", "DEBUG")
        elif ts == "Succeeded":
            logger.log(f"[Pipeline] {ts}: {name}({desc})", "DEBUG")
        elif ts == "Failed":
            # Custom 识别器未命中（box=None）即发 Failed；Starting 不再逐次
            # 打印——rate_limit 内每帧一对 Starting/Failed 会淹没有效信息。
            logger.log(f"[Pipeline] {name}({desc}) → 识别未命中（驻留重试）", "DEBUG")

    def on_node_action(self, context, noti_type, detail):
        ts = NotificationType(noti_type).name
        name = self._task_name(detail)
        desc = self._task_desc(name)
        success = getattr(detail, "success", None)
        if ts == "Succeeded" and success is False:
            logger.log(f"[Pipeline] {name}({desc}) → 动作❌失败", "WARNING")
        elif ts == "Succeeded" and success is not None:
            logger.log(f"[Pipeline] {name}({desc}) → 动作✅成功", "DEBUG")
        else:
            logger.log(f"[Pipeline] {ts} 动作: {name}({desc})", "DEBUG")
