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
    -- runner tier (Sep 2026): if tp2_sell_fraction is set, hitting TP2 sells
    -- only that fraction of what's left instead of closing the position —
    -- the remainder becomes a "runner" protected by runner_trail_pct instead
    -- of a fixed target. NULL tp2_sell_fraction = old behavior (full close).
    tp2_sell_fraction REAL,
    tp2_done INTEGER DEFAULT 0,
    runner_trail_pct REAL,
    triggered_by_wallet TEXT,           -- for strategy='og_wallet': the watched wallet address
    exit_time REAL,
    exit_price REAL,
    exit_sol_out REAL,
    exit_reason TEXT,                   -- 'take_profit_1','take_profit_2','stop_loss','time_exit'
    exit_fee_sol REAL,
    pnl_sol REAL,
    pnl_pct REAL,
    peak_price REAL,      -- highest PLAUSIBLE price observed while open (see MAX_EXIT_MULTIPLE_SANITY_FACTOR)
    peak_multiple REAL,   -- that peak, expressed as a multiple of entry_price
    window_peak_price REAL,     -- TRUE peak: highest plausible price seen within PEAK_WINDOW_SECONDS of ENTRY, even after we sold
    window_peak_multiple REAL,  -- that true peak as a multiple of entry_price (NULL until the window ends)
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

-- Small generic key/value store for things like a manual balance
-- adjustment set via a Telegram command
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value REAL
);

-- Passive discovery: candidate wallets found by watching ALL launches (not
-- just ones we trade) for wallets that keep buying early into winners.
-- Nothing here gets auto-traded — this is a review list for the user.
CREATE TABLE IF NOT EXISTS wallet_discovery (
    wallet TEXT PRIMARY KEY,
    appearance_count INTEGER DEFAULT 0,
    mints_json TEXT DEFAULT '[]',
    first_seen REAL,
    last_seen REAL,
    alerted INTEGER DEFAULT 0
);
"""


def init_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(SCHEMA)
    # Migration for DBs created before peak tracking existed — CREATE TABLE
    # IF NOT EXISTS above doesn't add columns to an already-existing table.
    for col in ("peak_price REAL", "peak_multiple REAL",
                "tp2_sell_fraction REAL", "tp2_done INTEGER DEFAULT 0", "runner_trail_pct REAL",
                "window_peak_price REAL", "window_peak_multiple REAL"):
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # column already exists
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
               triggered_by_wallet=None, meta_json="",
               tp2_sell_fraction=None, runner_trail_pct=None):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (strategy, mint, status, entry_time, entry_price, entry_sol_in,
                entry_tokens, entry_price_impact_pct, entry_fee_sol, priority_fee_sol,
                tp1_multiple, tp1_sell_fraction, tp2_multiple, sl_pct, max_hold_seconds,
                remaining_tokens, triggered_by_wallet, meta_json,
                tp2_sell_fraction, runner_trail_pct)
               VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy, mint, time.time(), entry_price, entry_sol_in, entry_tokens,
             entry_price_impact_pct, entry_fee_sol, priority_fee_sol,
             tp1_multiple, tp1_sell_fraction, tp2_multiple, sl_pct, max_hold_seconds,
             entry_tokens, triggered_by_wallet, meta_json,
             tp2_sell_fraction, runner_trail_pct),
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


def record_tp2_partial_exit(trade_id, remaining_tokens, realized_pnl_delta_sol):
    """Same idea as record_partial_exit, but for the TP2 leg on a runner
    position — flags tp2_done instead of tp1_done, and leaves the position
    open (the remainder is now protected by runner_trail_pct, not a fixed
    target)."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE trades SET remaining_tokens=?, realized_pnl_sol = realized_pnl_sol + ?, tp2_done=1
               WHERE id=?""",
            (remaining_tokens, realized_pnl_delta_sol, trade_id),
        )


def get_buy_count_for_wallet_mint(wallet_addr: str, mint: str) -> int:
    """How many times we've already paper-bought this mint off this watched
    wallet (open or closed — 'missed' attempts never actually bought, so
    they don't count against the cap)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE triggered_by_wallet=? AND mint=? AND status IN ('open','closed')",
            (wallet_addr, mint),
        ).fetchone()
        return row["c"]


