"""
The rest of the bot only ever SENDS to Telegram. This module is the other
direction: it polls Telegram for messages you send TO the bot, and responds
to a small set of commands. Only messages from config.TELEGRAM_CHAT_ID are
ever acted on — anyone else's messages to the bot are silently ignored, so
this is safe even if the bot's username were ever guessed by someone else.

Commands:
  /help                 - list commands
  /balance              - show current paper balance
  /setbalance <amount>  - set paper balance to a specific $ amount (e.g. /setbalance 25)
  /status               - show open position counts
  /reset                - wipe all paper trades and stats back to a clean balance (same as reset.py, but from Telegram)
"""

import asyncio
import logging

import httpx

import config
import db
import price_feed
import telegram_sender

log = logging.getLogger("telegram_commands")


async def _get_updates(client: httpx.AsyncClient, offset: int | None):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    resp = await client.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", [])


async def _handle_command(text: str):
    text = text.strip()
    parts = text.split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/help":
        await telegram_sender.send(
            "Commands:\n"
            "/balance — show current paper balance\n"
            "/setbalance &lt;amount&gt; — set paper balance to a $ amount, e.g. /setbalance 25\n"
            "/status — show open position counts\n"
            "/reset — wipe all trades/stats back to a clean starting balance"
        )

    elif cmd == "/balance":
        bal = db.get_current_balance_sol()
        await telegram_sender.send(f"Balance: {price_feed.fmt_usd(bal)}")

    elif cmd == "/setbalance":
        if len(parts) < 2:
            await telegram_sender.send("Usage: /setbalance 25   (sets balance to $25)")
            return
        try:
            target_usd = float(parts[1].replace("$", "").replace(",", ""))
        except ValueError:
            await telegram_sender.send("Couldn't read that amount — usage: /setbalance 25")
            return

        price = price_feed.get_cached_sol_price()
        if not price:
            await telegram_sender.send("No SOL/USD price cached yet, try again in a few seconds.")
            return

        target_sol = target_usd / price
        current_sol = db.get_current_balance_sol()
        delta = target_sol - current_sol
        db.add_manual_balance_adjustment(delta)
        new_bal = db.get_current_balance_sol()
        await telegram_sender.send(f"Balance set to {price_feed.fmt_usd(new_bal)}")

    elif cmd == "/status":
        import position_manager  # deferred, avoids import-order issues at module load
        n_launch = position_manager.count_open("launch")
        n_og = position_manager.count_open("og_wallet")
        await telegram_sender.send(
            f"Open positions: {n_launch}/{config.MAX_CONCURRENT_LAUNCH_POSITIONS} launch, "
            f"{n_og}/{config.MAX_CONCURRENT_OG_POSITIONS} og_wallet\n"
            f"Balance: {price_feed.fmt_usd(db.get_current_balance_sol())}"
        )

    elif cmd == "/reset":
        with db.get_conn() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM wallet_performance")
        db.clear_manual_balance_adjustment()
        await telegram_sender.send(f"Reset done. Balance back to {price_feed.fmt_usd(config.STARTING_BALANCE_SOL)}")


async def poll_commands():
    offset = None
    async with httpx.AsyncClient() as client:
        # sync past any backlog of messages sent while the bot was offline,
        # so restarting doesn't replay old commands
        try:
            initial = await _get_updates(client, None)
            if initial:
                offset = initial[-1]["update_id"] + 1
        except Exception as e:
            log.warning(f"initial getUpdates sync failed: {e}")

        while True:
            try:
                updates = await _get_updates(client, offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "")
                    if chat_id != str(config.TELEGRAM_CHAT_ID) or not text.startswith("/"):
                        continue
                    await _handle_command(text)
            except Exception as e:
                log.warning(f"command polling error: {e}")
                await asyncio.sleep(5)
