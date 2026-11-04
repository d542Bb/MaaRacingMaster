# -*- coding: utf-8 -*-
"""极速狂飙插件包。

插件自包含契约：代码、模板图、图节点真源、几何标定、行为参数、模型权重
（resources/models/，含各权重许可声明 README；发布包默认不带权重，见 model_assets）
全部位于本目录内，registry 按 manifest.py 自动发现；整个目录拷走即卸载、放入即安装。本域新增的一切
（未来如 GUI 个性化卡片）同样住本目录，全局文档只放指向本目录的指针。

UI 流程的识别与点击规格由图节点承载（resources/pipeline/speedrush.json）；
驾驶决策的行为参数自阶段 B step 4 起住 **policy/decision.json**——本域自此有
policy 真源，与鉴宝出价表同类不同内容。

**三源分立**（设计稿 v2 §六，禁止互相抄数）：**游戏事实** = RULES.md（外部事实，
人工复核制）；**几何标定** = calibration/gate0.json（唯一入口
``world_model.load_calib``）；**行为参数** = policy/decision.json（唯一入口
``config.load_decision``，fail-loud）。``resources/policy/`` 另有纯数据住户
``hud_regions.json``（驾驶页区域集，由 ``hud.py`` 与离线探针共用）。
"""

from pathlib import Path

# 插件根目录（plugins/speedrush/）
PLUGIN_DIR = Path(__file__).resolve().parent
# 资源根与分类目录（模板图 image/、图节点 pipeline/、policy 真源 policy/、
# 几何标定 calibration/、模型权重 models/）
RES_DIR = PLUGIN_DIR / "resources"
IMAGE_DIR = RES_DIR / "image"
PIPELINE_DIR = RES_DIR / "pipeline"
POLICY_DIR = RES_DIR / "policy"
CALIB_DIR = RES_DIR / "calibration"
MODELS_DIR = RES_DIR / "models"
# 驾驶页 HUD 区域真源（照 core/roi_config 口径：归一化、x2/y2 排他）
HUD_REGIONS_FILE = POLICY_DIR / "hud_regions.json"
# 行为决策参数真源（FSM/评分/迟滞/校验阈值；config.load_decision 唯一读法）
DECISION_FILE = POLICY_DIR / "decision.json"
# 几何标定真源（Gate-0 收杆产物；world_model.load_calib 唯一读法）
GATE0_FILE = CALIB_DIR / "gate0.json"
# 模型权重（发布包默认不带，由可选模型包提供；仓库内保留真源与校验值。
# perception/ AGPL-3.0 衍生、depth/ Apache-2.0、lane/ BDD100K 非商业——来源与许可声明见
# models/README.md 与 THIRD_PARTY_LICENSES「模型权重」节。运行时由 model_assets
# 解析：用户数据目录优先、安装包兜底，带尺寸 + SHA256 校验）
PERCEPTION_MODEL_REL = "resources/models/perception/model.onnx"
PERCEPTION_MODEL_FILE = MODELS_DIR / "perception" / "model.onnx"
DEPTH_MODEL_REL = "resources/models/depth/moge2_vits_static_336x598_t1032_q4f16.onnx"
DEPTH_MODEL_FILE = MODELS_DIR / "depth" / "moge2_vits_static_336x598_t1032_q4f16.onnx"
LANE_MODEL_REL = "resources/models/lane/plus_small_384x640.onnx"
LANE_MODEL_FILE = MODELS_DIR / "lane" / "plus_small_384x640.onnx"