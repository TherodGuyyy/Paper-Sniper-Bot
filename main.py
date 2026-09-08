import asyncio
import logging
from datetime import datetime, timezone

import config
import db
import filters
import position_manager
import telegram_sender
import wallet_tracker
from pumpportal_client import PumpPortalClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("main")


async def on_new_token(new_token: dict):
    wallet_tracker.record_new_token_seen(new_token["mint"])

    if not config.LAUNCH_SNIPE_ENABLED:
        return
    ok, reason = await filters.run_all_filters(new_token)
    if not ok:
        db.log_missed("launch", new_token["mint"], f"filtered: {reason}")
        return
    asyncio.create_task(position_manager.attempt_launch_snipe(new_token, ppclient))


async def on_token_trade(trade: dict):
    await position_manager.on_trade_event(trade, ppclient)


async def on_account_trade(trade: dict):
    if not config.OG_WALLET_SNIPE_ENABLED:
        return
    wallet = trade["trader"]
    db.touch_watchlist_wallet(wallet)

    label = next((lbl for lbl, addr in config.WATCHED_WALLETS.items() if addr == wallet), wallet[:8])

    if trade.get("tx_type") != "buy":
        return
    mint = trade["mint"]
    dormant, reason = wallet_tracker.is_dormant_enough(mint)
    if not dormant:
        log.info(f"[og_wallet skip] {wallet[:8]} bought {mint}: {reason}")
        return
    if not trade.get("v_sol") or not trade.get("v_tokens"):
        log.info(f"[og_wallet skip] {mint}: no reserve data on this trade event")
        return
    await position_manager.attempt_og_snipe(mint, trade["v_sol"], trade["v_tokens"], label)


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
    db.init_db()
    wallet_tracker.load_watchlist_into_config()

    global ppclient
    ppclient = PumpPortalClient(on_new_token, on_token_trade, on_account_trade)

    for label, addr in config.WATCHED_WALLETS.items():
        await ppclient.subscribe_account(addr)
        log.info(f"watching wallet {label}: {addr}")

    await telegram_sender.send(
        "🚀 Paper sniper started.\n"
        f"Launch snipe: {'ON' if config.LAUNCH_SNIPE_ENABLED else 'off'} | "
        f"OG wallet snipe: {'ON' if config.OG_WALLET_SNIPE_ENABLED else 'off'} | "
        f"Watching {len(config.WATCHED_WALLETS)} wallet(s)."
    )

    await asyncio.gather(
        ppclient.run_forever(),
        position_manager.sweep_time_exits(ppclient),
        daily_summary_loop(),
        successor_check_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
