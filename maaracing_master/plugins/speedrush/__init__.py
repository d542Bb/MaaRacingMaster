# -*- coding: utf-8 -*-
"""极速狂飙插件包。

插件自包含契约：代码、模板图、图节点真源全部位于本目录 resources/ 内，
registry 按 manifest.py 自动发现；整个目录拷走即卸载、放入即安装。

本域无 policy 表：策略表是「决策密集型」玩法（鉴宝每回合要算出价）的载体，
而本玩法的 UI 流程是线性的，识别与点击规格全部由图节点承载（见
resources/pipeline/speedrush.json）。

``resources/policy/`` 只放**纯数据**真源，不放 policy 表：当前唯一住户是驾驶页 HUD 的
区域集（``hud_regions.json``，由 ``hud.py`` 的实时读数与离线探针共用）——它没有
阶段/转移/锚点语义，不属于 policy 数据面。
"""

from pathlib import Path

# 插件根目录（plugins/speedrush/）
PLUGIN_DIR = Path(__file__).resolve().parent
# 资源根与分类目录（模板图 image/、图节点 pipeline/、纯数据 policy/）
RES_DIR = PLUGIN_DIR / "resources"
IMAGE_DIR = RES_DIR / "image"
PIPELINE_DIR = RES_DIR / "pipeline"
POLICY_DIR = RES_DIR / "policy"
# 驾驶页 HUD 区域真源（照 core/roi_config 口径：归一化、x2/y2 排他）
HUD_REGIONS_FILE = POLICY_DIR / "hud_regions.json"