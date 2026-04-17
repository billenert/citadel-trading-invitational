"""maker_strategy.py — patient resting-order strategy for Citadel Beans.

=============================================================================
Rationale
=============================================================================
The taker (book-walk) strategy in trade.py only captures edge that is
*currently visible* at the moment we poll. Walk the book, take anything
that crosses mv, stop. It cannot capture opportunities that appear and
disappear between polls, for example:

  - Someone market-sells 50 beans, sweeping several bid levels. If we
    have no resting bid in the path, we miss it entirely.
  - A careless market-buy lifts through the book to $0.95. If we have no
    resting ask sitting in the path, someone else catches that flow.
  - Normal noise: prices oscillate, and patient resting orders eventually
    get tagged without our having to chase.

The maker strategy addresses this by parking LIMIT orders at prices
we'd be genuinely happy to fill at — at a meaningful margin from our
fair value `mv`, with sizes small enough that the *last* bean of a full
rung fill is still clearly profitable.

=============================================================================
Two-strategy separation (why they don't step on each other)
=============================================================================
The taker owns the zone right around `mv`. It walks while edge exists
and stops when edge runs out. The maker owns the zone *beyond* that,
where opportunities are rarer but the edge per fill is larger.

Each maker rung is specified as a distance `offset` from `mv`. As long
as `offset` is larger than the taker's typical walk radius, the two
strategies never touch the same price level. That is the "breathing
room" — the maker is always meaningfully outside where the taker would
already have acted.

Because the maker is patient, most of its orders don't fill most of the
time. When they do, the edge is large — and crucially, these fills
happen during transient dislocations the taker would never see.

=============================================================================
Ladder configuration
=============================================================================
Each rung is `(offset_from_mv, desired_lot)`:
  - `offset_from_mv` — distance from fair value. For bids, how far BELOW
    mv(x+1) we rest. For asks, how far ABOVE mv(x) we rest.
  - `desired_lot` — the size we'd like filled at that price.

At each poll, every rung is filtered by a size-aware profitability check:
  - bid at price P, lot Q → place only if mv(x + Q) > P + SAFETY_MARGIN
    (even the last bean of a full fill clears margin).
  - ask at price P, lot Q → place only if mv(x - Q + 1) < P - SAFETY_MARGIN
    (even the most-valuable bean we'd give up clears margin).
  - If the configured lot fails the check, it is shrunk to the largest
    lot that passes. If no lot passes, the rung is dropped.

This means the ladder self-suppresses at extreme states (e.g. at x=100,
n=0 the bid side collapses because mv(x+1)=0, and the ask side collapses
because mv(x)=Q).

=============================================================================
Public API
=============================================================================
    plan_maker_orders(x, cdf, Q) -> (bids, asks)

Returns two lists of `(price, qty)` tuples, ready to be posted as LIMIT
orders. Prices are rounded to the quoted tick (0.01). No network side
effects — this module is pure computation.

=============================================================================
Integration
=============================================================================
This file is NOT yet wired into trade.py. It is a standalone strategy
document meant to be read and edited in isolation. When you're happy
with it, integration is a 3-line change in trade.py's `step()`:

    from maker_strategy import plan_maker_orders
    m_bids, m_asks = plan_maker_orders(post_x, cdf, Q)
    # ... place each (price, qty) as a LIMIT order alongside the extremes

Run `python maker_strategy.py` to see the ladder at a variety of sample
states without touching the exchange.
"""
from __future__ import annotations

from typing import List, Tuple


# =============================================================================
# Tunables — edit these
# =============================================================================

TICK = 0.01           # BNZ is quoted_decimals=2
WINTER_TARGET = 100   # mirrors trade.py

# Per-bean cushion we insist on even at the last bean of a full fill.
# Buffers against (a) mv mis-estimates if the buyback estimator Q is off,
# and (b) adverse selection — whoever hits our resting order often knows
# something we don't.
SAFETY_MARGIN = 0.02

# Bid ladder — resting BUY limits parked BELOW mv(x+1).
# Format: (offset_below_mv, desired_lot).
# Tighter rungs first (closer to mv, smaller size); wider rungs last.
# Widen the offsets to stay further from the taker's zone; shrink lots if
# we want less total exposure when many rungs fill simultaneously.
BID_RUNGS: List[Tuple[float, int]] = [
    (0.10, 10),
    (0.25, 20),
    (0.50, 50),
]

# Ask ladder — resting SELL limits parked ABOVE mv(x).
# Format: (offset_above_mv, desired_lot).
ASK_RUNGS: List[Tuple[float, int]] = [
    (0.10, 10),
    (0.25, 20),
    (0.50, 50),
]


# =============================================================================
# Marginal value
#
# Duplicated from trade.py so this module stays standalone and readable.
# If you change the mv formula in trade.py, change it here too.
# =============================================================================
def marginal_value(x: int, cdf: List[float], Q: float) -> float:
    """mv(x) = Q * P(S <= 100 - x). Expected value of the x-th bean.

    Derivation: cost(x-1) - cost(x) = Q * P(S <= 100 - x). The x-th bean
    avoids exactly one unit of winter shortfall iff S, the sum of
    remaining harvests, is <= 100 - x.

    Saturates to Q when x is so low that every bean always helps, and to
    0 past the point where extra beans can no longer avoid any shortfall.
    """
    threshold = WINTER_TARGET - x
    if threshold < 0:
        return 0.0
    if threshold >= len(cdf):
        return Q
    return Q * cdf[threshold]


