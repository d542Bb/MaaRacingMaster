# -*- coding: utf-8 -*-
"""TreasureStore 线程亲和回归（P2b 真机落盘崩溃定案）。

v4 通道把决策栈搬上 MaaFW Tasker 线程后，`_tick_once` → 落盘全在桥线程执行，
而连接原先在主线程 start() 时建——跨线程用 sqlite 连接抛 ProgrammingError
（日志实拍："SQLite objects created in a thread..."），落盘整场丢失。
修复：store 用 threading.local 每线程独立连接，谁线程用谁线程建。

本测试只验证连接机制（不依赖交易所涉及的海量 module 字段），直接驱动
`_conn` 属性与 `close_db`：
- 主线程 `_conn` 与子线程 `_conn` 指向不同的 sqlite 连接对象；
- 子线程在建连线程内 execute 不抛错；
- 各自 close 不交叉。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from maaracing_master.plugins.treasure.store import TreasureStore


class _FakeModule:
    """store 只依赖 module._data_dir；其余字段 flush 时才用，测试不走 flush。"""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir


@pytest.fixture
def store() -> TreasureStore:
    with tempfile.TemporaryDirectory() as d:
        store_ = TreasureStore(_FakeModule(Path(d) / "data"))
        yield store_
        store_.close_db()  # 关当前线程连接，释放 WAL 文件句柄（Windows 清理目录需无占用）
    # 子线程连接随线程结束回收；临时目录退出时主线程句柄已释放。


def test_conn_is_lazy_and_same_thread_reusable(store):
    """首次取 _conn 即建连、重复取值复用同一连接对象（惰性+幂等）。"""
    c1 = store._conn
    c2 = store._conn
    assert isinstance(c1, sqlite3.Connection)
    assert c1 is c2  # 同线程复用同一连接


def test_conn_instance_differs_across_threads(store):
    """主线程与子线程各自持独立连接（thread-local），互不共享。"""
    main_conn = store._conn
    got: list = []

    def worker():
        got.append(store._conn)  # 子线程首次触达 → 独立建连
        store.close_db()  # 收尾：子线程关自己的连接，释放文件句柄

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(got) == 1
    assert got[0] is not main_conn  # 线程独立连接，非共享


def test_worker_can_write_in_its_own_thread(store):
    """子线程在"建连线程"内 execute 正常 —— 修复前跨线程会抛 ProgrammingError。"""
    def worker():
        conn = store._conn
        assert conn is not None
        conn.execute("CREATE TABLE IF NOT EXISTS t (n INTEGER)")
        conn.execute("INSERT INTO t (n) VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
        store.close_db()  # 收尾：子线程关自己的连接

    t = threading.Thread(target=worker)
    t.start()
    t.join()


def test_close_db_only_closes_current_thread_conn(store):
    """close_db 提交/关闭当前线程连接；不碰其他线程的连接（跨线程 close 会抛错）。"""
    main_conn = store._conn
    store.close_db()
    assert main_conn is not None
    with pytest.raises(sqlite3.ProgrammingError):
        main_conn.execute("SELECT 1")

    # 子线程连接不受主线程 close 影响，仍可用，且在其自己线程内可正常关
    state: list = []

    def worker():
        c = store._conn
        try:
            c.execute("SELECT 1")
            state.append("ok")
        except sqlite3.ProgrammingError:
            state.append("closed")
        store.close_db()  # 子线程收尾

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert state == ["ok"]