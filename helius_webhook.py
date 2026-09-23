"""
Receives Helius "Enhanced" webhook payloads (real-time on-chain swaps for
the watched wallets, any venue — Raydium, Jupiter, etc) and turns them into
the same shape of "watched wallet bought X" event that on_account_trade
already handles for pump.fun trades. This exists because PumpPortal's
account-trade stream ONLY fires for trades touching the pump.fun/PumpSwap
program — confirmed via a real missed trade (Meme Detective buying a
Raydium-migrated token, invisible to the bot). See config.py for the Helius
dashboard setup this depends on.

We deliberately SKIP any swap whose Helius `source` is pump.fun/PumpSwap —
those are already caught (faster, and with real bonding-curve reserve data)
by the existing PumpPortal path, and acting on both would double-buy.
"""

import logging

import config

log = logging.getLogger("helius_webhook")

_PUMP_SOURCES = {"PUMP_FUN", "PUMP_AMM", "PUMPSWAP", "PUMP_FUN_AMM"}


def _watched_label(addr: str) -> str | None:
    for label, watched_addr in config.WATCHED_WALLETS.items():
        if watched_addr == addr:
            return label
    return None


def _addrs_in_transfer_list(transfers: list, key_a: str, key_b: str) -> set:
    out = set()
    for t in transfers or []:
        for k in (key_a, key_b):
            v = t.get(k)
            if v:
                out.add(v)
    return out


def parse_payload(events: list[dict]) -> list[dict]:
    """Returns a list of candidate buy events:
    {wallet, label, mint, entry_price_sol, sol_in, tx_sig, source}
    One entry per detected watched-wallet BUY swap. Callers still apply
    their own gating (enabled flag, concurrency caps, etc) before acting."""
    out = []
    for tx in events or []:
        try:
            candidate = _parse_one(tx)
        except Exception as e:
            log.warning(f"failed to parse a helius webhook event: {e} | raw={tx}")
            candidate = None
        if candidate:
            out.append(candidate)
    return out


def _parse_one(tx: dict) -> dict | None:
    if (tx.get("type") or "").upper() != "SWAP":
        return None

    source = (tx.get("source") or "").upper()
    if source in _PUMP_SOURCES:
        return None  # already covered by PumpPortal — avoid double-buying

    token_transfers = tx.get("tokenTransfers") or []
    native_transfers = tx.get("nativeTransfers") or []

    involved = {tx.get("feePayer")} | _addrs_in_transfer_list(
        token_transfers, "fromUserAccount", "toUserAccount"
    ) | _addrs_in_transfer_list(native_transfers, "fromUserAccount", "toUserAccount")

    wallet = next((a for a in involved if a and _watched_label(a)), None)
    if not wallet:
        return None
    label = _watched_label(wallet)

    # What token did the wallet RECEIVE that isn't SOL/a stablecoin (i.e.
    # the buy)? Sep 2026 fix: this used to only exclude WSOL/USDC and always
    # took received[0] — a wallet routing through an intermediate hop (e.g.
    # SOL -> USDT -> TARGET on a multi-hop Jupiter route) can show that
    # intermediate token as a transfer INTO the wallet's own ATA too, and it
    # isn't necessarily last in the list, so "first non-excluded transfer"
    # silently grabbed the intermediate hop instead of what was actually
    # bought (confirmed live: a stuck position whose "mint" was literally
    # the USDT mint address). Now: exclude known stable/quote mints, and if
    # more than one candidate still remains, take the LAST — the final
    # output of a route lands in the wallet last — rather than guessing on
    # the first.
    received = [
        t for t in token_transfers
        if t.get("toUserAccount") == wallet
        and t.get("mint") not in (config.WSOL_MINT, config.USDC_MINT, config.USDT_MINT)
    ]
    if not received:
        return None  # this was a sell, or we didn't recognize the buy leg
    if len(received) > 1:
        log.warning(
            f"multiple non-stablecoin tokens received in one swap for {wallet} "
            f"(tx={tx.get('signature')}) — treating the LAST as the actual buy "
            f"(likely a multi-hop route), candidates={[r.get('mint') for r in received]}"
        )
    mint = received[-1]["mint"]
    tokens_received = float(received[0].get("tokenAmount") or 0)
    if tokens_received <= 0:
        return None

    # What did the wallet PAY — native SOL, or USDC?
    sol_paid_lamports = sum(
        float(t.get("amount") or 0)
        for t in native_transfers
        if t.get("fromUserAccount") == wallet
    )
    if sol_paid_lamports > 0:
        sol_in = sol_paid_lamports / 1e9
    else:
        usdc_paid = sum(
            float(t.get("tokenAmount") or 0)
            for t in token_transfers
            if t.get("fromUserAccount") == wallet and t.get("mint") == config.USDC_MINT
        )
        if usdc_paid <= 0:
            return None  # couldn't identify what was paid — skip rather than guess
        import price_feed
        sol_price = price_feed.get_cached_sol_price()
        if not sol_price:
            return None  # no SOL/USD rate cached yet, can't convert — skip this cycle
        sol_in = usdc_paid / sol_price

    entry_price_sol = sol_in / tokens_received
    return {
        "wallet": wallet,
        "label": label,
        "mint": mint,
        "entry_price_sol": entry_price_sol,
        "sol_in": sol_in,
        "tx_sig": tx.get("signature"),
        "source": source,
    }
