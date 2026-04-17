"""Book-walking trading bot for the Citadel Beans case.

Each poll:
  1. GET /case, /securities, /news, /securities/book
  2. Walk the (external) order book level-by-level:
       - buy while mv(x+1) > next ask price
       - sell while next bid price > mv(x)
     Send a MARKET order for the walked quantity — this takes crossable
     edge immediately.
  3. Cancel and re-lay a resting LIMIT ladder: extreme "fat-finger"
     orders at $0 / $1 plus a few intermediate rungs. Each rung is
     filtered by profitability (bids only if price < mv(x+1); asks only
     if price > mv(x)), so the ladder self-suppresses once we are full.

Valuation
---------
mv(x) = Q * P(S <= 100 - x)   # expected value of the x-th bean
Derivation: cost(x-1) - cost(x) = Q * P(S <= 100 - x), since the x-th
bean avoids one unit of winter shortfall iff harvest-sum S <= 100 - x.

Q is supplied by a pluggable estimator (default `flat_buyback` = $1,
the government rate / hard ceiling). Swap it in `main` for a smarter
model as we learn more about how the crowd clears.

DRY-RUN by default. Pass --live to actually send orders.

Usage:
    python trade.py
    python trade.py --live
    python trade.py --once
    python trade.py --interval 10

Env (optional): RIT_HOST, RIT_PORT, RIT_API_KEY  (see test_connection.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional, Tuple


HOST = os.environ.get("RIT_HOST", "localhost")
PORT = os.environ.get("RIT_PORT", "9999")
KEY = os.environ.get("RIT_API_KEY", "18P31QOO")
BASE = f"http://{HOST}:{PORT}/v1"
TIMEOUT = 5.0

TICKER = "BNZ"
TOTAL_HARVESTS = 2
WINTER_TARGET = 100
BOOK_DEPTH = 1000

# Tick size — BNZ is quoted to 2 decimals (see /securities.quoted_decimals).
TICK = 0.01

HARVEST_HEADLINE = re.compile(r"^H(\d+)\s*-\s*(\d+)\s*Beans$")

# Extreme resting LIMIT ladder — the "fat-finger harvesters". Someone
# panic-dumps at zero or a desperate buyer pays the ceiling, we win.
# Each rung is (price, qty). Filtered by marginal value each poll.
RESTING_BIDS: List[Tuple[float, int]] = [(0.00, 1000)]
RESTING_ASKS: List[Tuple[float, int]] = [(1.00, 1000)]

# Flow-capture rung lot size. One bid / one ask placed a tick inside the
# current spread (clamped by our fair value) to catch counterparties who
# cross the spread aggressively. These complement the MARKET book walk
# (which only takes currently-crossable edge) and the extreme 0/1 rungs
# (which only catch fat-fingers).
FLOW_LOT = 50


# ---------------------------------------------------------------------------
# API helpers (stdlib only — no pip install)
# ---------------------------------------------------------------------------
def _request(method: str, path: str, params: Optional[dict] = None):
    url = f"{BASE}{path}"
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = b"" if method == "POST" else None
    req = urllib.request.Request(
        url, method=method, data=data, headers={"X-API-Key": KEY}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def api_get(path: str):
    return _request("GET", path)


def api_post(path: str, **params):
    return _request("POST", path, params)


_TRADER_ID: Optional[str] = None


def trader_id() -> str:
    """Cached /trader.trader_id. Empty string if unavailable."""
    global _TRADER_ID
    if _TRADER_ID is None:
        try:
            _TRADER_ID = api_get("/trader").get("trader_id") or ""
        except Exception:
            _TRADER_ID = ""
    return _TRADER_ID


# ---------------------------------------------------------------------------
# Harvest PMF, CDF, marginal value
# ---------------------------------------------------------------------------
def harvest_pmf(n: int) -> List[float]:
    """PMF of the sum of n i.i.d. Uniform{0..100} harvests."""
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


def harvest_cdf(pmf: List[float]) -> List[float]:
    cdf, cum = [], 0.0
    for p in pmf:
        cum += p
        cdf.append(cum)
    return cdf


def marginal_value(x: int, cdf: List[float], Q: float) -> float:
    """mv(x) = Q * P(S <= 100 - x) — expected value of the x-th bean."""
    threshold = WINTER_TARGET - x
    if threshold < 0:
        return 0.0
    if threshold >= len(cdf):
        return Q
    return Q * cdf[threshold]


# ---------------------------------------------------------------------------
# Order-book walk
# ---------------------------------------------------------------------------
def _remaining(level: dict) -> int:
    qty = float(level.get("quantity") or 0)
    filled = float(level.get("quantity_filled") or 0)
    return max(0, int(qty - filled))


def walk_asks(x: int, asks: list, cdf: List[float], Q: float) -> Tuple[int, float]:
    """Walk ascending asks; buy each unit while mv(cur+1) > price."""
    asks = sorted(asks, key=lambda lvl: float(lvl["price"]))
    qty, last_price, cur = 0, 0.0, x
    for lvl in asks:
        price = float(lvl["price"])
        avail = _remaining(lvl)
        if avail <= 0:
            continue
        while avail > 0 and marginal_value(cur + 1, cdf, Q) > price:
            cur += 1
            avail -= 1
            qty += 1
            last_price = price
        if avail > 0:
            break  # unprofitable; deeper levels are all >= price
    return qty, last_price


def walk_bids(x: int, bids: list, cdf: List[float], Q: float) -> Tuple[int, float]:
    """Walk descending bids; sell each unit while price > mv(cur)."""
    bids = sorted(bids, key=lambda lvl: -float(lvl["price"]))
    qty, last_price, cur = 0, 0.0, x
    for lvl in bids:
        price = float(lvl["price"])
        avail = _remaining(lvl)
        if avail <= 0:
            continue
        while avail > 0 and price > marginal_value(cur, cdf, Q):
            cur -= 1
            avail -= 1
            qty += 1
            last_price = price
        if avail > 0:
            break
    return qty, last_price


# ---------------------------------------------------------------------------
# Order placement / cancellation
# ---------------------------------------------------------------------------
def cancel_my_ticker_orders(ticker: str) -> int:
    """Cancel all our open orders for `ticker`. Returns count cancelled (or
    attempted). Tries /commands/cancel first, falls back to per-order DELETE."""
    # Bulk cancel
    try:
        api_post("/commands/cancel", ticker=ticker)
        return -1  # bulk doesn't tell us exact count
    except urllib.error.HTTPError:
        pass
    # Fallback: iterate open orders
    try:
        orders = api_get("/orders?status=OPEN") or []
    except Exception:
        return 0
    n = 0
    for o in orders:
        if o.get("ticker") != ticker:
            continue
        try:
            _request("DELETE", f"/orders/{o['order_id']}")
            n += 1
        except Exception:
            pass
    return n


def market_order(action: str, qty: int):
    return api_post(
        "/orders", ticker=TICKER, type="MARKET", action=action, quantity=qty
    )


def limit_order(action: str, qty: int, price: float):
    return api_post(
        "/orders", ticker=TICKER, type="LIMIT", action=action, quantity=qty,
        price=price,
    )


# ---------------------------------------------------------------------------
# Buyback estimator — swap for a smarter model later.
# state keys: case, bnz, news, book, x, n
# ---------------------------------------------------------------------------
BuybackEstimator = Callable[[dict], float]


def flat_buyback(state: dict) -> float:
    """Government rate. Hard ceiling on rational market price."""
    return 1.0


# ---------------------------------------------------------------------------
# Harvest accounting
# ---------------------------------------------------------------------------
def personal_harvests_done(news: list) -> int:
    return sum(
        1 for n in news
        if HARVEST_HEADLINE.match((n.get("headline") or "").strip())
    )


# ---------------------------------------------------------------------------
# Resting ladder
# ---------------------------------------------------------------------------
def resting_plan(x: int, cdf: List[float], Q: float):
    """Return (bids_to_place, asks_to_place), each a list of (price, qty).
    Only rungs strictly profitable at current state are included."""
    mv_buy = marginal_value(x + 1, cdf, Q)
    mv_sell = marginal_value(x, cdf, Q)
    bids = [(p, q) for p, q in RESTING_BIDS if p < mv_buy]
    asks = [(p, q) for p, q in RESTING_ASKS if p > mv_sell]
    return bids, asks


def _format_ladder(orders: list) -> str:
    if not orders:
        return "-"
    return " ".join(f"{p:.2f}x{q}" for p, q in orders)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------
def step(live: bool, buyback: BuybackEstimator) -> None:
    case = api_get("/case")
    securities = api_get("/securities")
    news = api_get("/news?limit=50")
    book = api_get(f"/securities/book?ticker={TICKER}&limit={BOOK_DEPTH}")

    bnz = next(s for s in securities if s["ticker"] == TICKER)
    x = int(round(bnz["position"]))
    bid_top, ask_top = bnz["bid"], bnz["ask"]

    n_rem = max(0, TOTAL_HARVESTS - personal_harvests_done(news))
    cdf = harvest_cdf(harvest_pmf(n_rem))

    state = {"case": case, "bnz": bnz, "news": news, "book": book, "x": x, "n": n_rem}
    Q = buyback(state)

    prefix = (
        f"t={case['tick']:>3}/{case['ticks_per_period']} "
        f"x={x:>4d} n={n_rem} bid={bid_top:.2f} ask={ask_top:.2f} Q={Q:.2f}"
    )

    if case["status"] != "ACTIVE":
        print(f"{prefix} status={case['status']} -> hold")
        return

    # Exclude our own resting orders from the walk.
    own = trader_id()
    ext_asks = [a for a in (book.get("asks") or []) if a.get("trader_id") != own]
    ext_bids = [b for b in (book.get("bids") or []) if b.get("trader_id") != own]

    buy_qty, buy_px = walk_asks(x, ext_asks, cdf, Q)
    sell_qty, sell_px = walk_bids(x, ext_bids, cdf, Q)

    # Refresh resting orders every poll — cancel first, place later.
    if live:
        try:
            cancel_my_ticker_orders(TICKER)
        except Exception as e:
            print(f"{prefix} cancel error: {type(e).__name__}: {e}")

    # Execute the active (crossable) portion with a MARKET order.
    post_x = x
    if buy_qty > 0:
        post_x += buy_qty
        action_tag = _do_market("BUY", buy_qty, buy_px, live)
    elif sell_qty > 0:
        post_x -= sell_qty
        action_tag = _do_market("SELL", sell_qty, sell_px, live)
    else:
        mv_up = marginal_value(x + 1, cdf, Q)
        mv_here = marginal_value(x, cdf, Q)
        action_tag = f"no-cross mv(x+1)={mv_up:.3f} mv(x)={mv_here:.3f}"

    # Plan and place the resting ladder based on post-active position.
    bids, asks = resting_plan(post_x, cdf, Q)
    errors = []
    if live:
        for price, qty in bids:
            try:
                limit_order("BUY", qty, price)
            except urllib.error.HTTPError as e:
                errors.append(f"BUY {qty}@{price:.2f} {e.code} {e.read().decode()[:80]}")
        for price, qty in asks:
            try:
                limit_order("SELL", qty, price)
            except urllib.error.HTTPError as e:
                errors.append(f"SELL {qty}@{price:.2f} {e.code} {e.read().decode()[:80]}")

    mode = "" if live else "[dry] "
    print(
        f"{prefix} | {action_tag} | "
        f"{mode}bids: {_format_ladder(bids)} | asks: {_format_ladder(asks)}"
    )
    for err in errors:
        print(f"     ! {err}")


def _do_market(action: str, qty: int, last_px: float, live: bool) -> str:
    if not live:
        return f"[dry] MKT {action} {qty} (last {last_px:.2f})"
    try:
        resp = market_order(action, qty)
        return (
            f"MKT {action} {qty} "
            f"filled={resp.get('quantity_filled')} vwap={resp.get('vwap')} "
            f"status={resp.get('status')}"
        )
    except urllib.error.HTTPError as e:
        return f"MKT {action} {qty} FAIL {e.code} {e.read().decode()[:100]}"


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--interval", type=float, default=5.0,
                   help="poll interval in seconds (default 5)")
    p.add_argument("--live", action="store_true",
                   help="actually place orders (default: dry-run, log only)")
    p.add_argument("--once", action="store_true",
                   help="run a single iteration and exit")
    args = p.parse_args()

    buyback: BuybackEstimator = flat_buyback
    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"trade.py -> {BASE}  mode={mode}  interval={args.interval}s")
    if args.live:
        print(f"trader_id = {trader_id()!r}")

    try:
        while True:
            try:
                step(args.live, buyback)
            except Exception as e:
                print(f"step error: {type(e).__name__}: {e}")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ninterrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
