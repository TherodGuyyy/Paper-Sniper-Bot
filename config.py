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

# ---------------------------------------------------------------------------
# POSITION SIZING (Sep 2026) — was a flat SOL amount per wallet regardless of
# balance. Switched to a % of current balance so size scales with account
# growth/drawdown automatically instead of needing manual updates.
# "fixed" keeps the old behavior (OG_BUY_SIZE_SOL / per-wallet buy_size_sol)
# if you ever want to go back to it.
# ---------------------------------------------------------------------------
OG_BUY_SIZE_MODE = "pct_of_balance"  # "pct_of_balance" or "fixed"
OG_BUY_SIZE_PCT = 0.05  # default 5% of current balance, used if a wallet has no buy_size_pct override below
OG_BUY_SIZE_MIN_SOL = 0.01  # floor — never size a trade below this even if balance is badly drawn down
OG_BUY_SIZE_MAX_SOL = 1.0   # ceiling — caps a single trade's size even if balance grows a lot; tune as balance grows

OG_BUY_SIZE_SOL = 0.03  # only used if OG_BUY_SIZE_MODE = "fixed"

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

# Cap on how many times we'll paper-buy the SAME mint from the SAME watched
# wallet. Some wallets (meme_detective) buy into one token multiple times —
# a single buy is often too small a signal to act on alone, but letting the
# bot re-enter unlimited times on one token turns one wallet's habit into
# unbounded concentrated exposure. 2 = act on repeat conviction, cap the risk.
# Per-wallet override key: "max_buys_per_token".
OG_MAX_BUYS_PER_TOKEN = 2

# ---------------------------------------------------------------------------
# EXIT SANITY GUARD (Sep 2026) — a real incident: a trade closed for +5854%
# pnl on a $3.54 position, seconds after opening. Root cause: the exit path
# trusts whatever v_sol/v_tokens the latest incoming trade event reports,
# with no check at all (the BUY path already has MAX_SLIPPAGE_PCT for this
# exact reason — the SELL/exit path never got the same guard). Pump.fun
# tokens right at their bonding-curve migration boundary can emit one
# erratic final reserve reading, and the bot was taking that completely at
# face value. A single trade tick implying a multiple beyond
# tp2_multiple * this factor is treated as probably-bad data — skipped
# (not executed, not counted toward peak tracking), logged, and
# re-evaluated on the next tick instead.
# ---------------------------------------------------------------------------
MAX_EXIT_MULTIPLE_SANITY_FACTOR = 3.0

# A position whose price reading fails the sanity check above N consecutive
# times (e.g. mint got mismatched to a stablecoin/wrong swap leg upstream,
# and will NEVER look plausible) used to retry forever, permanently locking
# a concurrent-position slot. After this many consecutive suspect readings,
# force-close it anyway at the last suspect price, flagged so the P&L on
# that trade is understood to be unreliable, rather than leak the slot.
MAX_CONSECUTIVE_SUSPECT_RETRIES = 6

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
        "buy_size_pct": 0.065, "max_concurrent": 2,  # was a flat 0.08 SOL — 6.5% keeps roughly the same ratio vs meme_detective below, now scales with balance
    },
    # Meme Detective — trades daily, farms a lot (many low-quality buys),
    # occasionally shills a real mover on Twitter. Decided against building
    # a farm-vs-real classifier for him (tried something similar before,
    # didn't pan out) — accepting the farming as a cost of doing business
    # and leaning on fast execution instead: smaller size per trade (since
    # most individual trades are probably farms) and a tighter, faster exit
    # so no single one does much damage either way.
    # Sep 2026 update: most of his buys are still farms and should still exit
    # fast, but he occasionally hits something real and rides it far beyond a
    # 1.8x fantasy exit — the old setup fully closed at TP2 no matter what,
    # so those runners got capped at ~1.8x same as every farm. New shape:
    # TP1 locks in half early, TP2 banks most of what's left as a solid win,
    # and a small runner tail is kept open with a trailing stop (25% off its
    # peak) instead of a fixed target — so a farm still dies fast, but a real
    # mover gets room to actually run.
    "6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy": {
        "tp1_multiple": 1.5, "tp1_sell_fraction": 0.5,
        "tp2_multiple": 2.1, "tp2_sell_fraction": 0.7,
        "runner_trail_pct": 0.25,
        "sl_pct": 0.30, "max_hold_seconds": 600,
        # Sep 2026: bumped 0.025 -> 0.125 (2.5% -> 12.5% of balance, ~$2.5 on
        # a $20 balance) — small trades were getting shredded by fixed-cost
        # priority fees (a $1.15 trade eats ~35% of itself in fees alone,
        # same fee regardless of size). NOTE: 12.5% x max_concurrent=3 means
        # up to ~37.5% of balance can be in Meme Detective trades at once —
        # intentional per your ask, but worth watching balance closely for
        # the first few days at this size.
        "buy_size_pct": 0.125, "max_concurrent": 3,
        # Sep 2026: copy his own sell instead of relying on TP/SL. On these
        # low-cap tokens HIS sell is often the dump — our stop-loss checks
        # on a delay (trade-tick or 5s sweep) and can fill well past -30%
        # by the time it reacts. If he's flat, we should be flat too.
        "copy_wallet_sell_exit": True,
    },
}

