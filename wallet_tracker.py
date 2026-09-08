"""
Strategy B support: tracks watched wallets, a best-effort dormancy check for
tokens they buy, and the wallet-hopping successor detection you asked for
(trace where a wallet's funds moved after it goes quiet, and start watching
the new wallet automatically).

HONEST LIMITATION (read before trusting the dormancy check):
PumpPortal's websocket only gives us forward-looking data from the moment we
subscribe. We do NOT have a full trade history for every token on the
platform, so we can't truly verify "no trades in the last 6 hours" for an
arbitrary token. What we CAN do reliably is track token AGE, from the global
subscribeNewToken stream we're always connected to (mint -> first-seen
creation time). So for v1, dormancy = "old enough" using real data; the
"quiet" half of the check is a documented gap, not a guess dressed up as one.
If you want the real "quiet" check, the next step is a REST history API
(Bitquery/Solana Tracker) that can answer 'last trade time for this mint'
directly — flagged in bot-projects-overview as a possible follow-up, not
built here.
"""

import logging
import time

import httpx

import config
import db

log = logging.getLogger("wallet_tracker")

HELIUS_BASE = "https://api.helius.xyz/v0"

# mint -> first-seen creation timestamp, populated from every subscribeNewToken event
_mint_creation_time: dict[str, float] = {}


def record_new_token_seen(mint: str):
    if mint not in _mint_creation_time:
        _mint_creation_time[mint] = time.time()


def is_dormant_enough(mint: str) -> tuple[bool, str]:
    created = _mint_creation_time.get(mint)
    if created is None:
        return False, "token creation time unknown (predates this bot's runtime) — cannot verify age, skipping"
    age_hours = (time.time() - created) / 3600
    if age_hours < config.DORMANT_MIN_TOKEN_AGE_HOURS:
        return False, f"token only {age_hours:.1f}h old, below dormancy age threshold"
    return True, f"token is {age_hours:.1f}h old — age check passed (quiet-hours check not implemented, see module docstring)"


def load_watchlist_into_config():
    """Merge any wallets already in the DB (e.g. auto-added successors from a
    previous run) with the hardcoded starting list in config.py."""
    for w in db.get_watchlist():
        config.WATCHED_WALLETS[w["label"] or w["wallet"][:8]] = w["wallet"]
    for label, addr in config.WATCHED_WALLETS.items():
        db.upsert_watchlist(addr, label)


async def find_successor_wallet(old_wallet: str) -> tuple[str | None, str]:
    """Look at old_wallet's recent outgoing SOL transfers and guess which
    destination is a successor wallet: largest transfer, above the dust
    threshold, going to an address we haven't already got on the watchlist.
    This is a heuristic, not a certainty — always sanity-check a suggested
    successor manually before relying on it."""
    if not config.HELIUS_API_KEY:
        return None, "no HELIUS_API_KEY set"

    url = f"{HELIUS_BASE}/addresses/{old_wallet}/transactions"
    params = {"api-key": config.HELIUS_API_KEY, "limit": 30}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            txs = resp.json()
    except Exception as e:
        return None, f"helius call failed: {e}"

    known_addrs = {w["wallet"] for w in db.get_watchlist()}
    best_candidate, best_amount = None, 0.0
    for tx in txs:
        for transfer in tx.get("nativeTransfers", []):
            if transfer.get("fromUserAccount") != old_wallet:
                continue
            amount_sol = (transfer.get("amount") or 0) / 1e9
            to_addr = transfer.get("toUserAccount")
            if amount_sol < config.SUCCESSOR_MIN_TRANSFER_SOL or not to_addr or to_addr in known_addrs:
                continue
            if amount_sol > best_amount:
                best_candidate, best_amount = to_addr, amount_sol

    if best_candidate:
        return best_candidate, f"largest outgoing transfer ({best_amount:.3f} SOL) went here"
    return None, "no qualifying outgoing transfer found in recent history"


async def check_for_quiet_wallets_and_trace(ppclient):
    """Run periodically (see main.py). For any watched wallet that's gone
    quiet past the configured threshold, try to find and auto-add its
    successor wallet."""
    cutoff = time.time() - (config.WALLET_QUIET_DAYS_BEFORE_SUCCESSOR_CHECK * 86400)
    for w in db.get_watchlist():
        if w["last_seen_active"] and w["last_seen_active"] > cutoff:
            continue
        successor, reason = await find_successor_wallet(w["wallet"])
        if successor:
            label = f"successor_of_{w['label'] or w['wallet'][:8]}"
            db.upsert_watchlist(successor, label, successor_of=w["wallet"])
            config.WATCHED_WALLETS[label] = successor
            await ppclient.subscribe_account(successor)
            log.info(f"[SUCCESSOR FOUND] {w['wallet']} -> {successor} ({reason})")
        else:
            log.info(f"[SUCCESSOR CHECK] {w['wallet']} quiet, no successor found yet: {reason}")
