import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import config
import db
import discovery
import filters
import position_manager
import price_feed
import telegram_commands
import telegram_sender
import wallet_tracker
from pumpportal_client import PumpPortalClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("main")


class _HealthHandler(BaseHTTPRequestHandler):
    """Bare health-check endpoint so this can run as a Render free-tier Web
    Service (which requires binding to $PORT) instead of a Background
    Worker (which does not have a confirmed free tier — see README).
    UptimeRobot pings this same way it pings bot_v5, to stop Render from
    spinning it down after 15 min idle. Handles both GET and HEAD — UptimeRobot's
    default HTTP(s) monitor uses HEAD, and Python's http.server returns
    501 Not Implemented for any method without an explicit handler."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"paper sniper alive")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # don't spam Render logs with every health-check hit


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info(f"health check server listening on port {port}")


async def on_new_token(new_token: dict):
    wallet_tracker.record_new_token_seen(new_token["mint"])
    await discovery.start_watch(new_token, ppclient)

    if not config.LAUNCH_SNIPE_ENABLED:
        return
    if position_manager.count_open("launch") >= config.MAX_CONCURRENT_LAUNCH_POSITIONS:
        db.log_missed("launch", new_token["mint"], "at MAX_CONCURRENT_LAUNCH_POSITIONS cap")
        return
    ok, reason = await filters.run_all_filters(new_token)
    if not ok:
        db.log_missed("launch", new_token["mint"], f"filtered: {reason}")
        return
    asyncio.create_task(position_manager.attempt_launch_snipe(new_token, ppclient))


async def on_token_trade(trade: dict):
    await position_manager.on_trade_event(trade, ppclient)
    await discovery.on_trade(trade)


async def on_account_trade(trade: dict):
    if not config.OG_WALLET_SNIPE_ENABLED:
        return
    wallet = trade["trader"]
    db.touch_watchlist_wallet(wallet)

    label = next((lbl for lbl, addr in config.WATCHED_WALLETS.items() if addr == wallet), wallet[:8])

    if trade.get("tx_type") != "buy":
        return
    if position_manager.count_open("og_wallet") >= config.MAX_CONCURRENT_OG_POSITIONS:
        return
    wallet_max_concurrent = config.WALLET_EXIT_OVERRIDES.get(wallet, {}).get("max_concurrent")
    if wallet_max_concurrent is not None and position_manager.count_open_for_wallet(wallet) >= wallet_max_concurrent:
        return
    mint = trade["mint"]
    dormant, reason = wallet_tracker.is_dormant_enough(mint)
    if not dormant:
        log.info(f"[og_wallet skip] {wallet[:8]} bought {mint}: {reason}")
        return
    if not trade.get("v_sol") or not trade.get("v_tokens"):
        log.info(f"[og_wallet skip] {mint}: no reserve data on this trade event")
        return
    await position_manager.attempt_og_snipe(mint, trade["v_sol"], trade["v_tokens"], wallet, label)


async def on_connection_status(reconnect=False, failing=False, error=None):
    if failing:
        await telegram_sender.send(
            f"⚠️ Paper sniper is struggling to connect to PumpPortal (3+ failed attempts). "
            f"Last error: {error}\nStill retrying automatically."
        )
    elif reconnect:
        await telegram_sender.send("🔄 Paper sniper reconnected after a dropped connection.")
    else:
        await telegram_sender.send(
            "✅ Paper sniper is LIVE — connected to PumpPortal and watching launches.\n"
            f"Launch snipe: {'ON' if config.LAUNCH_SNIPE_ENABLED else 'off'} | "
            f"OG wallet snipe: {'ON' if config.OG_WALLET_SNIPE_ENABLED else 'off'} | "
            f"Watching {len(config.WATCHED_WALLETS)} wallet(s)."
        )


async def daily_summary_loop():
    last_sent_date = None
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == config.DAILY_SUMMARY_HOUR_UTC and now.date() != last_sent_date:
            await telegram_sender.send_daily_summary()
            last_sent_date = now.date()
        await asyncio.sleep(60)


async def successor_check_loop():
    while True:
        await asyncio.sleep(6 * 3600)  # check every 6 hours — this hits Helius, no need to run constantly
        await wallet_tracker.check_for_quiet_wallets_and_trace(ppclient)


async def main():
    start_health_server()
    db.init_db()
    wallet_tracker.load_watchlist_into_config()

    global ppclient
    ppclient = PumpPortalClient(on_new_token, on_token_trade, on_account_trade, on_connected=on_connection_status)

    for label, addr in config.WATCHED_WALLETS.items():
        await ppclient.subscribe_account(addr)
        log.info(f"watching wallet {label}: {addr}")

    await position_manager.reconcile_open_positions(ppclient)

    await asyncio.gather(
        ppclient.run_forever(),
        position_manager.sweep_time_exits(ppclient),
        daily_summary_loop(),
        successor_check_loop(),
        price_feed.refresh_loop(),
        telegram_commands.poll_commands(),
        discovery.sweep(ppclient),
    )


if __name__ == "__main__":
    asyncio.run(main())
