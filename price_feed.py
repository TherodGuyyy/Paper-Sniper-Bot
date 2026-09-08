"""
Free, no-key SOL/USD price via CoinGecko, cached and refreshed periodically
in the background so alert formatting never has to block on a network call.
Falls back to the last known price (or None) if a refresh fails — a stale
price is better than a crashed alert.
"""

import asyncio
import logging

import httpx

import config

log = logging.getLogger("price_feed")

_cached_price: float | None = None


def get_cached_sol_price() -> float | None:
    return _cached_price


def fmt_sol_usd(sol_amount: float) -> str:
    """'0.0500 SOL (~$9.12)' if we have a price, else just the SOL amount."""
    if sol_amount is None:
        return "n/a"
    if not config.SHOW_USD or _cached_price is None:
        return f"{sol_amount:.4f} SOL"
    usd = sol_amount * _cached_price
    return f"{sol_amount:.4f} SOL (~${usd:,.2f})"


async def refresh_loop():
    global _cached_price
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "solana", "vs_currencies": "usd"}
    while True:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                _cached_price = resp.json()["solana"]["usd"]
                log.info(f"SOL/USD refreshed: ${_cached_price}")
        except Exception as e:
            log.warning(f"SOL/USD price refresh failed, keeping last known value ({_cached_price}): {e}")
        await asyncio.sleep(config.SOL_PRICE_REFRESH_SECONDS)
