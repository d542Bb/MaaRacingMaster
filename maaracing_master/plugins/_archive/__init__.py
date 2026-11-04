# -*- coding: utf-8 -*-
"""插件归档区：已退役、不再随包分发的活动模块。

约定：
- 目录位置即归档判据——`core/registry` 只扫描 `plugins/` 的直接子目录，
  本目录下的模块不会注册、不会出现在模块列表；
- 打包排除——`scripts/release/assemble.ps1` 的 robocopy 显式排除本目录，
  归档代码与资源不进发布包；
- 仍保持可导入——归档模块的回归锁带 `retired` 标记，`pytest -m retired`
  需要能导入它们；复活时按各模块 RETIRED.md 的步骤摘标记并移回 `plugins/` 顶层。
"""
