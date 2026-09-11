"""SQLite persistence: market observations, trades, positions, config knobs.

Uses a single connection guarded by a threading.Lock; every method commits
immediately. SQLite in WAL mode handles the low write volume of this app
fine, and a lock keeps GUI-thread + asyncio-thread access safe.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from .models import Position, Side, Trade

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    item TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    side TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'mock',
    author TEXT DEFAULT '',
    channel_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tx_item_ts ON transactions(item, ts);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    account TEXT NOT NULL,
    item TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    mode TEXT NOT NULL,
    pnl REAL NOT NULL DEFAULT 0,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS positions (
    account TEXT NOT NULL,
    item TEXT NOT NULL,
    qty INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    opened_ts REAL NOT NULL,
    PRIMARY KEY (account, item)
);

CREATE TABLE IF NOT EXISTS failures (
    account TEXT NOT NULL,
    item TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (account, item, action)
);

CREATE TABLE IF NOT EXISTS cooldowns (
    account TEXT NOT NULL,
    item TEXT NOT NULL,
    until_ts REAL NOT NULL,
    PRIMARY KEY (account, item)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_history (
    ts REAL PRIMARY KEY,
    account TEXT NOT NULL,
    total REAL NOT NULL,
    coins REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path = "data/dankflipper.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Market observations
    # ------------------------------------------------------------------
    def add_observation(
        self,
        item: str,
        price: float,
        quantity: int,
        side: Side | str,
        source: str = "mock",
        author: str = "",
        channel_id: int | None = None,
        ts: float | None = None,
    ) -> None:
        ts = ts or time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO transactions(ts,item,price,quantity,side,source,author,channel_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (ts, item, price, quantity, str(side), source, author, channel_id),
            )
            self._conn.commit()

    def observations(
        self,
        item: str,
        max_age_s: float | None = None,
        side: Side | None = None,
    ) -> list[sqlite3.Row]:
        q = "SELECT * FROM transactions WHERE item=?"
        args: list = [item]
        if max_age_s is not None:
            q += " AND ts>=?"
            args.append(time.time() - max_age_s)
        if side is not None:
            q += " AND side=?"
            args.append(str(side))
        q += " ORDER BY ts"
        with self._lock:
            return self._conn.execute(q, args).fetchall()

    def count_observations(self, item: str, max_age_s: float | None = None) -> int:
        q = "SELECT COUNT(*) FROM transactions WHERE item=?"
        args: list = [item]
        if max_age_s is not None:
            q += " AND ts>=?"
            args.append(time.time() - max_age_s)
        with self._lock:
            return int(self._conn.execute(q, args).fetchone()[0])

    def items(self, max_age_s: float | None = None) -> list[str]:
        q = "SELECT DISTINCT item FROM transactions"
        args: list = []
        if max_age_s is not None:
            q += " WHERE ts>=?"
            args.append(time.time() - max_age_s)
        q += " ORDER BY item"
        with self._lock:
            return [r[0] for r in self._conn.execute(q, args).fetchall()]

    def recent_events(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transactions ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------
    def add_trade(self, trade: Trade) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades(ts,account,item,side,qty,price,fees,mode,pnl,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    trade.ts, trade.account, trade.item, str(trade.side), trade.qty,
                    trade.price, trade.fees, trade.mode, trade.pnl, trade.note,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def trades(self, limit: int = 500) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    def realized_pnl(self, account: str | None = None) -> float:
        q = "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE mode != 'paper_sim'"
        args: list = []
        if account:
            q += " AND account=?"
            args.append(account)
        with self._lock:
            return float(self._conn.execute(q, args).fetchone()[0])

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    def upsert_position(self, account: str, pos: Position) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO positions(account,item,qty,avg_cost,opened_ts) VALUES(?,?,?,?,?)"
                " ON CONFLICT(account,item) DO UPDATE SET qty=excluded.qty,"
                " avg_cost=excluded.avg_cost, opened_ts=excluded.opened_ts",
                (account, pos.item, pos.qty, pos.avg_cost, pos.opened_ts),
            )
            self._conn.commit()

    def delete_position(self, account: str, item: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM positions WHERE account=? AND item=?", (account, item)
            )
            self._conn.commit()

    def positions(self, account: str) -> list[Position]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item,qty,avg_cost,opened_ts FROM positions WHERE account=?",
                (account,),
            ).fetchall()
        return [
            Position(r["item"], r["qty"], r["avg_cost"], r["opened_ts"]) for r in rows
        ]

    # ------------------------------------------------------------------
    # Failures / cooldowns
    # ------------------------------------------------------------------
    def record_failure(self, account: str, item: str, action: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO failures(account,item,action,reason,ts) VALUES(?,?,?,?,?)"
                " ON CONFLICT(account,item,action) DO UPDATE SET reason=excluded.reason,"
                " ts=excluded.ts",
                (account, item, action, reason, time.time()),
            )
            self._conn.commit()

    def failure_count(self, account: str, item: str, action: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM failures WHERE account=? AND item=? AND action=?",
                (account, item, action),
            ).fetchone()
        if not row:
            return 0
        # The failures table stores only the latest failure per (acct,item,action);
        # repeated counting is handled by trading engine incrementing reasons.
        return 1

    def failures(self, account: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM failures WHERE account=? ORDER BY ts DESC", (account,)
            ).fetchall()

    def set_cooldown(self, account: str, item: str, until_ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cooldowns(account,item,until_ts) VALUES(?,?,?)"
                " ON CONFLICT(account,item) DO UPDATE SET until_ts=excluded.until_ts",
                (account, item, until_ts),
            )
            self._conn.commit()

    def on_cooldown(self, account: str, item: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT until_ts FROM cooldowns WHERE account=? AND item=?",
                (account, item),
            ).fetchone()
        return bool(row and row[0] > time.time())

    def clear_cooldowns(self, account: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cooldowns WHERE account=?", (account,))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Settings (persisted GUI knobs) & equity history
    # ------------------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else default

    def add_equity(self, account: str, total: float, coins: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_history(ts,account,total,coins)"
                " VALUES(?,?,?,?)",
                (now, account, total, coins),
            )
            self._conn.commit()

    def equity_history(
        self, account: str, since_ts: float = 0
    ) -> list[tuple[float, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,total FROM equity_history WHERE account=? AND ts>=? ORDER BY ts",
                (account, since_ts),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ------------------------------------------------------------------
    # Maintenance / analytics helpers
    # ------------------------------------------------------------------
    def prune_observations(self, max_age_s: float = 7 * 24 * 3600) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM transactions WHERE ts < ?", (time.time() - max_age_s,)
            )
            self._conn.commit()
            return cur.rowcount

    def stats_summary(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, "
                "SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) losses, "
                "COALESCE(SUM(pnl),0) pnl FROM trades WHERE side='sell'"
            ).fetchone()
        return dict(row)
