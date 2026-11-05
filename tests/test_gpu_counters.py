# -*- coding: utf-8 -*-
"""GPU 引擎计数器的纯函数单测（解析与聚合）。

PDH 调用本身依赖本机显卡与系统计数器，单测只锁「实例名怎么拆」与
「两条口径怎么算」——这两处错了，读数会静默错到底且看不出来。
"""
from maaracing_master.core.gpu_counters import aggregate, parse_instance

INST = ("pid_10576_luid_0x00000000_0x00011E0F_phys_0_eng_3_engtype_Copy")


def test_parse_instance_splits_pid_luid_engine():
    assert parse_instance(INST) == {
        "pid": 10576, "luid": "0x00011E0F", "phys": 0, "engtype": "Copy"}


def test_parse_instance_rejects_unknown_shapes():
    for name in ("", "garbage", "pid_abc_luid_0x0_0xA_phys_0_eng_0_engtype_3D", None):
        assert parse_instance(name) is None


def test_aggregate_sums_per_process_and_takes_busiest_engine():
    items = [
        (parse_instance("pid_100_luid_0x0_0xA_phys_0_eng_0_engtype_3D"), 40.0),
        (parse_instance("pid_100_luid_0x0_0xA_phys_0_eng_1_engtype_Copy"), 5.0),
        (parse_instance("pid_200_luid_0x0_0xA_phys_0_eng_0_engtype_3D"), 20.0),
        (parse_instance("pid_300_luid_0x0_0xA_phys_0_eng_2_engtype_VideoDecode"), 70.0),
    ]
    util, prog = aggregate(items, 100)
    # 引擎并行，整卡取最忙的那个引擎（3D 60 / Copy 5 / VideoDecode 70），不能相加
    assert util == 70.0
    # 本进程按引擎求和（3D 40 + Copy 5）
    assert prog == 45.0


def test_aggregate_clamps_and_ignores_noise():
    items = [
        (parse_instance("pid_1_luid_0x0_0xA_phys_0_eng_0_engtype_3D"), 80.0),
        (parse_instance("pid_1_luid_0x0_0xA_phys_0_eng_1_engtype_Copy"), 80.0),
        (None, 50.0),
        (parse_instance("pid_2_luid_0x0_0xA_phys_0_eng_0_engtype_3D"), -1.0),
    ]
    util, prog = aggregate(items, 1)
    assert prog == 100.0        # 160 截到 100
    assert util == 80.0


def test_aggregate_empty_is_zero():
    assert aggregate([], 1) == (0.0, 0.0)
