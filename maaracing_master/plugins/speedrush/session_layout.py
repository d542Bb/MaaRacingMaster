# -*- coding: utf-8 -*-
"""speedrush 会话产物布局与元信息：人工录制与控制取证共用一套。

**为什么单独成模块**：两条落盘路径（人工语料录制 / 程序调试图）曾各写一套
目录与索引，同名 ``frames.jsonl`` 在两边含义不同——人工局 ``file`` 是裸文件名、
隐含落在 ``frames/``，控制局 ``file`` 是相对会话根的 ``raw/...``；HUD 读数也
一个人工局在会话根、一个在 ``hud/``。离线工具必须先看目录名才知道怎么解释。
这里把「一个会话长什么样」收成一份：路径构造函数 + 元信息构造器，两个写手
都从这里取，不允许各自拼字符串、各自造字段。

**目录契约**（一局两个驾驶阶段，每阶段一个会话目录）::

    <会话>/
      meta.json        会话元信息（本模块构造，两种局同构）
      frames.jsonl     逐帧索引：seq / frame_id / ts_ns / age_ms / file / hud
      raw/0001_raw.jpg 原始帧（标准图像语义；人工局为录制帧率，控制局 10fps）
      0001_hud.jpg     程序叠加图（双联：画面标注 + BEV；缺失=该帧未渲染）
      hud/hud.jsonl    驾驶页 HUD 读数（与同会话索引按 frame_id / ts_ns 对齐）
      hud/hud_meta.json
      pads.jsonl       物理手柄样本（仅人工局）
      depth_debug/     深度证据包（debug_dump 闸：events/full）
      badframes/       深度双侧弃权帧取证

**历史会话不受影响**：2026-10-30 前的会话仍按旧布局读（``frames/`` + 裸文件名，
HUD 在会话根），迁移口径见插件 CODE_WIKI。
"""

from __future__ import annotations

# 会话数据格式版本。字段语义变动时递增，供离线步骤识别。
# v2：meta 增记帧图字节序（disk_pixel_order）。
# v3：meta 增记 phase / round_no。
# v4：人工局与控制局统一目录与索引字段；meta 增 source / hud_written /
#     hud_jpeg_quality，人工局 HUD 读数移入 hud/。
SCHEMA_VERSION = 4

RAW_DIR = "raw"
HUD_DIR = "hud"

# 两种会话来源（meta.source）：人工驾驶语料 / 程序控制取证
SOURCE_MANUAL = "manual"
SOURCE_CONTROL = "control"


def raw_relpath(seq: int) -> str:
    """原始帧相对会话根的路径（索引 ``file`` 字段唯一口径）。"""
    return f"{RAW_DIR}/{seq:04d}_raw.jpg"


def hud_relpath(seq: int) -> str:
    """程序叠加图相对会话根的路径（索引 ``hud`` 字段唯一口径）。"""
    return f"{seq:04d}_hud.jpg"


def build_meta(
    *,
    source: str,
    started_at: str,
    ended_at: str,
    monotonic_start_ns: int,
    phase: int | None,
    round_no: int | None,
    frame_w: int,
    frame_h: int,
    jpeg_quality: int,
    hud_jpeg_quality: int | None,
    frames_written: int,
    frames_dropped: int,
    hud_written: int,
    write_errors: int,
    pad_slot: int | None,
    pad_poll_hz: float | None,
    pad_samples: int | None,
    stop_reason: str,
) -> dict:
    """一个会话的 meta。两种来源同构：缺的项显式写 None，不用键缺席表达。

    字段缺失是 schema 1 那批数据认不出字节序的成因（只能猜），故「没采集到」
    必须是可辨认的显式状态，而不是「这个键恰好没有」。
    """
    return {
        "schema": SCHEMA_VERSION,
        "source": source,
        # 帧图文件的字节序：标准图像语义（首字节为红），仓库内读它走
        # image_io.read_rgb。schema 1 的会话没有这个键，其帧图是 R/B 互换的。
        "disk_pixel_order": "rgb",
        "started_at": started_at,
        "ended_at": ended_at,
        "monotonic_start_ns": monotonic_start_ns,
        # 原始帧 / 程序叠加图的 JPEG 质量分列：两者用途不同，取值本就不同
        "jpeg_quality": jpeg_quality,
        "hud_jpeg_quality": hud_jpeg_quality,
        "frame_w": frame_w,
        "frame_h": frame_h,
        "phase": phase,
        "round_no": round_no,
        "frames_written": frames_written,
        "frames_dropped": frames_dropped,
        "hud_written": hud_written,
        "write_errors": write_errors,
        # 手柄项是人工局专属；控制局显式 None（程序输入，物理手柄不参与）
        "pad_slot": pad_slot,
        "pad_poll_hz": pad_poll_hz,
        "pad_samples": pad_samples,
        "stop_reason": stop_reason,
    }
