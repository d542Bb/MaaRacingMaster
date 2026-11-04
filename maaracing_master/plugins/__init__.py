# -*- coding: utf-8 -*-
"""插件根包：一个活动 = 一个自包含子目录（含 manifest.py，供 core/registry 自动扫描）。

`_archive/` 是归档区：已退役模块按目录位置隔离——不在 registry 扫描范围、不随包分发，
但仍保持可导入（退役回归锁 `pytest -m retired` 需要）。约定见 `_archive/__init__.py`。
"""
