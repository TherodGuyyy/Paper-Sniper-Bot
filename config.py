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
LAUNCH_SNIPE_ENABLED = True

# Position sizing (paper — no real SOL moves, but must be realistic to size
# slippage/fees correctly)
LAUNCH_BUY_SIZE_SOL = 0.05

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
FILTER_MIN_INITIAL_BUY_SOL = 0.5     # dev's own opening buy, too small often = no conviction/instant abandon
FILTER_MAX_CREATOR_PRIOR_TOKENS = 15  # skip serial launchers with a huge past token count (serial ruggers / spam)
FILTER_REQUIRE_CREATOR_HISTORY_CHECK = True  # set False if Helius calls are too slow/unreliable and you want speed over filtering

# Exit logic (Strategy A)
TAKE_PROFIT_PCT = 0.80     # close at +80%
STOP_LOSS_PCT = 0.35       # close at -35%
MAX_HOLD_SECONDS = 900     # force-exit after 15 minutes regardless of P&L — most real moves on new launches happen fast or not at all

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

# Exit logic (Strategy B) — OG revival plays tend to run longer than fresh
# launches, so looser/slower exits than Strategy A
OG_TAKE_PROFIT_PCT = 1.00
OG_STOP_LOSS_PCT = 0.40
OG_MAX_HOLD_SECONDS = 3600

# Wallets to watch for OG-token buys. Starts empty on purpose — add wallets
# you've identified (see smart-wallet-finder project). Format: {"label": "addr"}
WATCHED_WALLETS = {
    # "example_label": "2YBHj8kf7AMgwhMKfMUxsyBk9UuX87FUq7UFFQDL9Atq",
}

# Wallet-hopping: if a watched wallet goes quiet for this long, check its
# recent outgoing SOL transfers for a likely successor wallet and auto-add it
WALLET_QUIET_DAYS_BEFORE_SUCCESSOR_CHECK = 4
SUCCESSOR_MIN_TRANSFER_SOL = 0.05  # ignore dust transfers when guessing the successor

# ---------------------------------------------------------------------------
# Persistence / reporting
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "paper_sniper.db")
DAILY_SUMMARY_HOUR_UTC = 23  # send one rollup message per day at this UTC hour
