# -*- coding: utf-8 -*-
"""鉴宝出价策略审计 · 2026-09-05（游戏改版分界）之后的样本。

分界依据：`docs/plan/archive/bid_audit_20260915/出价审计报告_20260915.md` 以 09-05 为段；
库里 09-05 起胜率形态突变（08-25~08-30 每天拍中 27~33 场，09-05 起连日 0~2 场）；
2026-09-06 会话已把游戏改版（中标结算页 UI 变化）记为既成事实（工作区记忆 mem_a69a91f7）。

本次回答的问题：V4（2026-09-15 卡第二改出到买入线）落地后，出价策略**还剩哪些空间**。
沿用上一轮审计的方法（我方槽位 = 玩家{my_rank}；成交按 RULES §2 的 K_r 复算；
整链静态回放），但换成**修复后的落盘库**（2026-09-17 已修 profit/income 脏值）与更大的样本。

复现：`.venv\\Scripts\\python.exe tools/experiments/bid-strategy-20260917/audit.py`
只读库；对手出价按落盘值静态回放（不建模「我方出价不同 → 对手反应不同」）→ 反事实部分
是**上界估计**，只用于候选改动之间的相对排序。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from maaracing_master.plugins.treasure.strategy import (  # noqa: E402
    K_RATIOS, BidContext, BidStrategy, RoundSnapshot,
)

DB = Path(os.path.expandvars(r"%APPDATA%")) / "MaaRacingMaster/data/treasure/treasure.db"
CUTOFF = "2026-09-05"            # 游戏改版分界（含）
V4_FROM = "2026-09-15"           # V4 落地
VAL_COEF = 1.28                  # 现行估值系数（strategy.VAL_COEF）
PROFIT_FLOOR = 0.10


# ----------------------------------------------------------------------
# 取数与复算
# ----------------------------------------------------------------------

def load(cutoff: str = CUTOFF) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = []
    for r in conn.execute(
        "SELECT bucket, game_seq, ts, auction_result, settle_final_price, settle_total_price,"
        " settle_profit, settle_my_income, h_prices, player_bids, my_rank FROM games"
        " WHERE bucket >= ? ORDER BY bucket, game_seq",
        (cutoff,),
    ):
        d = dict(r)
        try:
            d["h"] = [x for x in json.loads(d["h_prices"] or "[]") if isinstance(x, int) and x > 0]
            d["pb"] = {k: list(v) for k, v in json.loads(d["player_bids"] or "{}").items()}
        except Exception:
            d["h"], d["pb"] = [], {}
        out.append(d)
    conn.close()
    return out


def our_bids(g: dict) -> list[int] | None:
    """我方逐回合出价：槽位 = 玩家{my_rank}（上一轮审计已用日志逐条交叉验证）。"""
    if not g.get("my_rank"):
        return None
    slot = g["pb"].get(f"玩家{g['my_rank']}")
    return slot if isinstance(slot, list) and slot else None


def opp_bids(g: dict) -> dict[str, list[int]]:
    return {k: v for k, v in g["pb"].items() if k != f"玩家{g.get('my_rank')}"}


def settle_round(bids: list[int], h: list[int]) -> tuple[int | None, int | None]:
    """按 RULES §2 复算成交回合与成交价（第一名/第二名 ≥ K_r 即成交，K=2.0/1.6/1.3/1.1/1.0）。"""
    for r in range(1, min(len(bids), 5) + 1):
        s = sorted((b for b in bids if b > 0), reverse=True)
        if len(s) < 1:
            continue
        if len(s) == 1:
            return r, s[0]
        kr = K_RATIOS[r - 1]
        if s[0] >= kr * s[1]:
            return r, s[0]
    return None, None


def game_view(g: dict) -> dict | None:
    """把一场整理成分析视图；缺关键读数（H / 我方槽位 / 对手）→ None。"""
    h, ob = g["h"], our_bids(g)
    if not h or ob is None:
        return None
    opp = opp_bids(g)
    if len(opp) != 3:
        return None
    return {"g": g, "h": h, "maxh": max(h), "our": ob, "opp": opp}


# ----------------------------------------------------------------------
# A 段：V3 / V4 战绩（修复后的 profit / income）
# ----------------------------------------------------------------------

def outcome(v: dict) -> dict:
    """一场的我方结果：拍中判据 = 我方槽位是否持有成交价（成交价即赢家出价）。

    不看 `auction_result`——该字段 2026-09-06 漏读 21 场（改版后结果横幅全盲），
    按标签统计会把 6 场胜绩记成 0 胜（上一轮审计 §8.3 第 4 条）。
    """
    g = v["g"]
    fin, tot = g["settle_final_price"], g["settle_total_price"]
    ours = bool(fin) and max(v["our"]) == fin
    inc = g["settle_my_income"] if isinstance(g["settle_my_income"], int) else None
    profit = (tot - fin) if (ours and tot) else 0
    dividend = 0 if ours else (inc or 0)
    return {"ours": ours, "profit": profit, "dividend": dividend,
            "income": (profit if ours else dividend)}


def seg_stats(views: list[dict]) -> None:
    def band(name, lo, hi):
        vs = [v for v in views if lo <= v["g"]["bucket"] <= hi]
        outs = [outcome(v) for v in vs]
        wins = [o for o in outs if o["ours"]]
        wp = sum(o["profit"] for o in wins)
        dv = sum(o["dividend"] for o in outs)
        inc = wp + dv
        labeled = sum(1 for v in vs if v["g"]["auction_result"] == "win")
        print("  %-18s 可复算=%-3d 拍中=%-3d(%s) 拍中利润=%-10s 分红=%-9s 合计收益=%-11s 场均=%-8s (标签记 %d 胜)"
              % (name, len(vs), len(wins), "%d%%" % (100 * len(wins) // max(len(vs), 1)),
                 f"{wp:,}", f"{dv:,}", f"{inc:,}",
                 f"{int(inc / max(len(vs), 1)):,}", labeled))
    print("\n== A 战绩分段（拍中判据 = 持有成交价；收益 = 修复后的 profit / income）==")
    band("V3  09-05~09-14", CUTOFF, "2026-09-14")
    band("V4  09-15~09-17", V4_FROM, "2026-09-17")
    band("全段 09-05 起", CUTOFF, "2026-09-17")


# ----------------------------------------------------------------------
# B 段：估值系数校准（V̂ = VAL_COEF × max(H) 是否还成立）
# ----------------------------------------------------------------------

def val_calib(views_all: list[dict], cutoff: str) -> None:
    print("\n== B 估值系数校准：库内 拍品总价 / max(H) ==")
    print("   （现行假设 VAL_COEF=1.28；模块注释期望 sysmax13×1.35~1.44）")

    def dist(name, sel):
        rs = []
        for v in sel:
            tot = v["g"]["settle_total_price"]
            if tot and v["maxh"] > 0:
                rs.append(tot / v["maxh"])
        if not rs:
            print(f"  {name}: 无样本")
            return None
        rs.sort()
        n = len(rs)
        print("  %-22s n=%-4d 中位=%.3f  p25=%.3f  p75=%.3f  p90=%.3f  最小=%.3f"
              % (name, n, st.median(rs), rs[n // 4], rs[3 * n // 4], rs[int(n * 0.9)], rs[0]))
        return st.median(rs)

    post = [v for v in views_all if v["g"]["bucket"] >= cutoff]
    pre = [v for v in views_all if v["g"]["bucket"] < cutoff]
    med_post = dist("09-05 起（本段）", post)
    dist("08 月（对照）", pre)
    dist("其中 V4 段", [v for v in post if v["g"]["bucket"] >= V4_FROM])
    if med_post:
        print("  → 按本段中位反推：理想 V̂ 系数 ≈ %.3f；现行 1.28 使买入线"
              "（%.3f×maxH）低于「估值九成」真值（%.3f×maxH），差 %.1f%%"
              % (med_post, 0.9 * VAL_COEF, 0.9 * med_post,
                 100 * (0.9 * med_post - 0.9 * VAL_COEF) / (0.9 * VAL_COEF)))

    # 买入线安全性：按现行线成交是否必赚（对照库内真实估值）
    bad = tot_games = 0
    for v in post:
        tot = v["g"]["settle_total_price"]
        if not tot:
            continue
        tot_games += 1
        if 0.9 * VAL_COEF * v["maxh"] >= tot:
            bad += 1
    print("  买入线安全性：%d/%d 场「现行线 ≥ 真实估值」——该口径下按线成交会亏（应为 0）"
          % (bad, tot_games))


# ----------------------------------------------------------------------
# 段 C：错失机会（可胜价低于买入线却没出到）
# ----------------------------------------------------------------------

def min_win_bid(round_bids: list[int]) -> int:
    s = sorted((b for b in round_bids if b > 0), reverse=True)
    if not s:
        return 1
    if len(s) == 1:
        return 1
    return max(math.ceil(K_RATIOS[0] * 0), 0) or 0  # 占位，实际用下面的逐回合函数


def min_win_this_round(opp_round: list[int], r: int) -> int | None:
    """本回合我方要当第一名并成交所需的最低出价（三家对手读数必须全有效）。"""
    if any(x < 0 for x in opp_round):
        return None                       # -1 = 没读到，不是放弃 → 不能据此算「1 元即胜」
    valid = sorted((x for x in opp_round if x > 0), reverse=True)
    if not valid:
        return 1
    kr = K_RATIOS[r - 1]
    need = math.ceil(kr * valid[0])
    if len(valid) >= 2:
        need = max(need, valid[1] + 1)     # 成交还需第一名/第二名 ≥ K_r
    return need


def missed(views: list[dict]) -> None:
    print("\n== C 错失机会（可在我方买入线内拿下、实际没拿下）==")
    rows, total = [], 0
    for v in views:
        g = v["g"]
        tot = g["settle_total_price"]
        if not tot:
            continue
        line = 0.9 * VAL_COEF * v["maxh"]
        sr, _ = settle_round(list(v["our"]) + [b for lst in v["opp"].values() for b in [0]], [])
        # 成交回合以上一轮审计口径取库内成交价持有者所在回合
        fin = g["settle_final_price"]
        actual_r = None
        for r in range(1, 6):
            rr = [lst[r - 1] if len(lst) >= r else -1 for lst in v["opp"].values()]
            if any(x < 0 for x in rr):
                continue
            if fin and max([x for x in rr if x > 0] + [0]) >= fin:
                actual_r = r
                break
        best = None
        for r in range(1, 6):
            rr = [lst[r - 1] if len(lst) >= r else -1 for lst in v["opp"].values()]
            need = min_win_this_round(rr, r)
            if need is None or need <= 0:
                continue
            if actual_r and r > actual_r:
                break
            if need <= line:
                if best is None or need < best[1]:
                    best = (r, need)
        # 我方当回合是否已经赢了（持有成交价）
        if fin and max(v["our"]) == fin:
            continue
        if best:
            gain = tot - best[1]
            rows.append((g["bucket"], g["game_seq"], best[0], best[1], int(line), gain))
            total += gain
    rows.sort(key=lambda x: -x[5])
    for b, q, r, need, line, gain in rows[:15]:
        print("   %s #%-3d 可胜回合=R%d 最低胜出价=%-9s 买入线=%-9s 错失利润=%-9s"
              % (b, q, r, f"{need:,}", f"{line:,}", f"{gain:,}"))
    print("   合计 %d 场 / 错失利润 %s（中位 %s）"
          % (len(rows), f"{total:,}", f"{int(st.median([x[5] for x in rows])):,}" if rows else "-"))


# ----------------------------------------------------------------------
# 段 D：整链静态回放（现行策略 vs 候选改动）
# ----------------------------------------------------------------------

def snapshot_from(g: dict, v: dict, r: int) -> RoundSnapshot | None:
    """按落盘值构造第 r 轮的上一轮快照（对手三家 + 我方）。"""
    if r <= 1:
        return None
    ids, ob = [], []
    for i, (name, lst) in enumerate(sorted(v["opp"].items()), start=1):
        if len(lst) < r - 1:
            return None
        b = lst[r - 2]
        if b < 0:
            return None
        ids.append(i)
        ob.append(b)
    if len(ids) != 3:
        return None
    h_prev = v["h"][r - 2] if len(v["h"]) >= r - 1 else 0
    return RoundSnapshot(epoch=1, round_no=r - 1, h=h_prev,
                         our_bid=v["our"][r - 2] if len(v["our"]) >= r - 1 else 0,
                         opponent_bids=tuple(ob), opponent_ids=tuple(ids))


def replay(g: dict, v: dict, strat: BidStrategy | None, *, line_coef: float | None = None,
           buf_scale: float = 1.0, base_prev: bool = False) -> dict:
    """兼容壳：旧签名（line_coef/buf_scale/base_prev）→ 组合出候选策略实例后走 replay_one。"""
    if strat is not None and line_coef is None and buf_scale == 1.0 and not base_prev:
        chosen: BidStrategy = strat
    else:
        coef = line_coef or VAL_COEF
        half = buf_scale != 1.0
        chosen = _Coef(coef, half_buf=half) if not base_prev else _PrevBaseHalf()
    return replay_one(g, v, chosen)


# ---- 候选口径：继承生产类，只覆写目标开关（避免「简化复刻」把改进算成复刻误差）----

class _BufHalf(BidStrategy):
    """③ 秒杀预测缓冲减半（生产缓冲按价格桶 × 强度缩放，实测相对本轮实际对手最高价中位高估 1.10 倍）。"""

    def _buffer(self, base, vhat, opp_max, is_second=False):
        return int(super()._buffer(base, vhat, opp_max, is_second=is_second) * 0.5)


class _PrevBase(BidStrategy):
    """② 秒杀基准 M 改为「上一轮对手最高价」（M = 对手历史峰值，实测 48% 场次成交价高于它）。"""

    def decide(self, ctx):
        return super().decide(BidContext(
            round_no=ctx.round_no, h_seen=ctx.h_seen, last_round=ctx.last_round,
            balance=ctx.balance, our_last_bid=ctx.our_last_bid, opp_high_history=()))


class _Coef(BidStrategy):
    """④/⑤ 估值系数（买入线 = 0.9 × 系数 × max(H)）。"""

    def __init__(self, coef: float, half_buf: bool = False):
        super().__init__()
        self._coef, self._half = coef, half_buf

    def _vhat(self, h_seen):
        return self._coef * max(h_seen) if h_seen else 0.0

    def _buffer(self, base, vhat, opp_max, is_second=False):
        b = super()._buffer(base, vhat, opp_max, is_second=is_second)
        return int(b * 0.5) if self._half else b


class _PrevBaseHalf(_PrevBase):
    """②+③ 组合：基准改上一轮，且缓冲减半。"""

    def _buffer(self, base, vhat, opp_max, is_second=False):
        return int(super()._buffer(base, vhat, opp_max, is_second=is_second) * 0.5)


def replay_one(g: dict, v: dict, strat: BidStrategy) -> dict:
    """整链静态回放一场：返回 {round, ours_win, profit, dividend}。

    对手出价恒用落盘值（静止假设）→ 上界估计，只用于候选之间的相对排序。
    分红按 RULES §3② 复算：赢家亏钱时，我方按成交回合出价顺位拿 15/10/5%。
    """
    prices, hist = [], []
    tot = g["settle_total_price"] or 0
    for r in range(1, 6):
        if len(v["h"]) < r:
            break
        snap = snapshot_from(g, v, r)
        ctx = BidContext(round_no=r, h_seen=tuple(v["h"][:r]), last_round=snap,
                         balance=2_000_000_000,
                         our_last_bid=prices[-1] if prices else None,
                         opp_high_history=tuple(hist))
        price = strat.decide(ctx).price
        prices.append(price)
        hist.append(max(snap.opponent_bids) if snap else 0)
        rr = [lst[r - 1] if len(lst) >= r else -1 for lst in v["opp"].values()]
        if any(x < 0 for x in rr):
            continue                      # 对手读数不全 → 本回合无法判定
        table = [(price, True)] + [(x, False) for x in rr if x > 0]
        table.sort(reverse=True)
        if len(table) < 2:
            if table:
                return {"round": r, "ours_win": table[0][1],
                        "profit": tot - price if table[0][1] else 0, "dividend": 0}
            continue
        if table[0][0] >= K_RATIOS[r - 1] * table[1][0]:
            top, ours = table[0]
            if ours:
                return {"round": r, "ours_win": True, "profit": tot - price, "dividend": 0}
            loss = top - tot
            rank = 1 + sum(1 for b, _ in table[1:] if b > price) + (1 if price > 0 else 0)
            ratio = {2: 0.15, 3: 0.10, 4: 0.05}.get(rank, 0.0)
            div = int(loss * ratio) if loss > 0 else 0
            return {"round": r, "ours_win": False, "profit": 0, "dividend": div}
    return {"round": None, "ours_win": False, "profit": 0, "dividend": 0}


def sim(views: list[dict]) -> None:
    print()
    print("== D 整链静态回放（对手静止假设 → 上界估计，只比相对排序）==")
    prod = BidStrategy()
    hit = tot_n = 0
    for v in views:
        got = replay_one(v["g"], v, prod)
        exp = [b for b in v["our"][:len(got)] if b >= 0]
        # 逐回合比对生产策略复现价 vs 落盘我方出价（回放保真度）
        prices = []
        hist = []
        for r in range(1, len(exp) + 1):
            snap = snapshot_from(v["g"], v, r)
            ctx = BidContext(round_no=r, h_seen=tuple(v["h"][:r]), last_round=snap,
                             balance=2_000_000_000, opp_high_history=tuple(hist))
            p = prod.decide(ctx).price
            prices.append(p)
            hist.append(max(snap.opponent_bids) if snap else 0)
        for a, b in zip(prices, exp):
            tot_n += 1
            hit += 1 if a == b else 0
    print("  回放保真度（生产策略复现价 vs 落盘我方出价）= %d/%d = %.0f%%"
          % (hit, tot_n, 100 * hit / max(tot_n, 1)))

    cands = [
        ("现行 V4", prod),
        ("② 秒杀基准改上轮对手最高", _PrevBase()),
        ("③ 秒杀缓冲 ×0.5", _BufHalf()),
        ("④ 系数 1.35", _Coef(1.35)),
        ("⑤ 系数 1.35 + 缓冲 ×0.5", _Coef(1.35, half_buf=True)),
        ("⑥ 系数 1.20", _Coef(1.20)),
        ("⑦ ②+③ 组合", _PrevBaseHalf()),
    ]
    print("  %-26s %-5s %-12s %-11s %-11s %-8s" % ("候选", "拍中", "拍中利润", "分红", "合计收益", "亏损场"))
    for name, strat in cands:
        wins = profit = div = losses = 0
        for v in views:
            r = replay_one(v["g"], v, strat)
            if r["ours_win"]:
                wins += 1
                profit += r["profit"]
                if r["profit"] < 0:
                    losses += 1
            div += r["dividend"]
        print("  %-26s %-5d %-12s %-11s %-11s %-8d"
              % (name, wins, f"{profit:,}", f"{div:,}", f"{profit + div:,}", losses))


def main() -> int:
    views = [v for v in (game_view(g) for g in load()) if v]
    views_pre = [v for v in (game_view(g) for g in load("2026-08-15")) if v]
    print(f"样本：09-05 起可复算 {len(views)} 场；对照段（08 月）{len(views_pre)} 场")
    seg_stats(views)
    val_calib(views_pre, CUTOFF)
    missed(views)
    sim(views)
    print("\n限制：对手出价静止回放；样本 %d 场（V4 段仅 09-15 起）；"
          "结论只作候选改动的相对排序，不等于可实现收益。" % len(views))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())