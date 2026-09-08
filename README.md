# Paper Sniper — realistic paper trading for pump.fun launches + OG-wallet plays

## What this actually does

Two strategies running side by side, both fully simulated (no real SOL moves,
no real wallet needed):

- **Launch snipe (Strategy A):** watches every new pump.fun token launch,
  runs it through filters, and if it passes, simulates buying it — but only
  after waiting a realistic latency delay, and filling at whatever the REAL
  price has moved to by then (from real trades that happened in that window).
  Exits on take-profit, stop-loss, or a max hold time.
- **OG-wallet snipe (Strategy B):** watches a list of wallets you specify. If
  one of them buys a token that looks "dormant" (old enough, per real
  creation-time data), it paper-buys that token too. If a watched wallet goes
  quiet for a while, the bot automatically checks its recent transfers for a
  likely successor wallet and starts watching that one too.

Every attempt — filled, missed, opened, closed — gets logged to a local
database and (optionally) sent to Telegram, so after a few days/weeks you
have real numbers instead of a guess.

**Read `position_manager.py` and `filters.py`'s docstrings before trusting
the numbers blindly** — the honest limitations of the simulation (especially
around the OG-wallet dormancy check) are documented right there in the code,
not hidden.

## Before you start — what you need

1. A Telegram bot token + chat ID for alerts. **Use a brand-new bot/account
   for this, separate from your memecoin alert bot and any trading bot**,
   same as your existing setup for the other projects. If you've forgotten
   the steps: message @BotFather on Telegram → `/newbot` → follow the
   prompts → it gives you a token. To get your chat ID, message your new bot
   once, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in
   a browser and read the `chat.id` field.
2. A Helius API key. You already have one from the smart-wallet-finder
   project — you can reuse the same key here, it's just used for read-only
   lookups (creator history, successor wallet tracing).
3. A GitHub account and a Render account (you already have both from your
   other bots).

## Step-by-step setup

### 1. Get the code onto GitHub
1. Create a new, empty repository on GitHub (e.g. `paper-sniper`).
2. Download all the files from this project and upload them into that repo
   (GitHub's web interface has an "upload files" button — drag and drop all
   of them in).

### 2. Deploy it on Render
This needs to run **continuously** (it holds a live websocket connection),
the same way your memecoin alert bot runs — not a scheduled job like the
tips bot.

1. Go to Render → New → Background Worker (not a scheduled Cron Job — this
   process needs to stay running and listening).
2. Connect it to your new GitHub repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python main.py`
5. Under "Environment", add these three secrets (from `.env.example`):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `HELIUS_API_KEY`
6. Deploy.

### 3. Watch it run
You'll get a Telegram message when it starts, one message every time it
opens or closes a paper position, and one daily summary at 23:00 UTC.

### 4. After a few days/weeks — pull real numbers
Render's free tier disks aren't permanent, so periodically download
`paper_sniper.db` from the Render shell (Render dashboard → your service →
Shell tab → you can `cat` or copy the file out) and run:

```
python report.py
```

This gives you a plain-English summary (win rate, total simulated P&L, avg
P&L per trade) and a `trades_export.csv` you can open in Excel/Sheets to look
at every single attempt — including the ones marked `missed`, which are just
as important as the wins/losses, since they tell you how often the latency
assumption alone would have killed the trade before it even started.

## What to actually look for in the results

- **Missed-trade rate** (status='missed' in the export) — if this is very
  high, it means `MAX_SLIPPAGE_PCT` and your latency assumptions
  (`SIMULATED_LATENCY_MS_MIN/MAX`) are telling you that a solo, non-Jito-tier
  setup mostly can't compete on speed for fresh launches. That's a real,
  useful finding, not a bug.
- **Strategy A vs Strategy B win rate and P&L separately** — the daily
  summary and the CSV both break this out. Don't judge "is sniping
  profitable" as one number; these are two different bets with different
  risk profiles.
- **Whether OG-wallet successor auto-detection actually fires** — check the
  logs (Render dashboard → Logs) for `[SUCCESSOR FOUND]` messages. If your
  watched wallets never trigger this, the wallet-hopping trace isn't proving
  itself yet — worth watching a specific case manually to sanity-check it.

## Tuning

Every threshold that matters is in `config.py` with a comment explaining
what it controls — nothing else in the code should need editing to change
behavior. Reasonable first moves after a week of data:

- If missed-rate is very high: either accept that (real information) or
  tighten `FILTER_MIN_INITIAL_BUY_SOL` to only attempt higher-conviction
  launches where you're more likely to still get a fill worth having.
- If launch-strategy P&L is consistently negative but not from missed
  trades: the filters aren't finding real signal yet — this is where your
  smart-wallet-finder-style creator scoring would plug in next.
- Add real wallets to `WATCHED_WALLETS` in `config.py` once you have
  specific ones in mind (format is shown as a commented-out example).

## Known v1 limitations (on purpose, not oversights)

- **OG-wallet dormancy check** only verifies token *age* reliably (from live
  data). The "quiet for N hours" half of the check isn't independently
  verifiable without a full trade-history API (see `wallet_tracker.py`
  docstring) — flagged, not silently faked.
- **Field names from PumpPortal** (`vSolInBondingCurve` etc.) are from
  published docs, not a live-confirmed response. `main.py`'s logs will show
  `[RAW newToken sample]` / `[RAW trade sample]` lines on startup — check
  the first few against `pumpportal_client.py`'s parsing once it's live, and
  report back if anything looks off (e.g. all-None values), same as we've
  done for every other bot's uncertain schema.
- **Creator-history filter** is a blunt "how many prior launches" count, not
  a real rug-rate — a first pass, not a final answer.