# Wallets to watch for OG-token buys. Format: {"label": "address"}.
WATCHED_WALLETS = {
    "seven_pm_guy": "2YBHj8kf7AMgwhMKfMUxsyBk9UuX87FUq7UFFQDL9Atq",
    "meme_detective": "6qudAN2kV8mtCcYJxb5QQ6Vr15itdHHdeVbYm99NKMhy",
}

# ---------------------------------------------------------------------------
# STRATEGY B, RAYDIUM/JUPITER COVERAGE (Sep 2026) — PumpPortal's account-trade
# feed only sees trades that touch the pump.fun/PumpSwap program. Once a
# watched wallet's token has migrated to Raydium (or they buy anything
# through Jupiter routing), PumpPortal never fires for it — confirmed via a
# real missed trade (Meme Detective buying PFBABY on Raydium, invisible to
# the bot). This closes that gap with a Helius webhook subscribed to the
# watched wallets directly, which sees ALL their on-chain swaps regardless
# of venue. Set up required on Helius's side (helius.dev dashboard ->
# Webhooks): create an "Enhanced" webhook, type SWAP, account addresses =
# the two WATCHED_WALLETS values above, webhook URL =
# https://<your-render-url>/helius-webhook, and set the Authorization header
# below to a random string you generate yourself (also paste that same
# string into HELIUS_WEBHOOK_SECRET as a Render env var).
# ---------------------------------------------------------------------------
HELIUS_WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET", "")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Modeled fee+slippage for a Raydium/Jupiter fill — we don't get a real
# quote for these paper trades, just the watched wallet's own executed
# price, so this pads entry cost a bit to stay conservative. VERIFY against
# a few real Raydium receipts if you want to tighten this.
RAYDIUM_FEE_PCT = 0.006

# How often to poll Jupiter for current price on open Raydium-sourced
# positions, to check TP1/TP2/stop-loss/max-hold. No trade-event stream
# exists for these like there is for pump.fun mints, so this has to poll.
RAYDIUM_PRICE_POLL_SECONDS = 8

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

# ---------------------------------------------------------------------------
# TRUE-PEAK TRACKING (Sep 2026) — the old "peak while open" number stops the
# moment the bot sells, so a token that keeps running after we're out (or
# that the bot got stopped out of right before a big move) looked far
# weaker than it really was. Now, from the moment a position OPENS, the bot
# keeps watching that token's price for this many seconds no matter what
# the position does (sold, stopped out, still open) and records the
# highest price seen in that window as the token's real peak. Reported in
# Telegram when the window ends, saved on the trade (window_peak_multiple),
# and summarized by the /peaks command. This is measuring only — it never
# changes when the bot buys or sells.
# 600 = 10 minutes after entry. Raise it (e.g. 1800 = 30 min) to see how
# far his slower runners really go.
# ---------------------------------------------------------------------------
PEAK_WINDOW_SECONDS = 600

# Protection against a single garbage price reading (e.g. a glitchy tick
# right at a pump.fun -> Raydium migration) inflating a peak: one reading
# more than this many times higher than the last accepted one is ignored.
# If 3 readings in a row all agree on the new higher level, it's treated as
# a real move and accepted. 4.0 = "ignore a sudden 4x+ jump in a single
# reading unless it keeps showing up".
PEAK_WINDOW_MAX_TICK_JUMP = 4.0

SHOW_USD = True  # show a $ estimate alongside SOL amounts, using a cached SOL/USD price
SOL_PRICE_REFRESH_SECONDS = 300  # how often to refresh the cached SOL/USD price

# ---------------------------------------------------------------------------
# Persistence / reporting
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "paper_sniper.db")
DAILY_SUMMARY_HOUR_UTC = 23  # send one rollup message per day at this UTC hour
