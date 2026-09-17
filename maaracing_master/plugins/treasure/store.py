# -*- coding: utf-8 -*-
"""鉴宝落盘子域：结构化落盘（SQLite：games 明细 + daily_summary 当日汇总）与会话总结。

TreasureStore 持有模块实例引用（与 treasure_renderer 持 debug 引用同模式），
把落盘/DB 职责从 TreasureModule 移出，让模块本体保持「状态机 + 编排」。

日界约定：凌晨 5 点为界（与 module._refresh_daily_bucket 一致），
落盘失败仅告警，不阻断自动化决策（记录是附加功能）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta

from maaracing_master.core.logger import logger


class TreasureStore:
    """鉴宝落盘存储：DB 连接管理 + 场次/当日汇总写入 + 会话总结。

    线程亲和（P2b 真机八炸后复盘定案）：SQLite 连接必须"在哪个线程用就在哪个
    线程建"。v4 通道把决策栈搬上 MaaFW Tasker 线程，`_tick_once` → 落盘
    全在桥线程执行，而连接原先在 start()（主线程）建——跨线程使用 SQLite 连接
    会抛 ProgrammingError（实测："SQLite objects created in a thread can only
    be used in that same thread"），落盘整场丢失。此处用 threading.local 让每个
    线程持有自己的连接；与之同构的先例见 wgcap.py 的零拷贝帧缓存线程模型。
    """

    def __init__(self, module):
        """module: TreasureModule 实例（状态机与落盘字段的所有者）。"""
        self._m = module
        self._local = threading.local()  # 每线程独立连接（_conn 惰性创建）

    # ---------- 线程本地连接 ----------

    @property
    def _conn(self) -> sqlite3.Connection | None:
        """当前线程的连接（惰性建连；失败仅告警，记录不阻断自动化）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            if self._m._data_dir is None:
                return None
            self._m._data_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._m._data_dir / "treasure.db"))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    game_seq INTEGER NOT NULL,
                    auction_result TEXT,
                    settle_final_price INTEGER,
                    settle_total_price INTEGER,
                    settle_profit INTEGER,
                    settle_my_income INTEGER,
                    daily_high_score INTEGER,
                    egg_red INTEGER NOT NULL DEFAULT 0,
                    egg_yellow INTEGER NOT NULL DEFAULT 0,
                    egg_blue INTEGER NOT NULL DEFAULT 0,
                    h_prices TEXT NOT NULL DEFAULT '[]',
                    our_bids TEXT NOT NULL DEFAULT '[]',
                    player_bids TEXT NOT NULL DEFAULT '{}',
                    my_rank INTEGER,
                    balance INTEGER,
                    strategy_mode TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_games_bucket ON games(bucket);
                CREATE TABLE IF NOT EXISTS daily_summary (
                    bucket TEXT PRIMARY KEY,
                    games INTEGER NOT NULL DEFAULT 0,
                    win INTEGER NOT NULL DEFAULT 0,
                    fail INTEGER NOT NULL DEFAULT 0,
                    profit_sum INTEGER NOT NULL DEFAULT 0,
                    income_sum INTEGER NOT NULL DEFAULT 0,
                    highest_score INTEGER NOT NULL DEFAULT 0,
                    egg_red INTEGER NOT NULL DEFAULT 0,
                    egg_yellow INTEGER NOT NULL DEFAULT 0,
                    egg_blue INTEGER NOT NULL DEFAULT 0,
                    egg_coin INTEGER NOT NULL DEFAULT 0,
                    egg_score INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            # 迁移：旧库 games 表可能缺少 strategy_mode 列（IF NOT EXISTS 不改已有表）
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(games)").fetchall()]
                if "strategy_mode" not in cols:
                    conn.execute("ALTER TABLE games ADD COLUMN strategy_mode TEXT")
            except Exception as e:
                logger.log(f"[鉴宝落盘] games 表迁移失败（strategy_mode 列缺失）: {e}", "WARNING")
            # 迁移：旧库 daily_summary 补「彩蛋任务领取」两列（银币/积分，stage-from-node-plan §10）
            try:
                scols = [r[1] for r in conn.execute("PRAGMA table_info(daily_summary)").fetchall()]
                for col in ("egg_coin", "egg_score"):
                    if col not in scols:
                        conn.execute(f"ALTER TABLE daily_summary ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            except Exception as e:
                logger.log(f"[鉴宝落盘] daily_summary 迁移失败（egg_coin/egg_score 列）: {e}", "WARNING")
            self._local.conn = conn
            return conn
        except Exception as e:
            logger.log(f"[鉴宝落盘] SQLite 初始化失败（数据不记录，不影响自动化）: {e}", "WARNING")
            return None

    @_conn.setter
    def _conn(self, value) -> None:
        self._local.conn = value

    # ---------- 日界 ----------

    def current_bucket_str(self, now: datetime | None = None) -> str:
        """凌晨 5 点为界的日期桶：05:00 ~ 次日 04:59 属同一天。"""
        now = now or datetime.now()
        day = now.date() if now.hour >= 5 else now.date() - timedelta(days=1)
        return day.isoformat()

    # ---------- DB 连接 ----------

    def ensure_db(self) -> None:
        """打开落盘库并建表（幂等）。start 时调用；失败仅告警（记录功能不阻断自动化）。

        v5（线程亲和修复）后此方法为轻量占位：真正建连移到 `_conn` 惰性获取
        （谁线程用、谁线程建）。保留空实现以兼容既有调用序（module.start → ensure_db）。
        """
        self._conn  # 触发当前线程惰性建连（幂等）

    def close_db(self) -> None:
        """提交并关闭**当前线程**的落盘连接（在哪个线程清理就在哪个线程关）。

        v4 通道决策栈跑 Tasker 线程，模块收尾时关闭线程可能不是建连线程；
        只关当前线程连接，避免跨线程 close 抛错。其他线程的连接随线程结束
        被 Python 回收（WAL 已持久化，无数据丢失）。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.commit()
            conn.close()
        except Exception as e:
            logger.log(f"[鉴宝落盘] 关闭连接失败: {e}", "WARNING")
        finally:
            self._local.conn = None

    # ---------- 场次落盘 ----------

    def flush_game_record(self) -> None:
        """完成一场 → 写入 SQLite：games 明细一行 + daily_summary 当日累计（UPSERT）。

        调用时机：回大厅且确认为「完整走完一场」，且本场字段（结算/彩蛋/积分）尚未清空。
        失败仅告警不阻断主循环（数据记录不影响自动化决策）。

        汇总口径（2026-09-17 定案）：
          - `profit_sum` = 我方竞拍净利，只累加**我方拍中**场的 `settle_profit`；
          - `income_sum` = 我方本场收入，全部场次都累加（拍中=利润、未拍中=分红）；
          - 当日银币净变化（看板「银币盈亏」）= `income_sum` + 蛋奖励银币（`egg_coin`）。
        """
        conn = self._conn
        if conn is None:
            return
        try:
            self._m._refresh_daily_bucket()  # 先对齐日界（跨凌晨5点重置计数），再算本场序号
            bucket = self.current_bucket_str()
            game_seq = self._m._session_daily_done_count + 1  # 落盘后 done+1，这里 +1 即本场号
            # 蛋奖励已改为「彩蛋任务页领取」（见 stage-from-node-plan §10），games 蛋列退役：
            # 不再写（列保留、默认 0，历史行不动）；蛋数只在 daily_summary 记「今日领取数」。
            conn.execute(
                """INSERT INTO games (ts, bucket, game_seq, auction_result,
                   settle_final_price, settle_total_price, settle_profit, settle_my_income,
                   daily_high_score,
                   h_prices, our_bids, player_bids, my_rank, balance, strategy_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    bucket, game_seq,
                    self._m._auction_result,                      # "win" / "fail" / null
                    self._m._settle_final_price,                  # 成交价
                    self._m._settle_total_price,                  # 系统估值
                    self._m._settle_profit,                       # 利润（中标者盈亏，可负）
                    self._m._settle_my_income,                    # 本场收入（分红）
                    self._m._daily_high_score,                    # 今日最高积分
                    json.dumps(self._m._h_prices, ensure_ascii=False),
                    json.dumps(self._m._our_bids, ensure_ascii=False),
                    json.dumps({k: list(v) for k, v in self._m._player_bids.items()}, ensure_ascii=False),
                    self._m._my_rank, self._m._my_balance,
                    "profit",               # 赚蛋模式已删，恒为赚钱（stage-from-node-plan §9）
                ),
            )
            # 当日汇总 UPSERT：读旧行累加（蛋列不在此累加——改由 record_egg_claim 记领取数）
            row = conn.execute(
                "SELECT games, win, fail, profit_sum, income_sum, highest_score"
                " FROM daily_summary WHERE bucket = ?",
                (bucket,),
            ).fetchone()
            g, w, fl, ps, inc, hs = row if row else (0, 0, 0, 0, 0, 0)
            # profit_sum 记「我方竞拍净利」：只累加**我方拍中**场的利润。未中场的
            # settle_profit 是中标者的盈亏（别人的钱，见 RULES §7 术语表），计进来等于
            # 把对手的亏赚记成我方的——2026-09-17 桶据此存出 −980,069，无意义。
            # 我方当日银币净变化 = income_sum + 彩蛋银币（收入已含未中场分红）。
            p = (
                int(self._m._settle_profit)
                if self._m._auction_result == "win"
                and isinstance(self._m._settle_profit, (int, float))
                else 0
            )
            inc_ = int(self._m._settle_my_income) if isinstance(self._m._settle_my_income, (int, float)) else 0
            hs_ = int(self._m._daily_high_score) if isinstance(self._m._daily_high_score, (int, float)) else 0
            conn.execute(
                """INSERT INTO daily_summary (bucket, games, win, fail, profit_sum,
                   income_sum, highest_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bucket) DO UPDATE SET
                     games = excluded.games,
                     win = excluded.win,
                     fail = excluded.fail,
                     profit_sum = excluded.profit_sum,
                     income_sum = excluded.income_sum,
                     highest_score = MAX(highest_score, excluded.highest_score)""",
                (bucket, g + 1, w + (1 if self._m._auction_result == "win" else 0),
                 fl + (1 if self._m._auction_result == "fail" else 0),
                 ps + p, inc + inc_, max(hs, hs_)),
            )
            conn.commit()
            logger.log(
                f"[鉴宝落盘] 已记录第 {game_seq} 场: 策略=profit 结果={self._m._auction_result or '-'} "
                f"成交={self._m._settle_final_price or 0:,} 利润={self._m._settle_profit or 0:,} "
                f"收入={self._m._settle_my_income or 0:,}",
                "INFO",
            )
        except Exception as e:
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            logger.log(f"[鉴宝落盘] 写入失败: {e}", "WARNING")

    # ---------- 彩蛋领取落盘 ----------

    def record_egg_claim(self, counts: dict, coin: int = 0, score: int = 0) -> None:
        """「彩蛋任务」收尾链读到弹窗奖励 → daily_summary 当日桶累加（今日领取数）。

        counts=蛋数 {red,yellow,blue}；coin/score=同弹窗读到的鉴宝银币/鉴宝积分金额。
        调用时机：链内每轮弹窗读数成功一次。games 蛋列已退役（§10.3），领取数只活在本表。
        与其他落盘同样：失败仅告警不阻断自动化。
        """
        conn = self._conn
        if conn is None:
            return
        try:
            self._m._refresh_daily_bucket()  # 先对齐日界，跨凌晨 5 点写进新桶
            bucket = self.current_bucket_str()
            red = int(counts.get("red") or 0)
            yellow = int(counts.get("yellow") or 0)
            blue = int(counts.get("blue") or 0)
            coin = int(coin or 0)
            score = int(score or 0)
            if red <= 0 and yellow <= 0 and blue <= 0 and coin <= 0 and score <= 0:
                return
            if min(red, yellow, blue, coin, score) < 0:
                logger.log(f"[鉴宝落盘] 领取数含负值，拒写: {counts} coin={coin} score={score}",
                           "WARNING")
                return
            conn.execute(
                """INSERT INTO daily_summary
                   (bucket, egg_red, egg_yellow, egg_blue, egg_coin, egg_score)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bucket) DO UPDATE SET
                     egg_red = egg_red + excluded.egg_red,
                     egg_yellow = egg_yellow + excluded.egg_yellow,
                     egg_blue = egg_blue + excluded.egg_blue,
                     egg_coin = egg_coin + excluded.egg_coin,
                     egg_score = egg_score + excluded.egg_score""",
                (bucket, red, yellow, blue, coin, score),
            )
            conn.commit()
            logger.log(
                f"[鉴宝落盘] 领取数已累加({bucket}): 红+{red} 黄+{yellow} 蓝+{blue} "
                f"银币+{coin:,} 积分+{score:,}",
                "INFO",
            )
        except Exception as e:
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            logger.log(f"[鉴宝落盘] 领取蛋数写入失败: {e}", "WARNING")

    # ---------- 会话总结 ----------

    def log_session_summary(self):
        """会话总结：逐行输出，首行「鉴宝观察会话总结」被前端识别为区块卡片头（可展开分组）。"""
        m = self._m
        lines: list[str] = ["鉴宝观察会话总结"]
        lines.append(f"  阶段记录     : {m._current_stage or '-'}（结束时）")
        if m._round_no is not None:
            lines.append(f"  结束回合     : {m._round_no}")
        if m._h_prices:
            lines.append(f"  系统 H 记录  : {m._h_prices}")
        if m._our_bids:
            lines.append(f"  我方出价记录 : {m._our_bids}")
        if m._h_prices and max(m._h_prices) > 0:
            H_max = max(x for x in m._h_prices if x > 0)
            lines.append(f"  H_max        : {H_max:,}  → 估值区间 ≈ {int(H_max*1.33):,} ~ {int(H_max*1.44):,}")
        if m._settle_my_income is not None or m._settle_profit is not None:
            lines.append(
                f"  本场结算     : 收入 {m._settle_my_income or 0:,} / 利润 {m._settle_profit or 0:,}"
                f"（成交价 {m._settle_final_price or 0:,} / 估值 {m._settle_total_price or 0:,}）"
            )
        if m._daily_high_score is not None:
            lines.append(f"  今日最高积分 : {m._daily_high_score:,}")
        if m._session_dir:
            lines.append(f"  保存帧数     : {m._saved_frames} (raw 全量) / {m._debug_saved} (debug 图)")
            lines.append(f"  调试目录     : {m._session_dir}")
        for _ln in lines:
            logger.log(_ln, "INFO")
