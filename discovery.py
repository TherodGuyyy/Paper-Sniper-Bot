"""
Separate from Strategy A/B — this module never places a paper trade. It
just watches. For every new launch (up to MAX_CONCURRENT_DISCOVERY_WATCHES
at a time, since we can't watch literally every pump.fun launch), it tracks
who buys early and whether the token goes on to become a real winner. When
a wallet keeps showing up as an early buyer on separate winners, that's a
real, evidence-based signal worth a look — you get a Telegram alert with the
address and example mints, and decide yourself whether to add it to
WATCHED_WALLETS. Nothing here trades or auto-adds anything.
"""

import asyncio
import logging
import time

import config
import db
import telegram_sender

log = logging.getLogger("discovery")

# mint -> {"creator", "start_time", "baseline_price", "early_buyers": set()}
_active: dict[str, dict] = {}

# mint -> (v_sol, v_tokens), separate from position_manager's cache since
# discovery watches many tokens we never actually trade
_reserves: dict[str, tuple[float, float]] = {}


def active_count() -> int:
    return len(_active)


async def start_watch(new_token: dict, ppclient):
    if not config.DISCOVERY_ENABLED:
        return
    if len(_active) >= config.MAX_CONCURRENT_DISCOVERY_WATCHES:
        return  # sampling, not full coverage — fine to skip some

    mint = new_token["mint"]
    v_sol, v_tokens = new_token.get("v_sol"), new_token.get("v_tokens")
    if not v_sol or not v_tokens:
        return

    baseline_price = v_sol / v_tokens
    _active[mint] = {
        "creator": new_token.get("creator"),
        "start_time": time.time(),
        "baseline_price": baseline_price,
        "early_buyers": set(),
    }
    _reserves[mint] = (v_sol, v_tokens)
    await ppclient.subscribe_mint_trades(mint)


async def on_trade(trade: dict):
    mint = trade.get("mint")
    watch = _active.get(mint)
    if not watch:
        return

    if trade.get("v_sol") and trade.get("v_tokens"):
        _reserves[mint] = (trade["v_sol"], trade["v_tokens"])

    if trade.get("tx_type") == "buy" and trade.get("trader"):
        elapsed = time.time() - watch["start_time"]
        if elapsed <= config.DISCOVERY_EARLY_BUYER_WINDOW_SECONDS and trade["trader"] != watch["creator"]:
            watch["early_buyers"].add(trade["trader"])


async def _evaluate_and_close(mint: str, ppclient):
    watch = _active.pop(mint, None)
    reserves = _reserves.pop(mint, None)
    if not watch or not reserves:
        return

    v_sol, v_tokens = reserves
    current_price = v_sol / v_tokens
    multiple = current_price / watch["baseline_price"] if watch["baseline_price"] else 0

    if multiple >= config.DISCOVERY_SUCCESS_MULTIPLE:
        for wallet in watch["early_buyers"]:
            new_count = db.record_discovery_appearance(wallet, mint)
            log.info(f"[DISCOVERY] {wallet[:8]} early buyer on winner {mint} ({multiple:.1f}x) - appearance #{new_count}")
            if new_count >= config.DISCOVERY_MIN_APPEARANCES:
                row = db.get_discovery_wallet(wallet)
                if row and not row["alerted"]:
                    db.mark_discovery_alerted(wallet)
                    await telegram_sender.send_discovery_alert(wallet, new_count, row["mints_json"])

    await ppclient.unsubscribe_mint_trades(mint)


async def sweep(ppclient):
    """Background loop — closes out discovery watches once their window
    expires, evaluating whether the token became a winner."""
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for mint in list(_active.keys()):
            watch = _active.get(mint)
            if watch and now - watch["start_time"] >= config.DISCOVERY_WINDOW_SECONDS:
                await _evaluate_and_close(mint, ppclient)