# =============================================================================
# Lot-size clamping — the size-aware profitability filter
# =============================================================================
def _max_safe_buy_lot(x: int, cdf: List[float], Q: float,
                      price: float, desired_lot: int, margin: float) -> int:
    """Largest k in [0, desired_lot] such that mv(x + k) > price + margin.

    mv is monotonically nonincreasing in its argument, so the k-th bean
    of a k-lot fill is the worst (lowest-value). If the k-th bean still
    clears margin, every earlier bean clears it by more. We walk down
    from desired_lot until the condition holds.
    """
    k = desired_lot
    while k > 0 and marginal_value(x + k, cdf, Q) <= price + margin:
        k -= 1
    return k


def _max_safe_sell_lot(x: int, cdf: List[float], Q: float,
                       price: float, desired_lot: int, margin: float) -> int:
    """Largest k such that mv(x - k + 1) + margin < price.

    When we sell k beans from position x, we give up beans indexed
    x, x-1, ..., x-k+1. The *last* (smallest-indexed) of those, at x-k+1,
    is the MOST VALUABLE bean we give up (mv grows as index shrinks). We
    need even this bean to be profitably sold at `price`.
    """
    k = desired_lot
    while k > 0 and marginal_value(x - k + 1, cdf, Q) + margin >= price:
        k -= 1
    return k


# =============================================================================
# The strategy itself
# =============================================================================
def plan_maker_orders(x: int, cdf: List[float], Q: float):
    """Compute (bids, asks) — the maker's resting LIMIT orders for now.

    Each returned list contains `(price, qty)` tuples.
      - price is rounded to TICK.
      - qty is the size-aware safe lot (<= configured desired_lot).
      - rungs where no qty > 0 passes the margin check are omitted.

    Args:
        x: current bean position (may be negative).
        cdf: harvest-sum CDF for the remaining n harvests.
        Q:  per-bean winter buyback price estimate.
    """
    mv_buy = marginal_value(x + 1, cdf, Q)   # value of the next bean acquired
    mv_sell = marginal_value(x, cdf, Q)      # value of the first bean sold

    # ---- Bid side ---------------------------------------------------------
    bids: List[Tuple[float, int]] = []
    for offset, desired_lot in BID_RUNGS:
        price = round(mv_buy - offset, 2)
        if price <= 0:
            # A bid at or below 0 is pointless (covered by the extreme-$0
            # rung living in trade.py, and the exchange may reject <=0).
            continue
        safe_lot = _max_safe_buy_lot(x, cdf, Q, price, desired_lot,
                                     SAFETY_MARGIN)
        if safe_lot > 0:
            bids.append((price, safe_lot))

    # ---- Ask side ---------------------------------------------------------
    asks: List[Tuple[float, int]] = []
    for offset, desired_lot in ASK_RUNGS:
        price = round(mv_sell + offset, 2)
        # No upper bound on ask price — if a future Q-estimator thinks
        # winter buyback is above $1, asks above $1 can still be profitable.
        safe_lot = _max_safe_sell_lot(x, cdf, Q, price, desired_lot,
                                      SAFETY_MARGIN)
        if safe_lot > 0:
            asks.append((price, safe_lot))

    return bids, asks


# =============================================================================
# Demo — `python maker_strategy.py`
# Shows the ladder output at a variety of (x, n, Q) states so you can eyeball
# whether the prices/sizes match what you'd actually want to rest.
# =============================================================================
def _harvest_pmf(n: int) -> List[float]:
    pmf = [1.0]
    for _ in range(n):
        nxt = [0.0] * (len(pmf) + 100)
        for s, p in enumerate(pmf):
            if p == 0.0:
                continue
            share = p / 101.0
            for u in range(101):
                nxt[s + u] += share
        pmf = nxt
    return pmf


def _cdf(pmf: List[float]) -> List[float]:
    out, cum = [], 0.0
    for p in pmf:
        cum += p
        out.append(cum)
    return out


def _demo() -> None:
    scenarios = [
        # label,                     x,    n, Q
        ("start of sim",              0,   2, 1.0),
        ("spring, one harvest in",   50,   1, 1.0),
        ("mid, already long",        80,   1, 1.0),
        ("mid, short",              -30,   2, 1.0),
        ("endgame, at target",      100,   0, 1.0),
        ("endgame, short 20",        80,   0, 1.0),
        ("endgame, over by 50",     150,   0, 1.0),
    ]
    fmt = lambda xs: " ".join(f"{p:.2f}x{q}" for p, q in xs) or "-"
    for label, x, n, Q in scenarios:
        cdf = _cdf(_harvest_pmf(n))
        mv_buy = marginal_value(x + 1, cdf, Q)
        mv_sell = marginal_value(x, cdf, Q)
        bids, asks = plan_maker_orders(x, cdf, Q)
        print(
            f"{label:28} x={x:>4d} n={n} Q={Q:.2f} "
            f"mv_buy={mv_buy:.3f} mv_sell={mv_sell:.3f}"
        )
        print(f"    bids: {fmt(bids)}")
        print(f"    asks: {fmt(asks)}")


if __name__ == "__main__":
    _demo()
