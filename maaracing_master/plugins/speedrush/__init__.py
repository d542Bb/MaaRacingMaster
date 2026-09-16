# -*- coding: utf-8 -*-
"""极速狂飙插件包。

插件自包含契约：代码、模板图、图节点真源全部位于本目录 resources/ 内，
registry 按 manifest.py 自动发现；整个目录拷走即卸载、放入即安装。

本域无 policy 表：策略表是「决策密集型」玩法（鉴宝每回合要算出价）的载体，
而本玩法的 UI 流程是线性的，识别与点击规格全部由图节点承载（见
resources/pipeline/speedrush.json）。
"""

from pathlib import Path

# 插件根目录（plugins/speedrush/）
PLUGIN_DIR = Path(__file__).resolve().parent
# 资源根与分类目录（模板图 image/、图节点 pipeline/）
RES_DIR = PLUGIN_DIR / "resources"
IMAGE_DIR = RES_DIR / "image"
PIPELINE_DIR = RES_DIR / "pipeline"