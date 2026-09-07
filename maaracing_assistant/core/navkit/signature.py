# -*- coding: utf-8 -*-
"""锚点规范化签名（D2）：user_data 覆盖层的失效判据。

覆盖层锚定「规范化字段子集 hash」而非文件字节 hash：注释、键顺序、格式化
不影响匹配行为，不该废掉用户的救急补丁；反之，任何影响模板匹配结果的字段
变更必须使覆盖层失效。`threshold` 与单模板 `template_thresholds` 同进同出，
否则"整体收紧使覆盖层失效、单模板放宽却续命"的不对称会漏掉横幅类调参
（如鉴宝中标横幅因彩条特效放宽到 0.60）。`order`/`label`/`comment` 不影响
匹配结果，不进签名。
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .assets import Assets

__all__ = ["anchor_signature"]


def anchor_signature(assets: "Assets", anchor_id: str) -> str:
    """单个锚点的规范化签名（sha256 前 8 位）。

    取**全部影响匹配结果的字段**：`{id, kind, rect, templates, threshold,
    scales, arbitration（含 template_thresholds）, guarded_by, page}`；
    `threshold`/`scales` 取继承后有效值（锚点未声明时落 `match` 唯一口径，
    两种取法会产生不同 hash，此处钉死"有效值"）。rect 量化到 4 位小数；
    canonical JSON（sort_keys、无分隔符、ensure_ascii=False）。
    """
    anchor = assets.anchors[anchor_id]
    arb = anchor.arbitration
    payload = {
        "id": anchor_id,
        "kind": anchor.kind,
        "rect": [round(float(v), 4) for v in anchor.rect.as_list()],
        "templates": list(anchor.templates),
        "threshold": (
            anchor.threshold if anchor.threshold is not None
            else assets.match.threshold
        ),
        "scales": list(
            anchor.scales if anchor.scales is not None else assets.match.scales
        ),
        "arbitration": {} if arb is None else {
            "margin": arb.margin,
            "round_from_template": arb.round_from_template,
            "template_thresholds": dict(arb.template_thresholds),
        },
        "guarded_by": anchor.guarded_by,
        "page": anchor.page,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
