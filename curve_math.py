"""
Pump.fun's bonding curve behaves like a constant-product AMM over virtual
SOL/token reserves (vSolInBondingCurve, vTokensInBondingCurve). We use that
same math to simulate what OUR hypothetical buy/sell would actually have
filled at, given the REAL reserve state at the moment our (simulated-latency)
order would land. This is what makes the paper trading honest: if real trades
from other wallets moved the reserves during our latency window, our fill
price reflects that — we are not just assuming we get the quoted price.
"""

from dataclasses import dataclass


@dataclass
class FillResult:
    tokens_or_sol_received: float
    effective_price: float          # SOL per token, after impact
    price_impact_pct: float         # vs. the pre-trade mid price
    fee_paid_sol: float


def simulate_buy(v_sol: float, v_tokens: float, sol_in: float, fee_pct: float) -> FillResult:
    """
    v_sol, v_tokens: current virtual reserves (from the latest real trade
    event we've observed for this mint).
    sol_in: SOL we are spending (before fee).
    Returns tokens received and the effective price actually paid.
    """
    if v_sol <= 0 or v_tokens <= 0 or sol_in <= 0:
        raise ValueError("invalid reserves or trade size")

    fee_paid = sol_in * fee_pct
    sol_net = sol_in - fee_paid

    k = v_sol * v_tokens
    new_v_sol = v_sol + sol_net
    new_v_tokens = k / new_v_sol
    tokens_out = v_tokens - new_v_tokens

    pre_mid_price = v_sol / v_tokens
    effective_price = sol_in / tokens_out if tokens_out > 0 else float("inf")
    impact = (effective_price - pre_mid_price) / pre_mid_price if pre_mid_price > 0 else 0.0

    return FillResult(
        tokens_or_sol_received=tokens_out,
        effective_price=effective_price,
        price_impact_pct=impact,
        fee_paid_sol=fee_paid,
    )


def simulate_sell(v_sol: float, v_tokens: float, tokens_in: float, fee_pct: float) -> FillResult:
    """
    Mirror of simulate_buy: selling tokens_in tokens into the current
    reserves, returns SOL received net of fee.
    """
    if v_sol <= 0 or v_tokens <= 0 or tokens_in <= 0:
        raise ValueError("invalid reserves or trade size")

    k = v_sol * v_tokens
    new_v_tokens = v_tokens + tokens_in
    new_v_sol = k / new_v_tokens
    sol_out_gross = v_sol - new_v_sol

    fee_paid = sol_out_gross * fee_pct
    sol_out_net = sol_out_gross - fee_paid

    pre_mid_price = v_sol / v_tokens
    effective_price = sol_out_net / tokens_in if tokens_in > 0 else 0.0
    impact = (pre_mid_price - effective_price) / pre_mid_price if pre_mid_price > 0 else 0.0

    return FillResult(
        tokens_or_sol_received=sol_out_net,
        effective_price=effective_price,
        price_impact_pct=impact,
        fee_paid_sol=fee_paid,
    )
