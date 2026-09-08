# -*- coding: utf-8 -*-
"""调试台匹配实现 ↔ 运行时匹配实现的 parity 守卫。

背景：逐帧匹配算法存在两份同构实现——
  - 调试台：`tools/navkit/core/reader.py match_local`
  - 运行时：`maaracing_assistant/plugins/treasure/detector.py TreasureStageDetector._match_local`
两者当前逐行等价，**但没有任何机器守卫**：任一侧改动（缩放插值、跳过条件、最优档
选取）都不会让另一侧或测试失败，只会让「调试台校准出的阈值」在运行时悄悄失灵。

本测试用合成图 + 合成模板把两实现跑在同一组输入上，断言：
  - 最优分一致（浮点容差）；
  - 最优缩放档一致；
  - 无可用档（模板放不进 ROI）时两实现都判为不可命中。

失败语义差异是**显式豁免**的：调试台返回 -1.0 表达「无法匹配」（前端要显示
SIZE 不足），运行时返回 0.0 表达「未命中」（参与阈值比较）。这是两套返回值契约，
不是算法差异，故只在可命中分支做数值比对。

另覆盖 G1.6：调试台档位来源必须是资产文档 `match.scales`，而非模块常量。
"""
from __future__ import annotations

import json

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from tools.navkit.core.reader import MATCH_SCALES, match_local  # noqa: E402

from maaracing_assistant.plugins.treasure.detector import (  # noqa: E402
    TreasureStageDetector,
)


# ----------------------------------------------------------------------
# 合成图/模板：确定性、无外部资源依赖
# ----------------------------------------------------------------------

def _synthetic_frame(size: int = 160) -> "np.ndarray":
    """低频背景 + 若干亮块，保证 TM_CCOEFF_NORMED 有非退化的响应面。"""
    rng = np.random.default_rng(20260908)
    base = np.full((size, size), 40, dtype=np.uint8)
    for y in range(0, size, 16):
        for x in range(0, size, 16):
            base[y:y + 8, x:x + 8] = int(60 + 120 * rng.random())
    return base


def _synthetic_template(h: int = 24, w: int = 24) -> "np.ndarray":
    """非对称图案（角块 + 中心十字），避免旋转/平移对称导致多峰同分。"""
    t = np.zeros((h, w), dtype=np.uint8)
    t[2:8, 2:8] = 255
    t[h - 10:h - 2, w - 12:w - 4] = 200
    cy, cx = h // 2, w // 2
    t[cy - 1:cy + 2, :] = 160
    t[:, cx - 1:cx + 2] = 160
    return t


def _runtime_match(gray_big, gray_tpl, rect, scales) -> float:
    """按运行时 `_match_local` 的公共契约调用（经实例方法，不复制算法）。"""
    from pathlib import Path

    proj = Path(__file__).resolve().parents[1]
    det = TreasureStageDetector(proj, ocr=None)
    det.match_scales = tuple(scales)  # 只注入档位，算法本身不复制
    W, H = gray_big.shape[1], gray_big.shape[0]
    return det._match_local(gray_big, gray_tpl, rect[0], rect[1], rect[2], rect[3], W, H)


# ----------------------------------------------------------------------
# 参数矩阵：覆盖「正常 ROI / 紧 ROI / 过大模板」三类
# ----------------------------------------------------------------------

RECTS = {
    # 归一化 rect（x1, y1, x2, y2）
    "roomy": (0.05, 0.05, 0.95, 0.95),
    "tight": (0.40, 0.40, 0.62, 0.62),
    "offset": (0.55, 0.20, 0.90, 0.55),
}
TEMPLATE_SIZES = [(24, 24), (40, 16), (16, 40)]


