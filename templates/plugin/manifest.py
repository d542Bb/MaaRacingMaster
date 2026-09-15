# -*- coding: utf-8 -*-
"""<插件名> 插件清单：供 core/registry 自动扫描发现与注册。

契约：ID 唯一标识；MODULE_CLASS 定位模块类（plugins/<id>/<path>.py 的 <attr> 类）。
NAME / STAGE_ORDER / REQUIRES / REQUIRES_GAMEPAD_EXCLUSIVE / REQUIRED_ASSETS
从模块类读取（单一来源），不在 manifest 重复声明。
VALID_FROM / VALID_UNTIL：可选的活动有效期（两端均含，ISO 8601 带时区偏移；
缺省 = 永久有效）。
"""

ID = "sample"
MODULE_CLASS = "module.SampleModule"
# 可选：活动有效期，形如 "2026-08-06T05:00:00+08:00"；不声明即永久有效。
# VALID_FROM = "2026-08-06T05:00:00+08:00"
# VALID_UNTIL = "2026-09-23T04:59:59+08:00"
