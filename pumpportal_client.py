"""
Thin wrapper around PumpPortal's free public WebSocket feed
(wss://pumpportal.fun/api/data). No API key needed for the data streams used
here.

IMPORTANT — verify-live note (same philosophy as the rest of your projects):
the exact field names below (mint, traderPublicKey, vSolInBondingCurve,
vTokensInBondingCurve, marketCapSol, etc.) come from PumpPortal's published
docs/tutorials, not a live-tested response. main.py logs the RAW first few
messages of each type on startup so you can confirm field names match before
trusting the numbers — if they don't, that raw dump tells you exactly what
to fix in parse_new_token/parse_trade below.
"""

import asyncio
import json
import logging

import websockets

import config

log = logging.getLogger("pumpportal")


def parse_new_token(msg: dict) -> dict:
    return {
        "mint": msg.get("mint"),
        "creator": msg.get("traderPublicKey"),
        "name": msg.get("name"),
        "symbol": msg.get("symbol"),
        "initial_buy_sol": msg.get("solAmount", 0.0),
        "v_sol": msg.get("vSolInBondingCurve"),
        "v_tokens": msg.get("vTokensInBondingCurve"),
        "market_cap_sol": msg.get("marketCapSol"),
        "raw": msg,
    }


def parse_trade(msg: dict) -> dict:
    return {
        "mint": msg.get("mint"),
        "trader": msg.get("traderPublicKey"),
        "tx_type": msg.get("txType"),  # 'buy' or 'sell'
        "sol_amount": msg.get("solAmount", 0.0),
        "token_amount": msg.get("tokenAmount", 0.0),
        "v_sol": msg.get("vSolInBondingCurve"),
        "v_tokens": msg.get("vTokensInBondingCurve"),
        "market_cap_sol": msg.get("marketCapSol"),
        "raw": msg,
    }


class PumpPortalClient:
    """
    Maintains one websocket connection, subscribes to new-token events plus
    whatever mints/wallets are added dynamically, and dispatches parsed
    events to async callback functions.
    """

    def __init__(self, on_new_token, on_token_trade, on_account_trade):
        self.on_new_token = on_new_token
        self.on_token_trade = on_token_trade
        self.on_account_trade = on_account_trade
        self._ws = None
        self._subscribed_mints = set()
        self._subscribed_accounts = set()
        self._raw_logged = {"newToken": 0, "trade": 0}

    async def run_forever(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(config.PUMPPORTAL_WS_URL, ping_interval=20) as ws:
                    self._ws = ws
                    backoff = 1
                    await self._subscribe_new_tokens()
                    # re-subscribe anything added while disconnected
                    for m in list(self._subscribed_mints):
                        await self._send({"method": "subscribeTokenTrade", "keys": [m]})
                    for a in list(self._subscribed_accounts):
                        await self._send({"method": "subscribeAccountTrade", "keys": [a]})

                    async for raw in ws:
                        await self._handle_message(raw)
            except Exception as e:
                log.warning(f"pumpportal ws disconnected ({e}), reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _subscribe_new_tokens(self):
        await self._send({"method": "subscribeNewToken"})

    async def subscribe_mint_trades(self, mint: str):
        if mint in self._subscribed_mints:
            return
        self._subscribed_mints.add(mint)
        if self._ws:
            await self._send({"method": "subscribeTokenTrade", "keys": [mint]})

    async def unsubscribe_mint_trades(self, mint: str):
        self._subscribed_mints.discard(mint)
        if self._ws:
            await self._send({"method": "unsubscribeTokenTrade", "keys": [mint]})

    async def subscribe_account(self, wallet: str):
        if wallet in self._subscribed_accounts:
            return
        self._subscribed_accounts.add(wallet)
        if self._ws:
            await self._send({"method": "subscribeAccountTrade", "keys": [wallet]})

    async def _send(self, payload: dict):
        await self._ws.send(json.dumps(payload))

    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # PumpPortal sends either a create event or a trade event on this
        # single stream; distinguish by txType/fields present. Log raw
        # samples once per type so field names can be confirmed live.
        if msg.get("txType") == "create" or ("mint" in msg and "initialBuy" in msg and "traderPublicKey" in msg and msg.get("txType") is None):
            if self._raw_logged["newToken"] < 3:
                log.info(f"[RAW newToken sample] {msg}")
                self._raw_logged["newToken"] += 1
            await self.on_new_token(parse_new_token(msg))
        elif msg.get("txType") in ("buy", "sell"):
            if self._raw_logged["trade"] < 3:
                log.info(f"[RAW trade sample] {msg}")
                self._raw_logged["trade"] += 1
            parsed = parse_trade(msg)
            await self.on_token_trade(parsed)
            if parsed["trader"] in self._subscribed_accounts:
                await self.on_account_trade(parsed)
