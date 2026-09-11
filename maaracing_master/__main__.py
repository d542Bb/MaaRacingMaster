#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MaaRacingAssistant 包入口：python -m maaracing_master
启动 JSONL sidecar（被 mra_shell.exe 托管；独立运行时等待 stdin RPC）。
"""

from maaracing_master.core.sidecar import main

main()