def close_trade(trade_id, exit_price, exit_sol_out, exit_reason, exit_fee_sol, pnl_sol, pnl_pct,
                 peak_price=None, peak_multiple=None):
    """pnl_sol here should be the TOTAL trade P&L (realized partial + final leg)."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE trades SET status='closed', exit_time=?, exit_price=?, exit_sol_out=?,
               exit_reason=?, exit_fee_sol=?, pnl_sol=?, pnl_pct=?, peak_price=?, peak_multiple=? WHERE id=?""",
            (time.time(), exit_price, exit_sol_out, exit_reason, exit_fee_sol, pnl_sol, pnl_pct,
             peak_price, peak_multiple, trade_id),
        )


def record_window_peak(trade_id, peak_price, peak_multiple):
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET window_peak_price=?, window_peak_multiple=? WHERE id=?",
            (peak_price, peak_multiple, trade_id),
        )


def get_trades_awaiting_window_peak(since_ts):
    """Closed trades that opened after since_ts but never got their window
    peak recorded (the bot restarted mid-window) — used on startup to
    resume watching the ones whose window hasn't ended yet."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND window_peak_multiple IS NULL AND entry_time >= ?",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_window_peaks(strategy=None):
    """Every trade that has a recorded true peak, oldest first."""
    with get_conn() as conn:
        q = "SELECT * FROM trades WHERE window_peak_multiple IS NOT NULL"
        args = ()
        if strategy:
            q += " AND strategy=?"
            args = (strategy,)
        rows = conn.execute(q + " ORDER BY entry_time", args).fetchall()
        return [dict(r) for r in rows]


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
        adj_row = conn.execute("SELECT value FROM settings WHERE key='manual_adjustment_sol'").fetchone()
        adjustment = adj_row["value"] if adj_row else 0.0
        return config.STARTING_BALANCE_SOL + (row["s"] or 0) + adjustment


def add_manual_balance_adjustment(delta_sol: float):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='manual_adjustment_sol'").fetchone()
        current = row["value"] if row else 0.0
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('manual_adjustment_sol', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (current + delta_sol,),
        )


def clear_manual_balance_adjustment():
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key='manual_adjustment_sol'")


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


def record_discovery_appearance(wallet: str, mint: str) -> int:
    """Call when a wallet is seen as an early buyer on a token that hit the
    discovery success threshold. Returns the new appearance_count."""
    import json
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM wallet_discovery WHERE wallet=?", (wallet,)).fetchone()
        if row:
            mints = json.loads(row["mints_json"] or "[]")
            mints = (mints + [mint])[-5:]  # keep last 5 examples
            new_count = row["appearance_count"] + 1
            conn.execute(
                "UPDATE wallet_discovery SET appearance_count=?, mints_json=?, last_seen=? WHERE wallet=?",
                (new_count, json.dumps(mints), time.time(), wallet),
            )
            return new_count
        else:
            conn.execute(
                "INSERT INTO wallet_discovery (wallet, appearance_count, mints_json, first_seen, last_seen, alerted) "
                "VALUES (?, 1, ?, ?, ?, 0)",
                (wallet, json.dumps([mint]), time.time(), time.time()),
            )
            return 1


def get_discovery_wallet(wallet: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM wallet_discovery WHERE wallet=?", (wallet,)).fetchone()
        return dict(row) if row else None


def mark_discovery_alerted(wallet: str):
    with get_conn() as conn:
        conn.execute("UPDATE wallet_discovery SET alerted=1 WHERE wallet=?", (wallet,))


def get_discovery_candidates():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_discovery ORDER BY appearance_count DESC"
        ).fetchall()
        return [dict(r) for r in rows]
