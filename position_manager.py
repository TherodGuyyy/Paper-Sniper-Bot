"""
This is where the actual paper trading happens. Key idea, worth restating:
when we 'buy', we don't just take the price at detection time — we wait the
configured simulated latency, then fill against whatever the REAL reserve
state has become by then (from real trades that happened in that window).
That's what makes the P&L numbers mean something instead of being fantasy
best-case numbers.

Exit model: multiple-based scale-out. TP1 sells a fraction of the position
at entry_price * TP1_MULTIPLE; the remainder rides toward TP2_MULTIPLE,
protected by a stop-loss (on the remaining position) and a max hold time.
"""

import asyncio
import logging
import random
import time

import config
import curve_math
import db
import price_feed
import telegram_sender

log = logging.getLogger("position_manager")

# mint -> latest known (v_sol, v_tokens) from real trade events
_reserve_cache: dict[str, tuple[float, float]] = {}

# mint -> in-memory open position record
_open_positions: dict[str, dict] = {}

# mint -> set of trader addresses seen buying during the pending latency
# window, used for the fast early-buyer-interest check
_pending_buyers: dict[str, set] = {}


def count_open(strategy: str) -> int:
    return len([p for p in _open_positions.values() if p["strategy"] == strategy])


def count_open_for_wallet(wallet_addr: str) -> int:
    return len([p for p in _open_positions.values() if p.get("triggered_by_wallet") == wallet_addr])


def update_reserve_cache(mint: str, v_sol: float, v_tokens: float):
    if v_sol and v_tokens:
        _reserve_cache[mint] = (v_sol, v_tokens)


def get_latest_reserves(mint: str):
    return _reserve_cache.get(mint)


async def reconcile_open_positions(ppclient):
    """On startup, reload any positions left 'open' in the DB from before a
    restart/redeploy, so they keep getting monitored instead of silently
    orphaned. Everything needed to resume (entry price/tokens, the exit
    rules that were active when it opened) is already persisted."""
    rows = db.get_open_trades()
    for r in rows:
        mint = r["mint"]
        # meta_json is a loose str(dict) (not real JSON, matches how it's
        # written elsewhere in this file) — a raydium-sourced position tags
        # itself this way at open time so a restart can tell the two apart.
        is_raydium = "'source': 'raydium'" in (r["meta_json"] or "")
        _open_positions[mint] = {
            "id": r["id"],
            "strategy": r["strategy"],
            "entry_time": r["entry_time"],
            "entry_price": r["entry_price"],
            "entry_sol_in": r["entry_sol_in"],
            "remaining_tokens": r["remaining_tokens"],
            "entry_tokens_total": r["entry_tokens"],
            "priority_fee_sol": r["priority_fee_sol"],
            "tp1_multiple": r["tp1_multiple"],
            "tp1_sell_fraction": r["tp1_sell_fraction"],
            "tp2_multiple": r["tp2_multiple"],
            "sl_pct": r["sl_pct"],
            "max_hold": r["max_hold_seconds"],
            "tp1_done": bool(r["tp1_done"]),
            "tp2_sell_fraction": r["tp2_sell_fraction"],
            "tp2_done": bool(r["tp2_done"]),
            "runner_trail_pct": r["runner_trail_pct"],
            "realized_pnl_sol": r["realized_pnl_sol"] or 0,
            "triggered_by_wallet": r["triggered_by_wallet"],
            "baseline_reserves": None,  # not persisted — dead-token early exit just won't apply to reconciled positions, max_hold still will
            "price_source": "raydium" if is_raydium else "curve",
            "peak_multiple": 1.0, "peak_price": r["entry_price"],  # peak tracking restarts fresh after a restart — not persisted mid-trade
        }
        if not is_raydium:
            await ppclient.subscribe_mint_trades(mint)
    if rows:
        log.info(f"reconciled {len(rows)} open position(s) from previous run")


