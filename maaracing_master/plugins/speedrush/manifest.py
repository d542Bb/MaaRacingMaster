# -*- coding: utf-8 -*-
"""极速狂飙插件清单：供 core/registry 自动扫描发现与注册。

契约：ID 唯一标识；MODULE_CLASS 定位模块类（plugins/<id>/<path>.py 的 <attr> 类）。
NAME / STAGE_ORDER / REQUIRES / REQUIRES_GAMEPAD_EXCLUSIVE 从模块类读取（单一来源）。

未声明 VALID_FROM / VALID_UNTIL：活动的开放窗口尚未同步（大厅按钮带「极速狂飙
开放中」角标，疑为限时活动），缺省 = 永久有效。待取得权威口径后再补。
"""

ID = "speedrush"
MODULE_CLASS = "module.SpeedRushModule"