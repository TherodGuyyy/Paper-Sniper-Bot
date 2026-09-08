"""
This is where the actual paper trading happens. Key idea, worth restating:
when we 'buy', we don't just take the price at detection time — we wait the
configured simulated latency, then fill against whatever the REAL reserve
state has become by then (from real trades that happened in that window).
That's what makes the P&L numbers mean something instead of being fantasy
best-case numbers.
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

# mint -> in-memory open position record (mirrors the DB row, kept here for
# fast per-tick exit checks without a DB read on every trade event)
_open_positions: dict[str, dict] = {}


def update_reserve_cache(mint: str, v_sol: float, v_tokens: float):
    if v_sol and v_tokens:
        _reserve_cache[mint] = (v_sol, v_tokens)


def get_latest_reserves(mint: str):
    return _reserve_cache.get(mint)


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
        strategy="launch",
        mint=mint,
        entry_price=fill.effective_price,
        entry_sol_in=config.LAUNCH_BUY_SIZE_SOL,
        entry_tokens=fill.tokens_or_sol_received,
        entry_price_impact_pct=fill.price_impact_pct,
        entry_fee_sol=fill.fee_paid_sol,
        priority_fee_sol=priority_fee,
        meta_json=str({"name": new_token.get("name"), "symbol": new_token.get("symbol"), "latency_ms": latency_ms}),
    )
    _open_positions[mint] = {
        "id": trade_id,
        "strategy": "launch",
        "entry_time": time.time(),
        "entry_price": fill.effective_price,
        "entry_tokens": fill.tokens_or_sol_received,
        "entry_sol_in": config.LAUNCH_BUY_SIZE_SOL,
        "priority_fee_sol": priority_fee,
        "tp": config.TAKE_PROFIT_PCT,
        "sl": config.STOP_LOSS_PCT,
        "max_hold": config.MAX_HOLD_SECONDS,
    }
    log.info(f"[OPEN launch] {mint} entry_price={fill.effective_price:.10f} impact={fill.price_impact_pct:.1%}")
    await telegram_sender.send_trade_open_alert("launch", mint, fill.effective_price, config.LAUNCH_BUY_SIZE_SOL)
    return trade_id


async def attempt_og_snipe(mint: str, v_sol: float, v_tokens: float, watched_wallet_label: str):
    if mint in _open_positions:
        return  # already in a position on this mint

    update_reserve_cache(mint, v_sol, v_tokens)
    try:
        fill = curve_math.simulate_buy(v_sol, v_tokens, config.OG_BUY_SIZE_SOL, config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        db.log_missed("og_wallet", mint, f"buy sim error: {e}")
        return

    priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    trade_id = db.open_trade(
        strategy="og_wallet",
        mint=mint,
        entry_price=fill.effective_price,
        entry_sol_in=config.OG_BUY_SIZE_SOL,
        entry_tokens=fill.tokens_or_sol_received,
        entry_price_impact_pct=fill.price_impact_pct,
        entry_fee_sol=fill.fee_paid_sol,
        priority_fee_sol=priority_fee,
        meta_json=str({"triggered_by": watched_wallet_label}),
    )
    _open_positions[mint] = {
        "id": trade_id,
        "strategy": "og_wallet",
        "entry_time": time.time(),
        "entry_price": fill.effective_price,
        "entry_tokens": fill.tokens_or_sol_received,
        "entry_sol_in": config.OG_BUY_SIZE_SOL,
        "priority_fee_sol": priority_fee,
        "tp": config.OG_TAKE_PROFIT_PCT,
        "sl": config.OG_STOP_LOSS_PCT,
        "max_hold": config.OG_MAX_HOLD_SECONDS,
    }
    log.info(f"[OPEN og_wallet] {mint} triggered_by={watched_wallet_label} entry_price={fill.effective_price:.10f}")
    await telegram_sender.send_trade_open_alert("og_wallet", mint, fill.effective_price, config.OG_BUY_SIZE_SOL)
    return trade_id


async def _close(mint: str, pos: dict, v_sol: float, v_tokens: float, reason: str, ppclient=None):
    try:
        fill = curve_math.simulate_sell(v_sol, v_tokens, pos["entry_tokens"], config.PUMPFUN_FEE_PCT)
    except ValueError as e:
        log.warning(f"close sim failed for {mint}: {e}")
        return

    exit_priority_fee = random.uniform(config.PRIORITY_FEE_SOL_MIN, config.PRIORITY_FEE_SOL_MAX)
    pnl_sol = (fill.tokens_or_sol_received - exit_priority_fee) - pos["entry_sol_in"] - pos["priority_fee_sol"]
    pnl_pct = pnl_sol / pos["entry_sol_in"]

    db.close_trade(
        pos["id"], fill.effective_price, fill.tokens_or_sol_received, reason, fill.fee_paid_sol, pnl_sol, pnl_pct
    )
    log.info(f"[CLOSE {pos['strategy']}] {mint} reason={reason} pnl={pnl_sol:+.4f} SOL ({pnl_pct:+.1%})")
    await telegram_sender.send_trade_close_alert(pos["strategy"], mint, reason, pnl_sol, pnl_pct)
    del _open_positions[mint]
    if ppclient:
        await ppclient.unsubscribe_mint_trades(mint)
    return pnl_sol, pnl_pct, reason


async def on_trade_event(trade: dict, ppclient):
    """Call this on every real trade event received (for mints we've
    subscribed to). Updates reserve cache and checks exit conditions for any
    open position on that mint."""
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
    change_pct = (current_price - pos["entry_price"]) / pos["entry_price"]

    if change_pct >= pos["tp"]:
        await _close(mint, pos, v_sol, v_tokens, "take_profit", ppclient)
    elif change_pct <= -pos["sl"]:
        await _close(mint, pos, v_sol, v_tokens, "stop_loss", ppclient)


async def sweep_time_exits(ppclient):
    """Background loop — catches force-exits for positions that haven't hit
    TP/SL but have gone over their max hold time, even if no new trade event
    has arrived to trigger a check."""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for mint in list(_open_positions.keys()):
            pos = _open_positions[mint]
            if now - pos["entry_time"] >= pos["max_hold"]:
                reserves = get_latest_reserves(mint)
                if reserves:
                    await _close(mint, pos, reserves[0], reserves[1], "time_exit", ppclient)
