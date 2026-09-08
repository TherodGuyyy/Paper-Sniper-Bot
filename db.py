import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,             -- 'launch' or 'og_wallet'
    mint TEXT NOT NULL,
    status TEXT NOT NULL,               -- 'open', 'closed', 'missed'
    entry_time REAL,
    entry_price REAL,
    entry_sol_in REAL,
    entry_tokens REAL,
    entry_price_impact_pct REAL,
    entry_fee_sol REAL,
    priority_fee_sol REAL,
    exit_time REAL,
    exit_price REAL,
    exit_sol_out REAL,
    exit_reason TEXT,                   -- 'take_profit','stop_loss','time_exit'
    exit_fee_sol REAL,
    pnl_sol REAL,
    pnl_pct REAL,
    miss_reason TEXT,                   -- filled if status='missed'
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    wallet TEXT PRIMARY KEY,
    label TEXT,
    added_time REAL,
    last_seen_active REAL,
    successor_of TEXT
);
"""


def init_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def open_trade(strategy, mint, entry_price, entry_sol_in, entry_tokens,
               entry_price_impact_pct, entry_fee_sol, priority_fee_sol, meta_json=""):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (strategy, mint, status, entry_time, entry_price, entry_sol_in,
                entry_tokens, entry_price_impact_pct, entry_fee_sol, priority_fee_sol, meta_json)
               VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy, mint, time.time(), entry_price, entry_sol_in, entry_tokens,
             entry_price_impact_pct, entry_fee_sol, priority_fee_sol, meta_json),
        )
        return cur.lastrowid


def log_missed(strategy, mint, miss_reason, meta_json=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO trades (strategy, mint, status, entry_time, miss_reason, meta_json)
               VALUES (?, ?, 'missed', ?, ?, ?)""",
            (strategy, mint, time.time(), miss_reason, meta_json),
        )


def close_trade(trade_id, exit_price, exit_sol_out, exit_reason, exit_fee_sol, pnl_sol, pnl_pct):
    with get_conn() as conn:
        conn.execute(
            """UPDATE trades SET status='closed', exit_time=?, exit_price=?, exit_sol_out=?,
               exit_reason=?, exit_fee_sol=?, pnl_sol=?, pnl_pct=? WHERE id=?""",
            (time.time(), exit_price, exit_sol_out, exit_reason, exit_fee_sol, pnl_sol, pnl_pct, trade_id),
        )


def get_open_trades(strategy=None):
    with get_conn() as conn:
        if strategy:
            rows = conn.execute("SELECT * FROM trades WHERE status='open' AND strategy=?", (strategy,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
        return [dict(r) for r in rows]


def get_stats_since(since_ts):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND exit_time >= ?", (since_ts,)
        ).fetchall()
        missed = conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE status='missed' AND entry_time >= ?", (since_ts,)
        ).fetchone()["c"]
        return [dict(r) for r in rows], missed


def upsert_watchlist(wallet, label="", successor_of=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO watchlist (wallet, label, added_time, last_seen_active, successor_of)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(wallet) DO UPDATE SET label=excluded.label""",
            (wallet, label, time.time(), time.time(), successor_of),
        )


def touch_watchlist_wallet(wallet):
    with get_conn() as conn:
        conn.execute("UPDATE watchlist SET last_seen_active=? WHERE wallet=?", (time.time(), wallet))


def get_watchlist():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM watchlist").fetchall()
        return [dict(r) for r in rows]
