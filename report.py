"""
Run this locally against a copy of paper_sniper.db (download it from Render,
see README) to get a CSV you can open in Excel/Sheets and actually look at
what happened trade by trade.

Usage: python report.py
"""

import csv
import sqlite3

import config

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM trades ORDER BY entry_time").fetchall()

with open("trades_export.csv", "w", newline="") as f:
    if rows:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))

closed = [r for r in rows if r["status"] == "closed"]
missed = [r for r in rows if r["status"] == "missed"]
wins = [r for r in closed if r["pnl_sol"] and r["pnl_sol"] > 0]

print(f"Total attempts logged: {len(rows)}")
print(f"Closed trades: {len(closed)}")
print(f"Missed (slippage/no-fill): {len(missed)}")
if closed:
    total_pnl = sum(r["pnl_sol"] or 0 for r in closed)
    print(f"Win rate: {len(wins)/len(closed):.1%}")
    print(f"Total simulated P&L: {total_pnl:+.4f} SOL")
    print(f"Avg P&L per trade: {total_pnl/len(closed):+.4f} SOL")
print(f"Current paper balance: {config.STARTING_BALANCE_SOL + sum(r['pnl_sol'] or 0 for r in closed):.4f} SOL (started at {config.STARTING_BALANCE_SOL})")
print("Full detail written to trades_export.csv")

wallet_rows = conn.execute("SELECT * FROM wallet_performance ORDER BY total_pnl_sol DESC").fetchall()
if wallet_rows:
    with open("wallet_performance_export.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=wallet_rows[0].keys())
        writer.writeheader()
        for r in wallet_rows:
            writer.writerow(dict(r))
    print(f"\nWatched wallet performance ({len(wallet_rows)} wallet(s) with closed trades):")
    for w in wallet_rows:
        wr = w["wins"] / w["trades_triggered"] if w["trades_triggered"] else 0
        print(f"  {w['label'] or w['wallet'][:8]}: {w['trades_triggered']} triggered, {wr:.0%} win rate, {w['total_pnl_sol']:+.4f} SOL total")
    print("Full detail written to wallet_performance_export.csv")
