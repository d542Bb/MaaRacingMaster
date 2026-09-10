# -*- coding: utf-8 -*-
"""一次性脚本：生成 policy.json `engine_contract` 段并写入（契约化偿债地基）。

值来源：重构前 core/navkit/policy.py 常量经 import 现物 dump（facts/state_fields/
algo_fields/decision_sources/effects/wait_keys/tuning_keys/fallback）、_effects_for
两分支与 validate 的 effect 条件覆盖（auto_effects/passthrough_effects/
effect_required_conditions）、DecisionFacts.freeze 硬编码推导（derived/
derived_sources）。与 commit 174c87a 的 policy.py 常量逐字段一致。

运行：.venv python tools/experiments/ecosystem-audit/contract_dump.py
幂等：每次以本文件常量整体重建 engine_contract 段（round-trip 制式写回）。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
POLICY = REPO / "maaracing_assistant/plugins/treasure/resources/policy/treasure.policy.json"

CONTRACT = {
    "facts": [
        "appraiser_decision", "bidding_decision", "clicked_once", "cooldown",
        "daily_high_score", "egg_read_done", "egg_reading", "frame_counter",
        "popup_kind", "retry_count", "retry_elapsed", "reward_elapsed",
        "session_decision", "settle_income", "skip_cycle", "stage",
    ],
    "state_fields": [
        "clicked_once", "cooldown", "daily_high_score", "egg_read_done",
        "egg_reading", "frame_counter", "retry_count", "reward_enter_frame",
        "settle_income", "settle_skip_since",
    ],
    "algo_fields": [
        "appraiser_decision", "bidding_decision", "popup_kind",
        "session_decision", "stage",
    ],
    "decision_sources": ["appraiser_decision", "bidding_decision", "session_decision"],
    "effects": ["popup_cooldown_decr", "settle_skip_retry"],
    "wait_keys": [
        "appraiser_waiting", "bid_waiting", "dividend_waiting", "fatal",
        "popup_click_cooldown", "popup_high_continue", "popup_reward_continue",
        "popup_waiting", "session_waiting", "stage_waiting",
    ],
    "fallback": {"hint": "等待阶段切换...（等待界面稳定）", "key": "stage_waiting"},
    "auto_effects": {"popup_click_cooldown": "popup_cooldown_decr"},
    "passthrough_effects": ["settle_skip_retry"],
    "effect_required_conditions": {
        "settle_skip_retry": ["clicked_once", "retry_count", "retry_elapsed",
                              "settle_income", "stage"],
    },
    "derived": [
        {"field": "retry_elapsed", "from": "settle_skip_since", "op": "elapsed"},
        {"field": "reward_elapsed", "from": "reward_enter_frame", "op": "elapsed"},
        {"field": "skip_cycle", "k": 3, "op": "frame_mod"},
    ],
    "derived_sources": ["reward_enter_frame", "settle_skip_since"],
    "tuning_keys": {
        "execution": ["click_cooldown_s"],
        "perception": [
            "appraiser_match_threshold", "appraiser_search_roi",
            "check_match_threshold", "session_match_threshold",
            "smart_bid_match_threshold",
        ],
        "policy": [
            "click_retry_frames", "click_retry_max", "daily_high_timeout_frames",
            "egg_ocr_timeout_frames", "popup_click_cooldown_frames",
            "popup_continue_retry_frames", "session_start_click_cooldown_frames",
            "settle_skip_retry_frames", "settle_skip_retry_max",
        ],
    },
}

if __name__ == "__main__":
    doc = json.loads(POLICY.read_text(encoding="utf-8"))
    doc["engine_contract"] = CONTRACT
    POLICY.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"engine_contract written to {POLICY.name}: {len(CONTRACT)} keys")
