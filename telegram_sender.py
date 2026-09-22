import logging
import time

import httpx

import config
import db
import price_feed

log = logging.getLogger("telegram")


async def send(text: str):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info(f"[telegram disabled] {text}")
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                log.warning(f"telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.warning(f"telegram send error: {e}")


def _bal_line():
    bal = db.get_current_balance_sol()
    return f"Balance: {price_feed.fmt_usd(bal)}"


def _open_line():
    import position_manager  # deferred: position_manager imports this module too, so importing it at module load time here would be circular
    n_launch = position_manager.count_open("launch")
    n_og = position_manager.count_open("og_wallet")
    return f"Open: {n_launch}/{config.MAX_CONCURRENT_LAUNCH_POSITIONS} launch, {n_og}/{config.MAX_CONCURRENT_OG_POSITIONS} og_wallet"


def _fmt_summary(rows, missed_count, label):
    if not rows:
        return f"<b>{label}</b>\nNo closed paper trades in this window. ({missed_count} entries skipped as MISSED.)\n{_bal_line()}\n{_open_line()}"

    wins = [r for r in rows if r["pnl_sol"] and r["pnl_sol"] > 0]
    total_pnl = sum(r["pnl_sol"] or 0 for r in rows)
    win_rate = len(wins) / len(rows) if rows else 0

    lines = [
        f"<b>{label}</b>",
        f"Closed trades: {len(rows)}  |  Missed: {missed_count}",
        f"Win rate: {win_rate:.0%}",
        f"Total P&L: {price_feed.fmt_usd(total_pnl)}",
        _bal_line(),
        _open_line(),
    ]
    for strat in ("launch", "og_wallet"):
        strat_rows = [r for r in rows if r["strategy"] == strat]
        if strat_rows:
            strat_pnl = sum(r["pnl_sol"] or 0 for r in strat_rows)
            strat_wr = len([r for r in strat_rows if r["pnl_sol"] and r["pnl_sol"] > 0]) / len(strat_rows)
            lines.append(f"  {strat}: {len(strat_rows)} trades, {strat_wr:.0%} win rate, {price_feed.fmt_usd(strat_pnl)}")

    perf = db.get_wallet_performance()
    if perf:
        lines.append("\n<b>Watched wallet performance:</b>")
        for w in perf[:5]:
            wr = w["wins"] / w["trades_triggered"] if w["trades_triggered"] else 0
            label_or_addr = w["label"] or w["wallet"][:8]
            lines.append(f"  {label_or_addr}: {w['trades_triggered']} triggered, {wr:.0%} win rate, {price_feed.fmt_usd(w['total_pnl_sol'])}")

    return "\n".join(lines)


async def send_daily_summary():
    since = time.time() - 86400
    rows, missed = db.get_stats_since(since)
    await send(_fmt_summary(rows, missed, "24h Paper Trading Summary"))


async def send_trade_open_alert(strategy, mint, entry_price, sol_in):
    await send(f"🟢 OPEN [{strategy}] {mint}\nsize: {price_feed.fmt_usd(sol_in)}\n{_bal_line()}\n{_open_line()}")


async def send_partial_exit_alert(strategy, mint, fraction, realized_sol):
    await send(f"🟡 PARTIAL EXIT [{strategy}] {mint}\nsold {fraction:.0%} at target, realized: {price_feed.fmt_usd(realized_sol)}\nremainder still riding")


async def send_trade_close_alert(strategy, mint, reason, pnl_sol, pnl_pct, peak_multiple=None):
    emoji = "✅" if pnl_sol and pnl_sol > 0 else "🔴"
    peak_line = f"\npeak while open: {peak_multiple:.2f}x" if peak_multiple else ""
    await send(f"{emoji} CLOSE [{strategy}] {mint}\nreason: {reason}\npnl: {price_feed.fmt_usd(pnl_sol)} ({pnl_pct:+.1%}){peak_line}\n{_bal_line()}\n{_open_line()}")


async def send_discovery_alert(wallet: str, appearance_count: int, mints_json: str):
    await send(
        f"🔎 DISCOVERY: candidate wallet found\n"
        f"<code>{wallet}</code>\n"
        f"Early buyer on {appearance_count} separate tokens that hit {config.DISCOVERY_SUCCESS_MULTIPLE}x+\n"
        f"Recent examples: {mints_json}\n"
        f"Not auto-added — review it, then add to WATCHED_WALLETS if it looks real."
    )
