# -*- coding: utf-8 -*-
"""鉴宝落盘库（treasure.db）历史脏数据修复：结算三笔金额与当日汇总。

用途：2026-09-17 之前的版本在结算页有两处读数缺陷，历史行因此存了错值。
本工具按**游戏规则**（`plugins/treasure/RULES.md`）把可推导的错值改对，
推不出来的只列出来、不猜：

1. `games.settle_profit` — 旧版独立 OCR 利润行且带「不低于 Hmax/20」护栏：
   薄利赢单的真实小额利润被当裁位残缺丢弃，滚动动画的中间值反而被固化。
   `RULES §3/§7`：利润 = 拍品总价 − 最终竞拍价 → 可直接重算。
2. `games.settle_my_income`（我方拍中）— 旧版转场串位把大厅场次卡的
   「资产要求 300,000」当成本场收入落盘。`RULES §3①`：拍中者收入 = 利润
   = 拍品总价 − 最终竞拍价 → 可直接重算。
3. `games.settle_my_income`（未拍中且中标者未亏）— `RULES §3②`：
   「若拍中者正利润（收入 ≥ 0），其他所有玩家收入为 0」→ 应为 0。
4. `daily_summary.profit_sum` / `income_sum` — 汇总列必须等于逐行加总；
   `profit_sum` 口径另修正为「只算我方拍中场的利润」（未中场的利润是中标者的钱）。
   本步**从 games 逐行重算**，任何桶的汇总都重新对齐到明细。

**不做**：未拍中且中标者亏钱、但收入与 5/10/15% 三方分红都对不上的行——真值取决于
我方出价顺位，而库内 `my_rank` 与分红比例经核对并不一致（顺位读取不可靠）、
`our_bids` 也是空的，无从推导 → 只列出、不猜（发明数据比留着错值更糟）。

用法（先 dry-run 看清单，再 --apply）：
    .venv\\Scripts\\python.exe scripts/repair_treasure_settle_data.py
    .venv\\Scripts\\python.exe scripts/repair_treasure_settle_data.py --apply

落盘前强制备份到 `<db>.bak-<时间戳>`（含 -wal/-shm）；修复在单事务内完成。
执行前请**关闭 GUI/自动化**（SQLite 单写者，边跑边改会互相干扰）。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path.home() / "AppData" / "Roaming" / "MaaRacingMaster" / "data" / "treasure" / "treasure.db"


def _dividend_ratios(loss: int) -> tuple[int, int, int]:
    """中标者亏损额 → 第 2/3/4 名的分红（游戏按截断取整，实测 124,707×10% = 12,470）。"""
    return tuple(int(loss * r) for r in (0.05, 0.10, 0.15))  # type: ignore[return-value]


def _conforming_income(result: str | None, final: int, total: int, income: int) -> bool:
    """收入读数是否符合 RULES §3（只看比例自洽，不判对错来源）。"""
    derived = total - final
    if result == "win":
        return income == derived
    if result == "fail":
        loss = -derived
        if loss <= 0:
            return income == 0
        opts = _dividend_ratios(loss)
        return any(abs(income - o) <= 1 for o in opts)  # ±1 容 OCR/取整抖动
    return False


def plan_repairs(conn: sqlite3.Connection) -> dict:
    """算出全部修复动作（纯读，不改库）。返回 {profit_fix, win_income_fix, fail_income_fix, unfixable}。"""
    conn.row_factory = sqlite3.Row
    profit_fix, win_income_fix, fail_income_fix, unfixable = [], [], [], []
    for r in conn.execute(
        "SELECT id, bucket, game_seq, auction_result, settle_final_price,"
        " settle_total_price, settle_profit, settle_my_income FROM games ORDER BY bucket, game_seq"
    ):
        final, total = r["settle_final_price"], r["settle_total_price"]
        if final is None or total is None:
            continue  # 两个价格缺任一 → 无派生依据，跳过
        derived = total - final
        if r["settle_profit"] != derived:
            profit_fix.append((r["id"], r["bucket"], r["game_seq"], r["settle_profit"], derived))
        income = r["settle_my_income"]
        if not isinstance(income, int):
            continue
        if r["auction_result"] == "win":
            if income != derived:
                win_income_fix.append((r["id"], r["bucket"], r["game_seq"], income, derived))
        elif r["auction_result"] == "fail":
            if derived >= 0 and income != 0:
                fail_income_fix.append((r["id"], r["bucket"], r["game_seq"], income, 0))
            elif derived < 0 and not _conforming_income("fail", final, total, income):
                unfixable.append((r["bucket"], r["game_seq"], final, total, income, -derived))
    return {
        "profit_fix": profit_fix,
        "win_income_fix": win_income_fix,
        "fail_income_fix": fail_income_fix,
        "unfixable": unfixable,
    }


def _summaries(conn: sqlite3.Connection, *, profit_override=None, income_override=None) -> dict:
    """按明细加总每桶汇总（口径见文件头）。

    `profit_override` / `income_override`：{行 id → 修复后的值}，用于投影「修复后」的
    汇总。**不能**改在库内旧汇总上做增量——旧 profit_sum 是旧口径（含未中场的中标者
    盈亏），与逐行加总本就不等，增量叠上去等于把旧口径留在里面（首次实现即此错，
    落库后复验当场抓出）。
    """
    po = profit_override or {}
    io = income_override or {}
    agg: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT id, bucket, auction_result, settle_profit, settle_my_income FROM games"
    ):
        d = agg.setdefault(r["bucket"], {"profit_sum": 0, "income_sum": 0, "games": 0,
                                         "win": 0, "fail": 0})
        d["games"] += 1
        income = io.get(r["id"], r["settle_my_income"])
        profit = po.get(r["id"], r["settle_profit"])
        if isinstance(income, int):
            d["income_sum"] += income
        if r["auction_result"] == "win":
            d["win"] += 1
            if isinstance(profit, int):
                d["profit_sum"] += profit
        elif r["auction_result"] == "fail":
            d["fail"] += 1
    return agg


def _overrides(plan: dict) -> tuple[dict, dict]:
    """行级修复计划 → (利润覆盖, 收入覆盖)。"""
    profit = {row_id: new for row_id, _b, _s, _o, new in plan["profit_fix"]}
    income = {row_id: new for row_id, _b, _s, _o, new in plan["win_income_fix"]}
    income.update({row_id: new for row_id, _b, _s, _o, new in plan["fail_income_fix"]})
    return profit, income


def _summary_fix(conn: sqlite3.Connection, agg: dict | None = None) -> list[tuple]:
    """汇总列与明细不一致的桶，返回 [(bucket, 列, 旧, 新)]。

    `agg` 给定时用它（即「按修复后的明细」投影），否则用库内当前明细。
    """
    agg = agg if agg is not None else _summaries(conn)
    out = []
    for r in conn.execute("SELECT bucket, profit_sum, income_sum FROM daily_summary"):
        d = agg.get(r["bucket"])
        if d is None:
            continue
        for col in ("profit_sum", "income_sum"):
            if r[col] != d[col]:
                out.append((r["bucket"], col, r[col], d[col]))
    return out


def project_summaries(conn: sqlite3.Connection, plan: dict) -> dict:
    """修复后的每桶汇总（dry-run 与落库共用同一份口径）。"""
    profit, income = _overrides(plan)
    return _summaries(conn, profit_override=profit, income_override=income)


def _backup(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db.with_suffix(db.suffix + f".bak-{stamp}")
    shutil.copy2(db, dest)
    for extra in ("-wal", "-shm"):
        side = db.with_suffix(db.suffix + extra)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + extra))
    return dest


def _apply(conn: sqlite3.Connection, plan: dict, summary_fix: list[tuple]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    for rows, col in ((plan["profit_fix"], "settle_profit"),
                      (plan["win_income_fix"], "settle_my_income"),
                      (plan["fail_income_fix"], "settle_my_income")):
        for row_id, _bucket, _seq, _old, new in rows:
            conn.execute(f"UPDATE games SET {col} = ? WHERE id = ?", (new, row_id))
    for bucket, col, _old, new in summary_fix:
        conn.execute(f"UPDATE daily_summary SET {col} = ? WHERE bucket = ?", (new, bucket))
    conn.commit()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="修复鉴宝落盘库的历史结算金额与当日汇总")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"库路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只打印计划）")
    ap.add_argument("--limit", type=int, default=12, help="每类明细打印条数（默认 12）")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"库不存在：{args.db}")
        return 1

    conn = sqlite3.connect(str(args.db))
    try:
        plan = plan_repairs(conn)
        summary_fix = _summary_fix(conn, project_summaries(conn, plan))
        print(f"库：{args.db}")
        print(f"  利润重算（拍品总价 − 最终竞拍价） : {len(plan['profit_fix'])} 行")
        print(f"  收入重算（我方拍中=利润）         : {len(plan['win_income_fix'])} 行")
        print(f"  收入归零（未中且中标者未亏）      : {len(plan['fail_income_fix'])} 行")
        print(f"  当日汇总对齐明细                  : {len(summary_fix)} 行")
        print(f"  推不出来、只列出（未中·中标者亏·收入不符三方分红）: {len(plan['unfixable'])} 行")
        for name, idx in (("利润重算", "profit_fix"), ("收入重算", "win_income_fix"),
                          ("收入归零", "fail_income_fix")):
            for bucket, seq, old, new in [(b, s, o, n) for _i, b, s, o, n in plan[idx][: args.limit]]:
                print(f"    [{name}] {bucket} #{seq}: {old} → {new}")
        for bucket, col, old, new in summary_fix:
            print(f"    [汇总] {bucket} {col}: {old} → {new}")
        if plan["unfixable"]:
            print("  未能修复（真值取决于我方出价顺位，库内无可靠依据）：")
            for bucket, seq, final, total, income, loss in plan["unfixable"][: args.limit]:
                print(f"    {bucket} #{seq}: 收入={income} 中标者亏损={loss}（应为 {_dividend_ratios(loss)} 之一）")

        if not args.apply:
            print("\n[dry-run] 未写库。确认无误后加 --apply 执行（会先备份）。")
            return 0

        backup = _backup(args.db)
        _apply(conn, plan, summary_fix)
        print(f"\n已修复，备份：{backup}")

        after = plan_repairs(conn)
        left = len(after["profit_fix"]) + len(after["win_income_fix"]) + len(after["fail_income_fix"])
        drift = len(_summary_fix(conn))
        print(f"复验：剩余可修复行 {left}（应为 0）、汇总与明细不一致 {drift}（应为 0）")
        return 0 if left == 0 and drift == 0 else 2
    except Exception as e:  # noqa: BLE001 —— 工具脚本：异常原样抛出定位
        conn.rollback()
        print(f"修复失败（已回滚）：{e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    time.sleep(0)  # 保证 Ctrl-C 可中断（长事务前的惯例）
    raise SystemExit(main())