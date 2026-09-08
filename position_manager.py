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
import telegram_sender

log = logging.getLogger("position_manager")

# mint -> latest known (v_sol, v_tokens) from real trade events
_reserve_cache: dict[str, tuple[float, float]] = {}

# mint -> in-memory open position record
_open_positions: dict[str, dict] = {}


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
        _open_positions[mint] = {
            "id": r["id"],
            "strategy": r["strategy"],
            "entry_time": r["entry_time"],
            "entry_price": r["entry_price"],
            "entry_sol_in": r["entry_sol_in"],
            "remaining_tokens": r["remaining_tokens"],
            "priority_fee_sol": r["priority_fee_sol"],
            "tp1_multiple": r["tp1_multiple"],
            "tp1_sell_fraction": r["tp1_sell_fraction"],
            "tp2_multiple": r["tp2_multiple"],
            "sl_pct": r["sl_pct"],
            "max_hold": r["max_hold_seconds"],
            "tp1_done": bool(r["tp1_done"]),
            "realized_pnl_sol": r["realized_pnl_sol"] or 0,
            "triggered_by_wallet": r["triggered_by_wallet"],
        }
        await ppclient.subscribe_mint_trades(mint)
    if rows:
        log.info(f"reconciled {len(rows)} open position(s) from previous run")


async def attempt_launch_snipe(new_token: dict, ppclient):
    mint = new_token["mint"]
    update_reserve_cache(mint, new_token.get("v_sol"), new_token.get("v_tokens"))
    await ppclient.subscribe_mint_trades(mint)

    latency_ms = random.uniform(config.SIMULATED_LATENCY_MS_MIN, config.SIMULATED_LATENCY_MS_MAX)
    await asyncio.sleep(latency_ms / 1000)

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
    }
    log.info(f"[OPEN launch] {mint} entry_price={fill.effective_price:.10f} impact={fill.price_impact_pct:.1%}")
    await telegram_sender.send_trade_open_alert("launch", mint, fill.effective_price, config.LAUNCH_BUY_SIZE_SOL)
    return trade_id


async def attempt_og_snipe(mint: str, v_sol: float, v_tokens: float, wallet_addr: str, watched_wallet_label: str):
    if mint in _open_positions:
        return

    update_reserve_cache(mint, v_sol, v_tokens)
    try:
        fill = curve_math.simulate_buy(v_sol, v_tokens, config.OG_BUY_SIZE_SOL, config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        db.log_missed("og_wallet", mint, f"buy sim error: {e}")
        return

    priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    trade_id = db.open_trade(
        strategy="og_wallet", mint=mint, entry_price=fill.effective_price,
        entry_sol_in=config.OG_BUY_SIZE_SOL, entry_tokens=fill.tokens_or_sol_received,
        entry_price_impact_pct=fill.price_impact_pct, entry_fee_sol=fill.fee_paid_sol,
        priority_fee_sol=priority_fee,
        tp1_multiple=config.OG_TP1_MULTIPLE, tp1_sell_fraction=config.OG_TP1_SELL_FRACTION,
        tp2_multiple=config.OG_TP2_MULTIPLE, sl_pct=config.OG_STOP_LOSS_PCT, max_hold_seconds=config.OG_MAX_HOLD_SECONDS,
        triggered_by_wallet=wallet_addr,
        meta_json=str({"triggered_by": watched_wallet_label}),
    )
    _open_positions[mint] = {
        "id": trade_id, "strategy": "og_wallet", "entry_time": time.time(),
        "entry_price": fill.effective_price, "entry_sol_in": config.OG_BUY_SIZE_SOL,
        "remaining_tokens": fill.tokens_or_sol_received, "priority_fee_sol": priority_fee,
        "tp1_multiple": config.OG_TP1_MULTIPLE, "tp1_sell_fraction": config.OG_TP1_SELL_FRACTION,
        "tp2_multiple": config.OG_TP2_MULTIPLE, "sl_pct": config.OG_STOP_LOSS_PCT, "max_hold": config.OG_MAX_HOLD_SECONDS,
        "tp1_done": False, "realized_pnl_sol": 0.0, "triggered_by_wallet": wallet_addr,
    }
    log.info(f"[OPEN og_wallet] {mint} triggered_by={watched_wallet_label} entry_price={fill.effective_price:.10f}")
    await telegram_sender.send_trade_open_alert("og_wallet", mint, fill.effective_price, config.OG_BUY_SIZE_SOL)
    return trade_id


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


async def _close(mint: str, pos: dict, v_sol: float, v_tokens: float, reason: str, ppclient=None):
    try:
        fill = curve_math.simulate_sell(v_sol, v_tokens, pos["remaining_tokens"], config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        log.warning(f"close sim failed for {mint}: {e}")
        return

    exit_priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    final_leg_pnl = fill.tokens_or_sol_received - exit_priority_fee
    total_pnl_sol = pos["realized_pnl_sol"] + final_leg_pnl - pos["entry_sol_in"] - pos["priority_fee_sol"]
    pnl_pct = total_pnl_sol / pos["entry_sol_in"]

    db.close_trade(
        pos["id"], fill.effective_price, fill.tokens_or_sol_received, reason, fill.fee_paid_sol, total_pnl_sol, pnl_pct
    )
    log.info(f"[CLOSE {pos['strategy']}] {mint} reason={reason} pnl={total_pnl_sol:+.4f} SOL ({pnl_pct:+.1%})")
    await telegram_sender.send_trade_close_alert(pos["strategy"], mint, reason, total_pnl_sol, pnl_pct)

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

    pos = _open_positions.get(mint)
    if not pos:
        return

    v_sol, v_tokens = trade["v_sol"], trade["v_tokens"]
    if not v_sol or not v_tokens:
        return

    current_price = v_sol / v_tokens
    multiple = current_price / pos["entry_price"]
    change_pct = multiple - 1.0

    if multiple >= pos["tp2_multiple"]:
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
            if now - pos["entry_time"] >= pos["max_hold"]:
                reserves = get_latest_reserves(mint)
                if reserves:
                    await _close(mint, pos, reserves[0], reserves[1], "time_exit", ppclient)
