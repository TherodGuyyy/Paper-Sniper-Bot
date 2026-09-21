"""
All tunable parameters live here. Nothing else in the codebase should hardcode
a threshold — change behavior by editing this file only.
"""

import os

# ---------------------------------------------------------------------------
# Secrets / connections (set these as environment variables, see .env.example)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")  # reuse the same key from smart-wallet-finder

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"

# ---------------------------------------------------------------------------
# STRATEGY A — new-launch sniping with filters
# ---------------------------------------------------------------------------
LAUNCH_SNIPE_ENABLED = False  # TURNED OFF after real testing showed negative P&L — pivoting to wallet-copy strategy (Strategy B) instead. Kept in the code in case you want to revisit it later, e.g. combined with the discovery feature below.

# Position sizing (paper — no real SOL moves, but must be realistic to size
# slippage/fees correctly)
LAUNCH_BUY_SIZE_SOL = 0.05

# Hard cap on how many launch positions can be open at once. This is the
# main defense against alert flooding — pump.fun produces far more launches
# per minute than any filter alone will meaningfully cut down, so once
# you're holding this many, new candidates are skipped until one closes.
MAX_CONCURRENT_LAUNCH_POSITIONS = 8
MAX_CONCURRENT_OG_POSITIONS = 5

# Simulated latency: time between "we saw the launch event" and "our buy
# transaction would land on-chain", in milliseconds. This is NOT a guess —
# during this window we let real subsequent trades from other wallets happen
# and buy against the ACTUAL reserve state at that later point, so the sim
# naturally captures "other bots got there first and moved the price".
# Set this based on your real infra tier once you know it; 400-900ms is a
# reasonable range for a solid RPC + no Jito relationship.
SIMULATED_LATENCY_MS_MIN = 400
SIMULATED_LATENCY_MS_MAX = 900

# Cost modeling
PUMPFUN_FEE_PCT = 0.01          # 1% platform fee, applied on both buy and sell — VERIFY against a real receipt before trusting this number
PRIORITY_FEE_SOL_MIN = 0.001    # modeled Jito-tip-equivalent cost, charged whether the trade wins or not
PRIORITY_FEE_SOL_MAX = 0.006
MAX_SLIPPAGE_PCT = 0.15         # if real price impact by the time our buy lands exceeds this, the trade is marked MISSED, not filled — this is what actually kills naive snipers

# Entry filters — a launch must pass ALL of these to be paper-bought
FILTER_MIN_INITIAL_BUY_SOL = 2.0      # RAISED from 0.5 — that let through almost every launch. This is a much stronger conviction bar.
FILTER_MAX_CREATOR_PRIOR_TOKENS = 15  # skip serial launchers with a huge past token count (serial ruggers / spam)
FILTER_REQUIRE_CREATOR_HISTORY_CHECK = True  # set False if Helius calls are too slow/unreliable and you want speed over filtering, or to save Helius credits

# --- rug-pattern filters below: both are FREE (zero extra API calls, zero extra latency) — they use data already in the launch/trade events we're already receiving ---

PUMPFUN_TOTAL_SUPPLY = 1_000_000_000  # standard fixed supply for every pump.fun token — VERIFY this against a live raw sample if a launch's math looks off
FILTER_MAX_CREATOR_SUPPLY_PCT = 0.15  # reject if the creator's own opening buy already gives them more than this share of total supply — a real, instant rug-risk signal (heavy dev allocation = they can crash the price alone), computed for free from the create event itself

FILTER_BUNDLE_MAX_OTHER_BUYERS = 1  # if MORE than this many other wallets buy within the same short latency window, treat it as a suspected coordinated bundle (a bundler launching many wallets in the same slot) rather than organic interest, and reject. Note this replaced an earlier MIN-buyers filter that required at least 1 other buyer as a positive signal — that direction was backwards: genuine organic follow-on buying that fast is rare, so seeing SEVERAL buyers that fast is actually more indicative of a bundle than of real interest.

# Exit logic (Strategy A) — multiple-based, with a scale-out:
# at TP1_MULTIPLE (price = entry * multiple), sell TP1_SELL_FRACTION of the
# position and let the rest ride toward TP2_MULTIPLE. If price never gets
# there, stop-loss or the time exit still protects the remainder.
TP1_MULTIPLE = 2.5          # e.g. 2.5 = exit partial at +150%
TP1_SELL_FRACTION = 0.5     # sell half at TP1, let the other half ride
TP2_MULTIPLE = 3.0          # remaining position exits fully here if reached
STOP_LOSS_PCT = 0.35        # applies to the REMAINING position at any point
MAX_HOLD_SECONDS = 900      # force-exit remaining position after 15 minutes regardless of P&L
DEAD_TOKEN_EXIT_SECONDS = 90  # NEW — if a position has seen ZERO trade activity since entry (common — most launches get no volume at all) for this long, close it early instead of occupying a slot for the full 15 minutes doing nothing

# ---------------------------------------------------------------------------
# STRATEGY B — OG-token revival: buy when a watched wallet buys a dormant
# token that isn't a brand-new launch
# ---------------------------------------------------------------------------
OG_WALLET_SNIPE_ENABLED = True

OG_BUY_SIZE_SOL = 0.03

# A token counts as "dormant" if it's older than this AND we've observed no
# trades on it (in our own running session) for at least this many hours
# before the watched wallet's buy. This is a best-effort proxy — see README
# for the real limitation (we only see trade history from when the bot
# itself starts running, not the token's full life).
DORMANT_MIN_TOKEN_AGE_HOURS = 24
DORMANT_MIN_QUIET_HOURS = 6