class TestMatchParity:
    """两实现在同一输入上必须给出同一最优分与同一最优档。"""

    @pytest.fixture
    def frame(self):
        return _synthetic_frame()

    @pytest.mark.parametrize("rect_name", list(RECTS))
    @pytest.mark.parametrize("tpl_shape", TEMPLATE_SIZES)
    def test_best_score_and_scale_match(self, frame, rect_name, tpl_shape):
        gray = frame
        tpl = _synthetic_template(*tpl_shape)
        rect = list(RECTS[rect_name])
        H, W = gray.shape[:2]

        debug_res = match_local(gray, tpl, rect, scales=MATCH_SCALES)
        runtime_score = _runtime_match(gray, tpl, rect, MATCH_SCALES)

        if not debug_res.get("size_ok"):
            # 无档可用：调试台 -1，运行时 0.0（语义不同，只断言都判为不可命中）
            assert runtime_score == 0.0, "运行时在无可用档时也必须判为未命中"
            assert debug_res.get("score", -1.0) < 0
            return

        assert debug_res["score"] == pytest.approx(runtime_score, abs=1e-6), (
            f"最优分漂移（rect={rect_name}, tpl={tpl_shape}）："
            f"调试台 {debug_res['score']} vs 运行时 {runtime_score}"
        )
        # 最优档一致性：分数相同还不够——插值/跳过条件改动会让「同分不同档」，
        # 进而在其它图上分歧。这里用运行时重跑该档核对。
        scale = debug_res["best_scale"]
        single = _runtime_match(gray, tpl, rect, [scale])
        assert single == pytest.approx(debug_res["score"], abs=1e-6), (
            f"最优档不一致（rect={rect_name}, tpl={tpl_shape}, scale={scale}）"
        )

    def test_debug_returns_pixel_geometry(self, frame):
        """调试台在可命中时须给出命中框（前端叠加依赖），与运行时无关但不可退化。"""
        res = match_local(frame, _synthetic_template(24, 24),
                          list(RECTS["roomy"]), scales=MATCH_SCALES)
        assert res["size_ok"] is True
        assert len(res["hit_box"]) == 4 and len(res["hit_norm"]) == 4
        x1, y1, x2, y2 = res["hit_box"]
        assert x2 - x1 == res["tpl_size"][0] and y2 - y1 == res["tpl_size"][1]


# ----------------------------------------------------------------------
# G1.6：调试台档位真源 = 资产文档 match.scales
# ----------------------------------------------------------------------

class TestMatchScalesSource:
    def test_debug_scales_come_from_document(self, tmp_path, monkeypatch):
        """文档 match.scales 改成两档后，调试台端点必须只用这两档。

        这是「调试台分数 = 运行时分数」的唯一保障：运行时取 `plan.scales`
        （源自文档），调试台若回落硬编码常量，两者口径即分裂。
        """
        import tools.navkit.server as srv

        doc = {
            "_schema_ver": 3, "_module": "treasure",
            "reference_size": [1280, 720],
            "match": {"scales": [0.8, 1.2], "threshold": 0.75, "margin_default": 0.0},
            "anchors": {},
        }
        assets = tmp_path / "treasure_assets.json"
        assets.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        state = srv.build_state("treasure")
        monkeypatch.setattr(srv, "assets_path_for", lambda module: assets)
        srv.Handler.state = state
        h = srv.Handler.__new__(srv.Handler)
        h.state = state

        assert h._match_scales() == (0.8, 1.2)

    def test_falls_back_to_constant_when_no_match_section(self, tmp_path, monkeypatch):
        """文档无 match 段（空资产/过渡期）→ 回落常量，不抛异常。"""
        import tools.navkit.server as srv

        assets = tmp_path / "treasure_assets.json"
        assets.write_text(json.dumps({"_schema_ver": 3, "anchors": {}}), encoding="utf-8")

        state = srv.build_state("treasure")
        monkeypatch.setattr(srv, "assets_path_for", lambda module: assets)
        srv.Handler.state = state
        h = srv.Handler.__new__(srv.Handler)
        h.state = state

        assert h._match_scales() == MATCH_SCALES
