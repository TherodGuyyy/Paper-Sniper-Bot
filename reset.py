"""
Wipes all paper trades and wallet-performance stats so you start fresh from
STARTING_BALANCE_SOL (config.py) — useful after tuning config.py and wanting
a clean comparison, rather than mixing old-rules trades with new ones.

Does NOT touch your watchlist (config.WATCHED_WALLETS) — those are wallets
you chose to watch, not trading results, so they survive a reset.

Run this LOCALLY against a downloaded copy of paper_sniper.db, or run it
directly on Render via the Shell tab (Render dashboard → your service →
Shell → `python reset.py`) if you want to reset the live running bot.

Usage: python reset.py
"""

import sqlite3

import config

print(f"This will permanently delete ALL paper trades and wallet performance stats in {config.DB_PATH}.")
print("Your watchlist (config.WATCHED_WALLETS) will NOT be affected.")
confirm = input("Type YES to confirm: ")

if confirm.strip() != "YES":
    print("Cancelled, nothing was deleted.")
else:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM wallet_performance")
    conn.commit()
    conn.close()
    print(f"Done. Paper balance is reset to {config.STARTING_BALANCE_SOL} SOL starting fresh.")