async def attempt_launch_snipe(new_token: dict, ppclient):
    mint = new_token["mint"]
    creator = new_token.get("creator")
    update_reserve_cache(mint, new_token.get("v_sol"), new_token.get("v_tokens"))
    await ppclient.subscribe_mint_trades(mint)
    _pending_buyers[mint] = set()

    latency_ms = random.uniform(config.SIMULATED_LATENCY_MS_MIN, config.SIMULATED_LATENCY_MS_MAX)
    await asyncio.sleep(latency_ms / 1000)

    early_buyers = _pending_buyers.pop(mint, set()) - {creator}
    if len(early_buyers) > config.FILTER_BUNDLE_MAX_OTHER_BUYERS:
        db.log_missed("launch", mint, f"suspected bundle: {len(early_buyers)} other buyer(s) landed within the latency window")
        await ppclient.unsubscribe_mint_trades(mint)
        return

    reserves = get_latest_reserves(mint)
    if not reserves:
        db.log_missed("launch", mint, "no reserve data at fill time", meta_json=str(new_token))
        return

    v_sol, v_tokens = reserves
    try:
        fill = curve_math.simulate_buy(v_sol, v_tokens, config.LAUNCH_BUY_SIZE_SOL, config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        db.log_missed("launch", mint, f"buy sim error: {e}")
        return

    if abs(fill.price_impact_pct) > config.MAX_SLIPPAGE_PCT:
        db.log_missed(
            "launch", mint,
            f"price moved {fill.price_impact_pct:.1%} during our {latency_ms:.0f}ms latency, over {config.MAX_SLIPPAGE_PCT:.0%} limit",
        )
        await ppclient.unsubscribe_mint_trades(mint)
        return

    priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    trade_id = db.open_trade(
        strategy="launch", mint=mint, entry_price=fill.effective_price,
        entry_sol_in=config.LAUNCH_BUY_SIZE_SOL, entry_tokens=fill.tokens_or_sol_received,
        entry_price_impact_pct=fill.price_impact_pct, entry_fee_sol=fill.fee_paid_sol,
        priority_fee_sol=priority_fee,
        tp1_multiple=config.TP1_MULTIPLE, tp1_sell_fraction=config.TP1_SELL_FRACTION,
        tp2_multiple=config.TP2_MULTIPLE, sl_pct=config.STOP_LOSS_PCT, max_hold_seconds=config.MAX_HOLD_SECONDS,
        meta_json=str({"name": new_token.get("name"), "symbol": new_token.get("symbol"), "latency_ms": latency_ms}),
    )
    _open_positions[mint] = {
        "id": trade_id, "strategy": "launch", "entry_time": time.time(),
        "entry_price": fill.effective_price, "entry_sol_in": config.LAUNCH_BUY_SIZE_SOL,
        "remaining_tokens": fill.tokens_or_sol_received, "priority_fee_sol": priority_fee,
        "tp1_multiple": config.TP1_MULTIPLE, "tp1_sell_fraction": config.TP1_SELL_FRACTION,
        "tp2_multiple": config.TP2_MULTIPLE, "sl_pct": config.STOP_LOSS_PCT, "max_hold": config.MAX_HOLD_SECONDS,
        "tp1_done": False, "realized_pnl_sol": 0.0, "triggered_by_wallet": None,
        "baseline_reserves": (v_sol, v_tokens),
        "peak_multiple": 1.0, "peak_price": fill.effective_price,
    }
    log.info(f"[OPEN launch] {mint} entry_price={fill.effective_price:.10f} impact={fill.price_impact_pct:.1%}")
    await telegram_sender.send_trade_open_alert("launch", mint, fill.effective_price, config.LAUNCH_BUY_SIZE_SOL)
    return trade_id


def _update_peak(pos: dict, current_price: float):
    multiple = current_price / pos["entry_price"]
    if multiple > pos.get("peak_multiple", 1.0):
        pos["peak_multiple"] = multiple
        pos["peak_price"] = current_price


def _runner_trailing_stop_hit(pos: dict, current_price: float) -> bool:
    """Once TP2 has partially exited a position, the remainder is a
    'runner' — instead of a fixed target, it's protected by a trailing stop
    off its own peak so a real mover gets room to keep going while still
    giving back the gain if it rolls over. Wallets without a
    runner_trail_pct override never enter this path (tp2_done implies old
    full-close-at-TP2 behavior for them)."""
    trail_pct = pos.get("runner_trail_pct")
    peak = pos.get("peak_price")
    if not trail_pct or not peak:
        return False
    drop_from_peak = (peak - current_price) / peak
    return drop_from_peak >= trail_pct


def _is_plausible(pos: dict, multiple: float) -> bool:
    """See MAX_EXIT_MULTIPLE_SANITY_FACTOR in config.py for why this
    exists — a single trade tick implying a move far beyond what this
    position's own TP2 target expects is treated as probably-bad reserve
    data (most commonly right at a pump.fun migration boundary), not a
    real price move, and is skipped rather than acted on or counted."""
    limit = pos["tp2_multiple"] * config.MAX_EXIT_MULTIPLE_SANITY_FACTOR
    return multiple <= limit


def _compute_buy_size_sol(overrides: dict) -> float:
    """Fixed-SOL sizing was blind to account growth/drawdown. This reads
    from overrides, or falls back to the global config default, whichever
    sizing mode is active — same helper used by both the pump.fun and
    Raydium buy paths so sizing behaves identically regardless of venue."""
    if config.OG_BUY_SIZE_MODE == "pct_of_balance":
        pct = overrides.get("buy_size_pct", config.OG_BUY_SIZE_PCT)
        balance = db.get_current_balance_sol()
        size = balance * pct
        return max(config.OG_BUY_SIZE_MIN_SOL, min(size, config.OG_BUY_SIZE_MAX_SOL))
    return overrides.get("buy_size_sol", config.OG_BUY_SIZE_SOL)


async def attempt_og_snipe(mint: str, v_sol: float, v_tokens: float, wallet_addr: str, watched_wallet_label: str):
    if mint in _open_positions:
        return

    overrides = config.WALLET_EXIT_OVERRIDES.get(wallet_addr, {})
    tp1_multiple = overrides.get("tp1_multiple", config.OG_TP1_MULTIPLE)
    tp1_sell_fraction = overrides.get("tp1_sell_fraction", config.OG_TP1_SELL_FRACTION)
    tp2_multiple = overrides.get("tp2_multiple", config.OG_TP2_MULTIPLE)
    sl_pct = overrides.get("sl_pct", config.OG_STOP_LOSS_PCT)
    max_hold_seconds = overrides.get("max_hold_seconds", config.OG_MAX_HOLD_SECONDS)
    tp2_sell_fraction = overrides.get("tp2_sell_fraction")  # None = old full-close-at-TP2 behavior
    runner_trail_pct = overrides.get("runner_trail_pct")
    buy_size_sol = _compute_buy_size_sol(overrides)

    max_buys = overrides.get("max_buys_per_token", config.OG_MAX_BUYS_PER_TOKEN)
    prior_buys = db.get_buy_count_for_wallet_mint(wallet_addr, mint)
    if prior_buys >= max_buys:
        db.log_missed("og_wallet", mint, f"already bought {prior_buys}x from {watched_wallet_label}, at max_buys_per_token={max_buys}")
        return

    update_reserve_cache(mint, v_sol, v_tokens)
    try:
        fill = curve_math.simulate_buy(v_sol, v_tokens, buy_size_sol, config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        db.log_missed("og_wallet", mint, f"buy sim error: {e}")
        return

    priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    trade_id = db.open_trade(
        strategy="og_wallet", mint=mint, entry_price=fill.effective_price,
        entry_sol_in=buy_size_sol, entry_tokens=fill.tokens_or_sol_received,
        entry_price_impact_pct=fill.price_impact_pct, entry_fee_sol=fill.fee_paid_sol,
        priority_fee_sol=priority_fee,
        tp1_multiple=tp1_multiple, tp1_sell_fraction=tp1_sell_fraction,
        tp2_multiple=tp2_multiple, sl_pct=sl_pct, max_hold_seconds=max_hold_seconds,
        triggered_by_wallet=wallet_addr,
        meta_json=str({"triggered_by": watched_wallet_label}),
        tp2_sell_fraction=tp2_sell_fraction, runner_trail_pct=runner_trail_pct,
    )
    _open_positions[mint] = {
        "id": trade_id, "strategy": "og_wallet", "entry_time": time.time(),
        "entry_price": fill.effective_price, "entry_sol_in": buy_size_sol,
        "remaining_tokens": fill.tokens_or_sol_received, "priority_fee_sol": priority_fee,
        "tp1_multiple": tp1_multiple, "tp1_sell_fraction": tp1_sell_fraction,
        "tp2_multiple": tp2_multiple, "sl_pct": sl_pct, "max_hold": max_hold_seconds,
        "tp1_done": False, "realized_pnl_sol": 0.0, "triggered_by_wallet": wallet_addr,
        "baseline_reserves": (v_sol, v_tokens),
        "peak_multiple": 1.0, "peak_price": fill.effective_price,
        "tp2_sell_fraction": tp2_sell_fraction, "tp2_done": False, "runner_trail_pct": runner_trail_pct,
        "entry_tokens_total": fill.tokens_or_sol_received,
    }
    log.info(f"[OPEN og_wallet] {mint} triggered_by={watched_wallet_label} entry_price={fill.effective_price:.10f} size={buy_size_sol} (tp1={tp1_multiple}x tp2={tp2_multiple}x hold={max_hold_seconds}s)")
    await telegram_sender.send_trade_open_alert("og_wallet", mint, fill.effective_price, buy_size_sol)
    return trade_id


async def attempt_og_snipe_raydium(mint: str, entry_price_sol: float, wallet_addr: str, watched_wallet_label: str, source: str = "raydium"):
    """Same idea as attempt_og_snipe, but for a buy detected via the Helius
    webhook (Raydium/Jupiter/etc) instead of PumpPortal. No bonding curve
    exists for these, so entry/exit pricing is done directly off the
    watched wallet's own executed price and later Jupiter price polls
    (see raydium_price_poll_loop), not curve_math."""
    if mint in _open_positions:
        return

    # Defensive backstop: even with helius_webhook.py's fix, never let a
    # known stablecoin/wSOL mint reach this far — a bug upstream, or a
    # payload shape we haven't seen yet, should fail closed here rather than
    # open a "position" in a token that was never actually bought.
    if mint in (config.WSOL_MINT, config.USDC_MINT, config.USDT_MINT):
        log.warning(f"attempt_og_snipe_raydium called with a stablecoin/wSOL mint ({mint}) — refusing, this indicates an upstream parsing bug")
        db.log_missed("og_wallet", mint, "refused: mint is a known stablecoin/wSOL, likely an upstream parsing bug")
        return


    overrides = config.WALLET_EXIT_OVERRIDES.get(wallet_addr, {})
    tp1_multiple = overrides.get("tp1_multiple", config.OG_TP1_MULTIPLE)
    tp1_sell_fraction = overrides.get("tp1_sell_fraction", config.OG_TP1_SELL_FRACTION)
    tp2_multiple = overrides.get("tp2_multiple", config.OG_TP2_MULTIPLE)
    sl_pct = overrides.get("sl_pct", config.OG_STOP_LOSS_PCT)
    max_hold_seconds = overrides.get("max_hold_seconds", config.OG_MAX_HOLD_SECONDS)
    tp2_sell_fraction = overrides.get("tp2_sell_fraction")
    runner_trail_pct = overrides.get("runner_trail_pct")
    buy_size_sol = _compute_buy_size_sol(overrides)

    max_buys = overrides.get("max_buys_per_token", config.OG_MAX_BUYS_PER_TOKEN)
    prior_buys = db.get_buy_count_for_wallet_mint(wallet_addr, mint)
    if prior_buys >= max_buys:
        db.log_missed("og_wallet", mint, f"already bought {prior_buys}x from {watched_wallet_label}, at max_buys_per_token={max_buys}")
        return

    if not entry_price_sol or entry_price_sol <= 0:
        db.log_missed("og_wallet", mint, "raydium: invalid entry price computed from webhook")
        return

    fee_sol = buy_size_sol * config.RAYDIUM_FEE_PCT
    net_sol_for_tokens = buy_size_sol - fee_sol
    tokens_received = net_sol_for_tokens / entry_price_sol
    priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)

    trade_id = db.open_trade(
        strategy="og_wallet", mint=mint, entry_price=entry_price_sol,
        entry_sol_in=buy_size_sol, entry_tokens=tokens_received,
        entry_price_impact_pct=0.0, entry_fee_sol=fee_sol,
        priority_fee_sol=priority_fee,
        tp1_multiple=tp1_multiple, tp1_sell_fraction=tp1_sell_fraction,
        tp2_multiple=tp2_multiple, sl_pct=sl_pct, max_hold_seconds=max_hold_seconds,
        triggered_by_wallet=wallet_addr,
        meta_json=str({"triggered_by": watched_wallet_label, "source": source}),
        tp2_sell_fraction=tp2_sell_fraction, runner_trail_pct=runner_trail_pct,
    )
    _open_positions[mint] = {
        "id": trade_id, "strategy": "og_wallet", "entry_time": time.time(),
        "entry_price": entry_price_sol, "entry_sol_in": buy_size_sol,
        "remaining_tokens": tokens_received, "priority_fee_sol": priority_fee,
        "tp1_multiple": tp1_multiple, "tp1_sell_fraction": tp1_sell_fraction,
        "tp2_multiple": tp2_multiple, "sl_pct": sl_pct, "max_hold": max_hold_seconds,
        "tp1_done": False, "realized_pnl_sol": 0.0, "triggered_by_wallet": wallet_addr,
        "baseline_reserves": None, "price_source": "raydium",
        "peak_multiple": 1.0, "peak_price": entry_price_sol,
        "tp2_sell_fraction": tp2_sell_fraction, "tp2_done": False, "runner_trail_pct": runner_trail_pct,
        "entry_tokens_total": tokens_received,
    }
    log.info(f"[OPEN og_wallet_raydium] {mint} triggered_by={watched_wallet_label} entry_price={entry_price_sol:.10f} size={buy_size_sol} source={source}")
    await telegram_sender.send_trade_open_alert("og_wallet", mint, entry_price_sol, buy_size_sol)
    return trade_id


def _raydium_fill(sol_amount: float, price: float):
    """Mirrors curve_math's Fill shape closely enough for the two exit
    helpers below, without needing bonding-curve reserves."""
    fee = sol_amount * config.RAYDIUM_FEE_PCT if sol_amount > 0 else 0
    return sol_amount - fee, fee


async def _raydium_partial_exit(mint: str, pos: dict, current_price: float):
    sell_tokens = pos["remaining_tokens"] * pos["tp1_sell_fraction"]
    gross_sol_out = sell_tokens * current_price
    realized, fee = _raydium_fill(gross_sol_out, current_price)
    pos["remaining_tokens"] -= sell_tokens
    pos["realized_pnl_sol"] += realized
    pos["tp1_done"] = True
    db.record_partial_exit(pos["id"], pos["remaining_tokens"], realized)
    log.info(f"[PARTIAL TP1 og_wallet_raydium] {mint} sold {pos['tp1_sell_fraction']:.0%} realized={realized:+.4f} SOL")
    await telegram_sender.send_partial_exit_alert(pos["strategy"], mint, pos["tp1_sell_fraction"], realized)


async def _raydium_tp2_partial_exit(mint: str, pos: dict, current_price: float):
    sell_tokens = pos["remaining_tokens"] * pos["tp2_sell_fraction"]
    gross_sol_out = sell_tokens * current_price
    realized, fee = _raydium_fill(gross_sol_out, current_price)
    pos["remaining_tokens"] -= sell_tokens
    pos["realized_pnl_sol"] += realized
    pos["tp2_done"] = True
    db.record_tp2_partial_exit(pos["id"], pos["remaining_tokens"], realized)
    log.info(f"[PARTIAL TP2 og_wallet_raydium] {mint} sold {pos['tp2_sell_fraction']:.0%} of remainder realized={realized:+.4f} SOL — runner riding with trail={pos.get('runner_trail_pct')}")
    await telegram_sender.send_partial_exit_alert(pos["strategy"], mint, pos["tp2_sell_fraction"], realized)


async def _raydium_close(mint: str, pos: dict, current_price: float, reason: str, data_unreliable: bool = False):
    if data_unreliable:
        fraction_remaining = pos["remaining_tokens"] / pos["entry_tokens_total"] if pos.get("entry_tokens_total") else 0
        final_leg_pnl = pos["entry_sol_in"] * fraction_remaining
        exit_price = pos["entry_price"]
        gross_sol_out = final_leg_pnl
        fee = 0.0
    else:
        gross_sol_out = pos["remaining_tokens"] * current_price
        final_leg_pnl, fee = _raydium_fill(gross_sol_out, current_price)
        exit_price = current_price

    total_pnl_sol = pos["realized_pnl_sol"] + final_leg_pnl - pos["entry_sol_in"] - pos["priority_fee_sol"]
    pnl_pct = total_pnl_sol / pos["entry_sol_in"]

    db.close_trade(pos["id"], exit_price, gross_sol_out, reason, fee, total_pnl_sol, pnl_pct,
                    peak_price=pos.get("peak_price"), peak_multiple=pos.get("peak_multiple"))
    log.info(f"[CLOSE og_wallet_raydium] {mint} reason={reason} pnl={total_pnl_sol:+.4f} SOL ({pnl_pct:+.1%}) peak={pos.get('peak_multiple', 1.0):.2f}x" + (" [UNRELIABLE DATA — remainder valued at cost]" if data_unreliable else ""))
    await telegram_sender.send_trade_close_alert(pos["strategy"], mint, reason, total_pnl_sol, pnl_pct, peak_multiple=pos.get("peak_multiple"))

    if pos.get("triggered_by_wallet"):
        db.record_wallet_trade_result(pos["triggered_by_wallet"], "", total_pnl_sol)

    del _open_positions[mint]
    return total_pnl_sol, pnl_pct, reason


async def raydium_price_poll_loop():
    """Standalone exit-checker for Raydium/Jupiter-sourced positions —
    these have no trade-event stream the way pump.fun mints do (that's what
    on_trade_event/sweep_time_exits rely on), so this polls Jupiter instead.
    Runs independently of sweep_time_exits so the existing, already-tested
    pump.fun exit path is untouched."""
    while True:
        await asyncio.sleep(config.RAYDIUM_PRICE_POLL_SECONDS)
        now = time.time()
        raydium_mints = [
            mint for mint, pos in _open_positions.items()
            if pos.get("price_source") == "raydium"
        ]
        if not raydium_mints:
            continue
        prices = await price_feed.get_jupiter_prices_sol(raydium_mints)

        for mint in raydium_mints:
            pos = _open_positions.get(mint)
            if not pos:
                continue  # closed by another path mid-loop

            elapsed = now - pos["entry_time"]
            current_price = prices.get(mint)

            if elapsed >= pos["max_hold"]:
                if current_price:
                    time_exit_multiple = current_price / pos["entry_price"]
                    if not _is_plausible(pos, time_exit_multiple):
                        pos["time_exit_suspect_streak"] = pos.get("time_exit_suspect_streak", 0) + 1
                        if pos["time_exit_suspect_streak"] >= config.MAX_CONSECUTIVE_SUSPECT_RETRIES:
                            log.warning(f"[SUSPECT DATA] {mint} time_exit price implies {time_exit_multiple:.1f}x for {pos['time_exit_suspect_streak']} straight polls — force-closing anyway, this position's data (and likely its mint) looks wrong, verify manually")
                            await _raydium_close(mint, pos, current_price, "stale_data_force_close", data_unreliable=True)
                        else:
                            log.warning(f"[SUSPECT DATA] {mint} time_exit price implies {time_exit_multiple:.1f}x — skipping this cycle, will retry ({pos['time_exit_suspect_streak']}/{config.MAX_CONSECUTIVE_SUSPECT_RETRIES})")
                        continue
                    pos["time_exit_suspect_streak"] = 0
                    await _raydium_close(mint, pos, current_price, "time_exit")
                continue

            if not current_price:
                continue  # not in this cycle's batch response — try again next poll

            multiple = current_price / pos["entry_price"]
            change_pct = multiple - 1.0

            if not _is_plausible(pos, multiple):
                log.warning(f"[SUSPECT DATA] {mint} Jupiter price implies {multiple:.1f}x in one poll tick — likely a bad/thin-liquidity reading, ignoring this cycle")
                continue
            _update_peak(pos, current_price)

            if pos.get("tp2_done"):
                if _runner_trailing_stop_hit(pos, current_price):
                    await _raydium_close(mint, pos, current_price, "trailing_stop_runner")
                elif change_pct <= -pos["sl_pct"]:
                    await _raydium_close(mint, pos, current_price, "stop_loss")
            elif multiple >= pos["tp2_multiple"]:
                if pos.get("tp2_sell_fraction"):
                    await _raydium_tp2_partial_exit(mint, pos, current_price)
                else:
                    await _raydium_close(mint, pos, current_price, "take_profit_2")
            elif not pos["tp1_done"] and multiple >= pos["tp1_multiple"]:
                await _raydium_partial_exit(mint, pos, current_price)
            elif change_pct <= -pos["sl_pct"]:
                await _raydium_close(mint, pos, current_price, "stop_loss")


async def _partial_exit(mint: str, pos: dict, v_sol: float, v_tokens: float, ppclient=None):
    sell_amount = pos["remaining_tokens"] * pos["tp1_sell_fraction"]
    try:
        fill = curve_math.simulate_sell(v_sol, v_tokens, sell_amount, config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        log.warning(f"partial exit sim failed for {mint}: {e}")
        return

    exit_priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    realized = fill.tokens_or_sol_received - exit_priority_fee
    pos["remaining_tokens"] -= sell_amount
    pos["realized_pnl_sol"] += realized
    pos["tp1_done"] = True
    db.record_partial_exit(pos["id"], pos["remaining_tokens"], realized)
    log.info(f"[PARTIAL TP1 {pos['strategy']}] {mint} sold {pos['tp1_sell_fraction']:.0%} realized={realized:+.4f} SOL")
    await telegram_sender.send_partial_exit_alert(pos["strategy"], mint, pos["tp1_sell_fraction"], realized)


async def _tp2_partial_exit(mint: str, pos: dict, v_sol: float, v_tokens: float, ppclient=None):
    """TP2 leg of a runner position — sells tp2_sell_fraction of what's
    left instead of closing entirely; the remainder rides under
    runner_trail_pct from here on (see _runner_trailing_stop_hit)."""
    sell_amount = pos["remaining_tokens"] * pos["tp2_sell_fraction"]
    try:
        fill = curve_math.simulate_sell(v_sol, v_tokens, sell_amount, config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        log.warning(f"tp2 partial exit sim failed for {mint}: {e}")
        return

    exit_priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    realized = fill.tokens_or_sol_received - exit_priority_fee
    pos["remaining_tokens"] -= sell_amount
    pos["realized_pnl_sol"] += realized
    pos["tp2_done"] = True
    db.record_tp2_partial_exit(pos["id"], pos["remaining_tokens"], realized)
    log.info(f"[PARTIAL TP2 {pos['strategy']}] {mint} sold {pos['tp2_sell_fraction']:.0%} of remainder realized={realized:+.4f} SOL — runner riding with trail={pos.get('runner_trail_pct')}")
    await telegram_sender.send_partial_exit_alert(pos["strategy"], mint, pos["tp2_sell_fraction"], realized)


async def _close(mint: str, pos: dict, v_sol: float, v_tokens: float, reason: str, ppclient=None, data_unreliable: bool = False):
    if data_unreliable:
        # Sep 2026 fix: this used to run the sell sim against v_sol/v_tokens
        # even when those exact reserves had just been flagged as
        # implausible by _is_plausible (that's WHY we're force-closing) —
        # producing fabricated multi-thousand-percent "profit" out of data
        # we already knew was garbage. Instead, value the unsold remainder
        # at cost (net-zero contribution) and let the P&L reflect only what
        # was actually realized via earlier partial exits on real data.
        fraction_remaining = pos["remaining_tokens"] / pos["entry_tokens_total"] if pos.get("entry_tokens_total") else 0
        final_leg_pnl = pos["entry_sol_in"] * fraction_remaining
        exit_price = pos["entry_price"]
        exit_sol_out = final_leg_pnl
        exit_fee_sol = 0.0
    else:
        try:
            fill = curve_math.simulate_sell(v_sol, v_tokens, pos["remaining_tokens"], config.PUMPFUN_FEE_PCT)
        except ValueError as e:
            log.warning(f"close sim failed for {mint}: {e}")
            return
        exit_priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
        final_leg_pnl = fill.tokens_or_sol_received - exit_priority_fee
        exit_price = fill.effective_price
        exit_sol_out = fill.tokens_or_sol_received
        exit_fee_sol = fill.fee_paid_sol

    total_pnl_sol = pos["realized_pnl_sol"] + final_leg_pnl - pos["entry_sol_in"] - pos["priority_fee_sol"]
    pnl_pct = total_pnl_sol / pos["entry_sol_in"]

    db.close_trade(
        pos["id"], exit_price, exit_sol_out, reason, exit_fee_sol, total_pnl_sol, pnl_pct,
        peak_price=pos.get("peak_price"), peak_multiple=pos.get("peak_multiple"),
    )
    log.info(f"[CLOSE {pos['strategy']}] {mint} reason={reason} pnl={total_pnl_sol:+.4f} SOL ({pnl_pct:+.1%}) peak={pos.get('peak_multiple', 1.0):.2f}x" + (" [UNRELIABLE DATA — remainder valued at cost]" if data_unreliable else ""))
    await telegram_sender.send_trade_close_alert(pos["strategy"], mint, reason, total_pnl_sol, pnl_pct, peak_multiple=pos.get("peak_multiple"))

    if pos["strategy"] == "og_wallet" and pos.get("triggered_by_wallet"):
        db.record_wallet_trade_result(pos["triggered_by_wallet"], "", total_pnl_sol)

    del _open_positions[mint]
    if ppclient:
        await ppclient.unsubscribe_mint_trades(mint)
    return total_pnl_sol, pnl_pct, reason


async def on_trade_event(trade: dict, ppclient):
    mint = trade["mint"]
    if trade.get("v_sol") and trade.get("v_tokens"):
        update_reserve_cache(mint, trade["v_sol"], trade["v_tokens"])

    if mint in _pending_buyers and trade.get("tx_type") == "buy" and trade.get("trader"):
        _pending_buyers[mint].add(trade["trader"])

    pos = _open_positions.get(mint)
    if not pos:
        return

    v_sol, v_tokens = trade["v_sol"], trade["v_tokens"]
    if not v_sol or not v_tokens:
        return

    current_price = v_sol / v_tokens
    multiple = current_price / pos["entry_price"]
    change_pct = multiple - 1.0

    if not _is_plausible(pos, multiple):
        log.warning(f"[SUSPECT DATA] {mint} trade event implies {multiple:.1f}x in one tick (v_sol={v_sol} v_tokens={v_tokens}) — likely a bad reading near migration, ignoring this tick")
        return
    _update_peak(pos, current_price)

    if pos.get("tp2_done"):
        # Runner phase: no fixed target anymore, protected by the trailing
        # stop (or the ordinary stop-loss, whichever's tighter at this point).
        if _runner_trailing_stop_hit(pos, current_price):
            await _close(mint, pos, v_sol, v_tokens, "trailing_stop_runner", ppclient)
        elif change_pct <= -pos["sl_pct"]:
            await _close(mint, pos, v_sol, v_tokens, "stop_loss", ppclient)
    elif multiple >= pos["tp2_multiple"]:
        if pos.get("tp2_sell_fraction"):
            await _tp2_partial_exit(mint, pos, v_sol, v_tokens, ppclient)
        else:
            await _close(mint, pos, v_sol, v_tokens, "take_profit_2", ppclient)
    elif not pos["tp1_done"] and multiple >= pos["tp1_multiple"]:
        await _partial_exit(mint, pos, v_sol, v_tokens, ppclient)
    elif change_pct <= -pos["sl_pct"]:
        await _close(mint, pos, v_sol, v_tokens, "stop_loss", ppclient)


async def sweep_time_exits(ppclient):
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for mint in list(_open_positions.keys()):
            pos = _open_positions[mint]
            elapsed = now - pos["entry_time"]
            reserves = get_latest_reserves(mint)

            if elapsed >= pos["max_hold"]:
                if reserves:
                    v_sol, v_tokens = reserves
                    multiple = (v_sol / v_tokens) / pos["entry_price"]
                    if not _is_plausible(pos, multiple):
                        pos["time_exit_suspect_streak"] = pos.get("time_exit_suspect_streak", 0) + 1
                        if pos["time_exit_suspect_streak"] >= config.MAX_CONSECUTIVE_SUSPECT_RETRIES:
                            log.warning(f"[SUSPECT DATA] {mint} time_exit reserves imply {multiple:.1f}x for {pos['time_exit_suspect_streak']} straight sweeps — force-closing anyway, this position's data (and likely its mint) looks wrong, verify manually")
                            await _close(mint, pos, v_sol, v_tokens, "stale_data_force_close", ppclient, data_unreliable=True)
                        else:
                            log.warning(f"[SUSPECT DATA] {mint} time_exit reserves imply {multiple:.1f}x — skipping this sweep, will retry ({pos['time_exit_suspect_streak']}/{config.MAX_CONSECUTIVE_SUSPECT_RETRIES})")
                        continue
                    pos["time_exit_suspect_streak"] = 0
                    await _close(mint, pos, v_sol, v_tokens, "time_exit", ppclient)
                continue

            baseline = pos.get("baseline_reserves")
            if (
                baseline
                and elapsed >= config.DEAD_TOKEN_EXIT_SECONDS
                and not pos["tp1_done"]
                and reserves == baseline
            ):
                await _close(mint, pos, reserves[0], reserves[1], "dead_token_exit", ppclient)
