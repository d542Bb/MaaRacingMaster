# -*- coding: utf-8 -*-
"""巅峰鉴宝插件清单：供 core/registry 自动扫描发现与注册。

契约：ID 唯一标识；MODULE_CLASS 定位模块类（plugins/<id>/<path>.py 的 <attr> 类）。
NAME / STAGE_ORDER / REQUIRES / REQUIRES_GAMEPAD_EXCLUSIVE 从模块类读取（单一来源）。
VALID_FROM / VALID_UNTIL：可选的活动有效期（模块自声明，不另设真源），两端均含，
缺省 = 永久有效；过期后 GUI 置灰、不再自动选中，仍可经下拉栏强制选择。
"""

ID = "treasure"
MODULE_CLASS = "module.TreasureModule"
# 巅峰鉴宝活动窗口：2026-08-06 05:00 — 2026-09-23 04:59（游戏服时间 UTC+8）
VALID_FROM = "2026-08-06T05:00:00+08:00"
VALID_UNTIL = "2026-09-23T04:59:59+08:00"
