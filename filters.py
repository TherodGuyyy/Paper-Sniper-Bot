"""
Entry filters for Strategy A. Kept deliberately simple for v1 — this is the
lever you'll actually tune over time, not the speed/latency side. Every
filter function returns (passed: bool, reason: str) so rejections are always
logged with a real cause, never silently dropped.

Speed/accuracy tradeoff: FILTER_REQUIRE_CREATOR_HISTORY_CHECK does a live
Helius call per launch, which adds real latency on top of
SIMULATED_LATENCY_MS. That's intentional — this file's whole job is to make
that tradeoff visible and tunable, not hide it.
"""

import logging
import time

import httpx

import config

log = logging.getLogger("filters")

HELIUS_BASE = "https://api.helius.xyz/v0"


def basic_launch_filters(new_token: dict) -> tuple[bool, str]:
    if not new_token.get("mint"):
        return False, "missing mint"
    if (new_token.get("initial_buy_sol") or 0) < config.FILTER_MIN_INITIAL_BUY_SOL:
        return False, f"initial buy {new_token.get('initial_buy_sol')} SOL below minimum"

    creator_tokens = new_token.get("initial_buy_tokens") or 0
    creator_pct = creator_tokens / config.PUMPFUN_TOTAL_SUPPLY
    if creator_pct > config.FILTER_MAX_CREATOR_SUPPLY_PCT:
        return False, f"creator holds {creator_pct:.1%} of supply from opening buy alone, over {config.FILTER_MAX_CREATOR_SUPPLY_PCT:.0%} limit"

    return True, "ok"


async def creator_history_filter(creator_wallet: str) -> tuple[bool, str]:
    """
    Best-effort check of how many prior tokens this creator wallet has
    launched via pump.fun. Uses Helius's enhanced transactions endpoint to
    look for prior 'create' instructions from this wallet.

    NOTE: this is a v1 heuristic, not a verified rug-rate calculation — it
    tells you "serial launcher, yes/no", not "serial launcher whose tokens
    actually rugged". Treat FILTER_MAX_CREATOR_PRIOR_TOKENS as a blunt
    spam/noise filter, and revisit once you have real logged outcomes.
    """
    if not config.HELIUS_API_KEY:
        return True, "no HELIUS_API_KEY set, skipping creator history check"

    url = f"{HELIUS_BASE}/addresses/{creator_wallet}/transactions"
    params = {"api-key": config.HELIUS_API_KEY, "limit": 50}
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            txs = resp.json()
    except Exception as e:
        log.warning(f"creator_history_filter Helius call failed for {creator_wallet}: {e}")
        return True, f"helius call failed ({e}), passing by default"

    prior_creates = 0
    for tx in txs:
        # Enhanced tx 'type' field varies by Helius parser version — this is
        # a best-effort match, verify against a real response and adjust.
        desc = (tx.get("type") or "") + " " + (tx.get("source") or "")
        if "PUMP" in desc.upper() and "CREATE" in desc.upper():
            prior_creates += 1

    if prior_creates > config.FILTER_MAX_CREATOR_PRIOR_TOKENS:
        return False, f"creator has {prior_creates} prior launches (over limit)"
    return True, f"creator has {prior_creates} prior launches (ok)"


async def run_all_filters(new_token: dict) -> tuple[bool, str]:
    ok, reason = basic_launch_filters(new_token)
    if not ok:
        return False, reason

    if config.FILTER_REQUIRE_CREATOR_HISTORY_CHECK and new_token.get("creator"):
        ok, reason = await creator_history_filter(new_token["creator"])
        if not ok:
            return False, reason

    return True, "passed all filters"