# FIX (Sep 2026): the dormancy check above requires the bot to have watched a
# token get created AND then age 24h+, which almost nothing satisfies in
# practice (either too new, or "unknown age" because it predates this run).
# That silently blocked nearly all real copy-trades from firing at all,
# defeating the actual goal — blind-copying everything a watched wallet does,
# farms included, to measure overall profitability. Off by default now so
# every watched-wallet buy is attempted. Flip back to True only if you
# specifically want the narrower "old dormant token revival" behavior.
OG_REQUIRE_DORMANT_TOKEN = False

# Exit logic (Strategy B) — DEFAULTS used only when a watched wallet has no
# entry in WALLET_EXIT_OVERRIDES below. With two very different real traders
# now in play (a rare, big-move guy vs. a daily high-frequency one), one
# shared exit shape doesn't fit both — set per-wallet overrides instead.
OG_TP1_MULTIPLE = 1.5
OG_TP1_SELL_FRACTION = 0.5
OG_TP2_MULTIPLE = 2.0
OG_STOP_LOSS_PCT = 0.30
OG_MAX_HOLD_SECONDS = 900

# Per-wallet exit overrides, keyed by wallet address. Any key you don't set
# for a wallet falls back to the OG_* defaults above.
WALLET_EXIT_OVERRIDES = {
    # "7pm guy" — buys real conviction plays at ~300-500 mc that move fast,
    # but only does this once every 2-3 days now (the rest of the time he's
    # quiet). Real prior trading history showed 1.3-1.5x typical, sometimes
    # 2x actually captured (well below the token's theoretical move, since
    # slippage/timing eats most of a naive "buy at 300mc, sell at 2k mc"
    # fantasy return) — targets set to match REAL captured outcomes, not
    # the token's theoretical ceiling. Rare/high-conviction -> bigger size,
    # small reserved concurrency slot so a busy day for Meme Detective can
    # never lock him out.
    "2YBHj8kf7AMgwhMKfMUxsyBk9UuX87FUq7UFFQDL9Atq": {
        "tp1_multiple": 1.4, "tp1_sell_fraction": 0.5, "tp2_multiple": 2.0,
        "sl_pct": 0.30, "max_hold_seconds": 900,
        "buy_size_sol": 0.08, "max_concurrent": 2,
    },
    # Meme Detective — trades daily, farms a lot (many low-quality buys),
    # occasionally shills a real mover on Twitter. Decided against building
    # a farm-vs-real classifier for him (tried something similar before,
    # didn't pan out) — accepting the farming as a cost of doing business
    # and leaning on fast execution instead: smaller size per trade (since
    # most individual trades are probably farms) and a tighter, faster exit
    # so no single one does much damage either way.
    "6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy": {
        "tp1_multiple": 1.3, "tp1_sell_fraction": 0.6, "tp2_multiple": 1.8,
        "sl_pct": 0.20, "max_hold_seconds": 300,
        "buy_size_sol": 0.03, "max_concurrent": 3,
    },
}

# Wallets to watch for OG-token buys. Format: {"label": "address"}.
WATCHED_WALLETS = {
    "seven_pm_guy": "2YBHj8kf7AMgwhMKfMUxsyBk9UuX87FUq7UFFQDL9Atq",
    "meme_detective": "6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy",
}

# Wallet-hopping: if a watched wallet goes quiet for this long, check its
# recent outgoing SOL transfers for a likely successor wallet and auto-add it
WALLET_QUIET_DAYS_BEFORE_SUCCESSOR_CHECK = 4
SUCCESSOR_MIN_TRANSFER_SOL = 0.05  # ignore dust transfers when guessing the successor

# ---------------------------------------------------------------------------
# DISCOVERY — passively watch ALL new launches (not just ones being traded)
# for wallets that keep showing up as early buyers on tokens that go on to
# pump. This is separate from Strategy A (which actually buys); discovery
# never trades, it only watches and reports. When a wallet reappears as an
# early buyer on enough separate winners, you get a Telegram alert with the
# address so you can review and decide whether to add it to WATCHED_WALLETS
# yourself — nothing is auto-added.
# ---------------------------------------------------------------------------
DISCOVERY_ENABLED = True

MAX_CONCURRENT_DISCOVERY_WATCHES = 40  # cap on how many tokens we watch at once, purely to bound resource/subscription usage — this is a sample, not full coverage, and that's fine
DISCOVERY_EARLY_BUYER_WINDOW_SECONDS = 30  # a buy counts as "early" if it lands within this long of the token's creation
DISCOVERY_WINDOW_SECONDS = 600         # how long we watch a token before judging it a winner or not
DISCOVERY_SUCCESS_MULTIPLE = 3.0       # a token counts as a "winner" if price is at least this many times its launch price by the end of the window
DISCOVERY_MIN_APPEARANCES = 2          # a wallet needs to show up as an early buyer on at least this many separate winners before we alert you about it

# ---------------------------------------------------------------------------
# Display / bankroll
# ---------------------------------------------------------------------------
# A starting paper bankroll purely for display purposes — running balance
# shown in Telegram alerts and the daily summary is STARTING_BALANCE_SOL +
# sum of all closed trades' pnl_sol. Real trades never touch this, it's just
# so the numbers read like a real account instead of isolated trade tickets.
STARTING_BALANCE_SOL = 5.0

SHOW_USD = True  # show a $ estimate alongside SOL amounts, using a cached SOL/USD price
SOL_PRICE_REFRESH_SECONDS = 300  # how often to refresh the cached SOL/USD price

# ---------------------------------------------------------------------------
# Persistence / reporting
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "paper_sniper.db")
DAILY_SUMMARY_HOUR_UTC = 23  # send one rollup message per day at this UTC hour
