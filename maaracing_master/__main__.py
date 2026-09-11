#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MaaRacingMaster 包入口：python -m maaracing_master
启动 JSONL sidecar（被 MaaRacingMaster.Shell.exe 托管；独立运行时等待 stdin RPC）。
"""

from maaracing_master.core.sidecar import main

main()
