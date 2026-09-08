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
    -- exit-rule snapshot, captured at open time so later config changes
    -- don't retroactively alter a trade already in flight
    tp1_multiple REAL,
    tp1_sell_fraction REAL,
    tp2_multiple REAL,
    sl_pct REAL,
    max_hold_seconds REAL,
    -- partial-exit state
    remaining_tokens REAL,
    realized_pnl_sol REAL DEFAULT 0,
    tp1_done INTEGER DEFAULT 0,
    triggered_by_wallet TEXT,           -- for strategy='og_wallet': the watched wallet address
    exit_time REAL,
    exit_price REAL,
    exit_sol_out REAL,
    exit_reason TEXT,                   -- 'take_profit_1','take_profit_2','stop_loss','time_exit'
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

-- Tracks how watched wallets actually perform once their OG-triggered paper
-- trades close, so you build a verified "these are the good ones" list from
-- real outcomes instead of just a hand-typed watchlist.
CREATE TABLE IF NOT EXISTS wallet_performance (
    wallet TEXT PRIMARY KEY,
    label TEXT,
    trades_triggered INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    total_pnl_sol REAL DEFAULT 0,
    last_updated REAL
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
               entry_price_impact_pct, entry_fee_sol, priority_fee_sol,
               tp1_multiple, tp1_sell_fraction, tp2_multiple, sl_pct, max_hold_seconds,
               triggered_by_wallet=None, meta_json=""):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (strategy, mint, status, entry_time, entry_price, entry_sol_in,
                entry_tokens, entry_price_impact_pct, entry_fee_sol, priority_fee_sol,
                tp1_multiple, tp1_sell_fraction, tp2_multiple, sl_pct, max_hold_seconds,
                remaining_tokens, triggered_by_wallet, meta_json)
               VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy, mint, time.time(), entry_price, entry_sol_in, entry_tokens,
             entry_price_impact_pct, entry_fee_sol, priority_fee_sol,
             tp1_multiple, tp1_sell_fraction, tp2_multiple, sl_pct, max_hold_seconds,
             entry_tokens, triggered_by_wallet, meta_json),
        )
        return cur.lastrowid


def log_missed(strategy, mint, miss_reason, meta_json=""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO trades (strategy, mint, status, entry_time, miss_reason, meta_json)
               VALUES (?, ?, 'missed', ?, ?, ?)""",
            (strategy, mint, time.time(), miss_reason, meta_json),
        )


def record_partial_exit(trade_id, remaining_tokens, realized_pnl_delta_sol):
    with get_conn() as conn:
        conn.execute(
            """UPDATE trades SET remaining_tokens=?, realized_pnl_sol = realized_pnl_sol + ?, tp1_done=1
               WHERE id=?""",
            (remaining_tokens, realized_pnl_delta_sol, trade_id),
        )


def close_trade(trade_id, exit_price, exit_sol_out, exit_reason, exit_fee_sol, pnl_sol, pnl_pct):
    """pnl_sol here should be the TOTAL trade P&L (realized partial + final leg)."""
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


def get_current_balance_sol():
    with get_conn() as conn:
        row = conn.execute("SELECT COALESCE(SUM(pnl_sol), 0) s FROM trades WHERE status='closed'").fetchone()
        return config.STARTING_BALANCE_SOL + (row["s"] or 0)


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


def record_wallet_trade_result(wallet, label, pnl_sol):
    """Call when an og_wallet-strategy trade closes, to build a real,
    outcome-based performance record for the wallet that triggered it."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO wallet_performance (wallet, label, trades_triggered, wins, total_pnl_sol, last_updated)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(wallet) DO UPDATE SET
                 trades_triggered = trades_triggered + 1,
                 wins = wins + ?,
                 total_pnl_sol = total_pnl_sol + ?,
                 label = excluded.label,
                 last_updated = excluded.last_updated""",
            (wallet, label, 1 if pnl_sol > 0 else 0, pnl_sol, time.time(),
             1 if pnl_sol > 0 else 0, pnl_sol),
        )


def get_wallet_performance():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_performance ORDER BY total_pnl_sol DESC"
        ).fetchall()
        return [dict(r) for r in rows]
