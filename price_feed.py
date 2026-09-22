"""
SOL/USD price, cached and refreshed periodically in the background so alert
formatting never has to block on a network call.

Uses Binance's public ticker endpoint as the primary source — CoinGecko's
free API shares rate limits across every app hosted on the same provider's
IPs (Render included), so it 429s constantly in practice. Binance's ticker
endpoint doesn't share that problem for this call volume. Falls back to
CoinGecko if Binance is ever unreachable, and to the last known cached price
if both fail — a stale price is better than a crashed alert.
"""

import asyncio
import logging

import httpx

import config

log = logging.getLogger("price_feed")

_cached_price: float | None = None

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


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


def fmt_usd(sol_amount: float) -> str:
    """'$9.12' — $ as the primary/only unit shown, per preference. Falls
    back to showing SOL (clearly labeled) only in the rare case no price is
    cached yet, since we can't show a $ figure without one."""
    if sol_amount is None:
        return "n/a"
    if _cached_price is None:
        return f"{sol_amount:.4f} SOL (price feed unavailable)"
    usd = sol_amount * _cached_price
    sign = "-" if usd < 0 else ""
    return f"{sign}${abs(usd):,.2f}"


async def _fetch_binance(client: httpx.AsyncClient) -> float | None:
    resp = await client.get(BINANCE_URL, params={"symbol": "SOLUSDT"})
    resp.raise_for_status()
    return float(resp.json()["price"])


async def _fetch_coingecko(client: httpx.AsyncClient) -> float | None:
    resp = await client.get(COINGECKO_URL, params={"ids": "solana", "vs_currencies": "usd"})
    resp.raise_for_status()
    return float(resp.json()["solana"]["usd"])


JUPITER_PRICE_URL = "https://api.jup.ag/price/v2"


async def get_jupiter_price_sol(mint: str) -> float | None:
    """Current price of `mint` in SOL, via Jupiter's price API (USD) divided
    by the cached SOL/USD rate. Used only for Raydium/Jupiter-sourced
    positions that have no pump.fun bonding curve to price against.
    NOTE: verify this endpoint still responds this way once deployed —
    Jupiter has changed this API's path before (v4 -> v2), and if it moves
    again this is the one function to update. On any failure this returns
    None and the caller should skip that check cycle rather than guess."""
    if _cached_price is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(JUPITER_PRICE_URL, params={"ids": mint})
            resp.raise_for_status()
            data = resp.json()
            usd_price = float(data["data"][mint]["price"])
            return usd_price / _cached_price
    except Exception as e:
        log.warning(f"Jupiter price fetch failed for {mint}: {e}")
        return None


async def refresh_loop():
    global _cached_price
    while True:
        sleep_for = config.SOL_PRICE_REFRESH_SECONDS
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                try:
                    _cached_price = await _fetch_binance(client)
                    log.info(f"SOL/USD refreshed via Binance: ${_cached_price}")
                except Exception as e_binance:
                    log.info(f"Binance price fetch failed ({e_binance}), trying CoinGecko fallback")
                    _cached_price = await _fetch_coingecko(client)
                    log.info(f"SOL/USD refreshed via CoinGecko fallback: ${_cached_price}")
        except Exception as e:
            log.warning(f"SOL/USD price refresh failed on both sources, keeping last known value ({_cached_price}): {e}")
            sleep_for = 30  # retry sooner than the normal interval after a full failure
        await asyncio.sleep(sleep_for)
