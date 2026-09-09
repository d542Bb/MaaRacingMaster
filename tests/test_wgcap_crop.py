# -*- coding: utf-8 -*-
"""WgcCapture._crop_client 纯逻辑回归（真机八炸：兜底裁反方向定案）。

不触碰 Win32/原生采集：__init__ 只存参数，裁剪状态手工装配。
"""
from __future__ import annotations

import numpy as np

from maaracing_assistant.core.wgcap import WgcCapture


def _bare_cap(offset=None, size=None, dwm=None) -> WgcCapture:
    cap = WgcCapture(0)
    cap._client_offset = offset
    cap._client_size = size
    cap._dwm_size = dwm
    return cap


def test_fallback_anchor_is_bottom_not_top():
    """整窗帧（含顶部标题栏）非 16:9 时：裁顶部、保底部——游戏主体 UI 在底部。

    旧实现 img[:target_h] 恰好裁反：底部 38 行客户区丢失 → 右下导航按钮
    模板不完整 → 识别全 miss（真机八炸事故帧实锤）。
    """
    cap = _bare_cap()  # 无裁剪信息 → 直接走 16:9 兜底
    img = np.zeros((759, 1282, 4), np.uint8)
    img[720:, :, :] = 255  # 底部 39 行标记（真画面内容所在）
    out = cap._crop_client(img)
    assert out.shape == (721, 1282, 4)  # target_h = round(1282*9/16)
    assert bool((out[-1] == 255).all())  # 底行保留 = 底部锚定
    assert not bool((out[0] == 255).all())


def test_client_crop_applied_when_geometry_matches():
    """帧尺寸 ≈ DWM 边界 → 装饰链：按 offset/size 裁出客户区，兜底不再触发。"""
    cap = _bare_cap(offset=(1, 38), size=(1280, 720), dwm=(1282, 759))
    img = np.arange(759 * 1282 * 4, dtype=np.uint8).reshape(759, 1282, 4)
    out = cap._crop_client(img)
    assert out.shape == (720, 1280, 4)
    assert np.array_equal(out, img[38:758, 1:1281])


def test_out_of_range_offset_warns_once_and_keeps_frame():
    """裁剪越界（如 DPI 混系负偏移）：不裁、整窗下传，越界告警只一次。"""
    cap = _bare_cap(offset=(-228, 19), size=(1024, 576), dwm=(1282, 759))
    img = np.zeros((759, 1282, 4), np.uint8)
    out = cap._crop_client(img)
    assert out.shape == (721, 1282, 4)  # 未裁客户区，仅 16:9 兜底
    assert cap._crop_warned is True
    cap._crop_client(img)
    assert cap._crop_warned is True  # 幂等（不重复置位）
