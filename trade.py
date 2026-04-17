"""Mid-anchored market-making bot for the Citadel Beans case.

Strategy
--------
Centers quotes on an EMA of market mid (not theoretical fair value).
Uses EMA variance to adaptively widen/tighten spreads with volatility.
Inventory skew shifts the center to shed position toward TARGET (100 beans).
Linear rung spacing with large lots to absorb clueless-opponent mistakes.
Taker sweep runs first each tick, then resting ladder is placed.
Extreme $0 / $1 fat-finger rungs always on.

EMA resets on harvest news to avoid lagging a genuine fundamental shift.

Polls every tick (1 second, 540 ticks total). DRY-RUN by default.

Usage:
    python trade.py                 # dry-run, 1-tick cadence
    python trade.py --live          # place real orders
    python trade.py --once          # single tick and exit
    python trade.py --interval 1    # explicit poll cadence (seconds)

Env (optional): RIT_HOST, RIT_PORT, RIT_API_KEY
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
from typing import List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════
# Connection
# ═══════════════════════════════════════════════════════════════════════════
HOST = os.environ.get("RIT_HOST", "localhost")
PORT = os.environ.get("RIT_PORT", "9999")
KEY  = os.environ.get("RIT_API_KEY", "18P31QOO")
BASE = f"http://{HOST}:{PORT}/v1"
TIMEOUT = 5.0

TICKER         = "BNZ"
TOTAL_HARVESTS = 2
WINTER_TARGET  = 100
BOOK_DEPTH     = 1000
TICK           = 0.01

HARVEST_HEADLINE = re.compile(r"^H(\d+)\s*-\s*(\d+)\s*Beans$")


# ═══════════════════════════════════════════════════════════════════════════
# Market-maker parameters — all tunables in one place
# ═══════════════════════════════════════════════════════════════════════════

# -- Ladder shape --
N_RUNGS        = 5       # levels per side
BASE_LOT       = 200     # lot size of the tightest rung
LOT_INCREMENT  = 200     # additive increase per rung → 200, 400, 600, 800, 1000

# -- Spread & spacing --
MIN_EDGE       = 0.01    # hard minimum half-spread (1 tick)
BASE_SPREAD    = 0.02    # half-spread floor in calm markets ($)
VOL_MULT       = 2.0     # half_spread += VOL_MULT * ema_vol
STEP_FLOOR     = 0.02    # minimum gap between adjacent rungs ($)
STEP_FRAC      = 0.5     # rung step = half_spread * STEP_FRAC (before floor)
VOL_STEP_SCALE = 0.00005 # extra step per bean in the rung's lot

# -- Inventory management --
SKEW_FACTOR    = 0.10    # max center shift ($) at full inventory
MAX_INVENTORY  = 2000    # deviation from target where one side fully suppresses
TARGET         = WINTER_TARGET
MAX_POSITION   = 20000   # hard exchange limit (gross/net)

# -- EMA --
EMA_ALPHA      = 0.03    # ~23-tick half-life at 1 tick/sec

# -- Timing / blackout --
MIN_TICK       = 10      # don't trade before this tick (let market stabilize)
BLACKOUT_PRE   = 5       # ticks before a news event to pull orders
BLACKOUT_POST  = 5       # ticks after a news event to stay dark

# News event ticks where the price can gap (from docs/rit_api.md timeline).
# Personal harvests change our position; aggregate reveals total supply.
_NEWS_TICKS = [
    50,   # personal H1 warning  ("released in 10sec")
    60,   # personal H1 drop     (qty auto-credited to BNZ)
    170,  # aggregate H1 warning ("Release in 10sec")
    180,  # aggregate H1 release ("Total Harvest is N")
    289,  # personal H2 warning
    299,  # personal H2 drop
]


def _in_blackout(tick: int) -> bool:
    """True if we should NOT have orders resting during this tick."""
    if tick <= MIN_TICK:
        return True
    return any(t - BLACKOUT_PRE <= tick <= t + BLACKOUT_POST
               for t in _NEWS_TICKS)


# -- Extreme fat-finger rungs (always on, outside the ladder) --
EXTREME_BID_PRICE = 0.00
EXTREME_BID_QTY   = 1000
EXTREME_ASK_PRICE = 1.00
EXTREME_ASK_QTY   = 1000


# ═══════════════════════════════════════════════════════════════════════════
# API helpers (stdlib only)
# ═══════════════════════════════════════════════════════════════════════════
def _request(method: str, path: str, params: Optional[dict] = None):
    url = f"{BASE}{path}"
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = b"" if method == "POST" else None
    req = urllib.request.Request(
        url, method=method, data=data, headers={"X-API-Key": KEY},
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
    """Cached /trader.trader_id.  Empty string if unavailable."""
    global _TRADER_ID
    if _TRADER_ID is None:
        try:
            _TRADER_ID = api_get("/trader").get("trader_id") or ""
        except Exception:
            _TRADER_ID = ""
    return _TRADER_ID


# ═══════════════════════════════════════════════════════════════════════════
# Order helpers
# ═══════════════════════════════════════════════════════════════════════════
def cancel_my_ticker_orders(ticker: str) -> int:
    """Bulk-cancel all our open orders for *ticker*."""
    try:
        api_post("/commands/cancel", ticker=ticker)
        return -1
    except urllib.error.HTTPError:
        pass
    # Fallback: iterate
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
        "/orders", ticker=TICKER, type="MARKET", action=action, quantity=qty,
    )


def limit_order(action: str, qty: int, price: float):
    return api_post(
        "/orders", ticker=TICKER, type="LIMIT", action=action, quantity=qty,
        price=price,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Book-walk helpers (threshold + max-qty based)
# ═══════════════════════════════════════════════════════════════════════════
def _remaining(level: dict) -> int:
    qty = float(level.get("quantity") or 0)
    filled = float(level.get("quantity_filled") or 0)
    return max(0, int(qty - filled))


def walk_asks(asks: list, threshold: float, max_qty: int) -> Tuple[int, float]:
    """Walk ascending asks; buy each level while price < threshold."""
    asks = sorted(asks, key=lambda lvl: float(lvl["price"]))
    qty, last_price = 0, 0.0
    for lvl in asks:
        price = float(lvl["price"])
        if price >= threshold:
            break
        avail = _remaining(lvl)
        if avail <= 0:
            continue
        take = min(avail, max_qty - qty)
        if take <= 0:
            break
        qty += take
        last_price = price
    return qty, last_price


def walk_bids(bids: list, threshold: float, max_qty: int) -> Tuple[int, float]:
    """Walk descending bids; sell each level while price > threshold."""
    bids = sorted(bids, key=lambda lvl: -float(lvl["price"]))
    qty, last_price = 0, 0.0
    for lvl in bids:
        price = float(lvl["price"])
        if price <= threshold:
            break
        avail = _remaining(lvl)
        if avail <= 0:
            continue
        take = min(avail, max_qty - qty)
        if take <= 0:
            break
        qty += take
        last_price = price
    return qty, last_price


# ═══════════════════════════════════════════════════════════════════════════
# Harvest detection
# ═══════════════════════════════════════════════════════════════════════════
def personal_harvests_done(news: list) -> int:
    return sum(
        1 for n in news
        if HARVEST_HEADLINE.match((n.get("headline") or "").strip())
    )


# ═══════════════════════════════════════════════════════════════════════════
# Harvest PMF / CDF / marginal value  (kept for endgame reference)
# ═══════════════════════════════════════════════════════════════════════════
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
    threshold = WINTER_TARGET - x
    if threshold < 0:
        return 0.0
    if threshold >= len(cdf):
        return Q
    return Q * cdf[threshold]


# ═══════════════════════════════════════════════════════════════════════════
# Market Maker
# ═══════════════════════════════════════════════════════════════════════════
class MarketMaker:
    """Stateful market-making engine.  Carries EMA across ticks."""

    def __init__(self, live: bool):
        self.live = live
        self.ema_mid: Optional[float] = None
        self.ema_var: float = 0.0
        self.last_tick: int = -1
        self.prev_harvests: int = 0

    # ── helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _raw_mid(bnz: dict, fallback: Optional[float]) -> float:
        """Mid from top-of-book; graceful fallback for empty sides."""
        bid = bnz.get("bid", 0)
        ask = bnz.get("ask", 0)
        has_bid = bid > 0 and bnz.get("bid_size", 0) > 0
        has_ask = ask > 0 and bnz.get("ask_size", 0) > 0
        if has_bid and has_ask:
            return (bid + ask) / 2.0
        if has_bid:
            return bid
        if has_ask:
            return ask
        if fallback is not None:
            return fallback
        return 0.50

    def _update_ema(self, raw_mid: float, n_harvests: int) -> None:
        """EMA mid + variance.  Hard-resets on new harvest."""
        if n_harvests > self.prev_harvests:
            # Fundamental shift — snap to new price, zero vol
            self.ema_mid = raw_mid
            self.ema_var = 0.0
            self.prev_harvests = n_harvests
            return
        self.prev_harvests = n_harvests

        if self.ema_mid is None:
            self.ema_mid = raw_mid
            self.ema_var = 0.0
            return

        innovation = raw_mid - self.ema_mid
        self.ema_mid = EMA_ALPHA * raw_mid + (1 - EMA_ALPHA) * self.ema_mid
        self.ema_var = EMA_ALPHA * innovation ** 2 + (1 - EMA_ALPHA) * self.ema_var

    def _compute_ladder(
        self, post_x: int, center: float, half_spread: float,
    ) -> Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]:
        """Return (bids, asks) for the resting ladder.

        Volume scales with inventory: the side we want to shed gets bigger,
        the side we'd accumulate on shrinks, linearly in inv_ratio.
        Includes extreme fat-finger rungs.
        """
        inv_ratio = max(-1.0, min(1.0, (post_x - TARGET) / MAX_INVENTORY))

        bids: List[Tuple[float, int]] = []
        asks: List[Tuple[float, int]] = []
        offset = half_spread

        for i in range(N_RUNGS):
            lot = BASE_LOT + i * LOT_INCREMENT

            # Inventory-scaled lots:
            #   long (inv_ratio>0) → bid shrinks, ask grows
            #   short (inv_ratio<0) → bid grows, ask shrinks
            bid_lot = round(lot * max(0.0, 1.0 - inv_ratio))
            ask_lot = round(lot * max(0.0, 1.0 + inv_ratio))

            bid_price = round(center - offset, 2)
            ask_price = round(center + offset, 2)

            if 0.01 <= bid_price <= 0.99 and bid_lot > 0:
                bids.append((bid_price, bid_lot))
            if 0.01 <= ask_price <= 0.99 and ask_lot > 0:
                asks.append((ask_price, ask_lot))

            # Step widens with spread (vol-adaptive) and with rung lot size
            step = max(STEP_FLOOR, half_spread * STEP_FRAC) + lot * VOL_STEP_SCALE
            offset += step

        # Extreme fat-finger rungs (respect hard position limit only)
        if post_x < MAX_POSITION:
            bids.append((EXTREME_BID_PRICE, EXTREME_BID_QTY))
        if post_x > -MAX_POSITION:
            asks.append((EXTREME_ASK_PRICE, EXTREME_ASK_QTY))

        return bids, asks

    def _sync_ladder(
        self,
        desired_bids: List[Tuple[float, int]],
        desired_asks: List[Tuple[float, int]],
    ) -> Tuple[int, int, int, List[str]]:
        """Differential order sync — only cancel/place what actually changed.

        Matches existing resting orders against the desired ladder by
        (action, price).  Orders at a matching price are KEPT (preserving
        queue priority).  Unmatched existing orders are cancelled; unmatched
        desired rungs are placed fresh.

        If the diff is large enough to risk the 20-orders/sec rate limit,
        falls back to bulk cancel + repost.

        Returns (cancelled, placed, kept, errors).
        """
        # Build desired keyed by (action, price)
        desired: dict[Tuple[str, float], int] = {}
        for price, qty in desired_bids:
            desired[("BUY", round(price, 2))] = qty
        for price, qty in desired_asks:
            desired[("SELL", round(price, 2))] = qty

        # Ground truth: what's actually resting on the exchange
        try:
            open_orders = api_get("/orders?status=OPEN") or []
        except Exception:
            open_orders = []

        # Match existing orders against desired rungs
        matched: set[Tuple[str, float]] = set()
        to_cancel: List[int] = []

        for o in open_orders:
            if o.get("ticker") != TICKER or o.get("type") != "LIMIT":
                continue
            remaining = _remaining(o)
            if remaining <= 0:
                continue
            key = (o["action"], round(float(o["price"]), 2))
            if key in desired and key not in matched:
                matched.add(key)          # keep — same side & price
            else:
                to_cancel.append(o["order_id"])  # stale or duplicate

        # Desired rungs with no existing match → need fresh placement
        to_place = [
            (action, price, desired[(action, price)])
            for action, price in desired
            if (action, price) not in matched
        ]

        # If the diff is large, bulk cancel + full repost is cheaper and
        # safer than hitting the 20-ops/sec rate limit with individual
        # DELETE + POST calls.
        total_ops = len(to_cancel) + len(to_place)
        if total_ops > 16:
            cancel_my_ticker_orders(TICKER)
            to_cancel = []
            matched = set()
            to_place = [
                (action, price, desired[(action, price)])
                for action, price in desired
            ]

        # Execute cancels
        cancelled = 0
        for oid in to_cancel:
            try:
                _request("DELETE", f"/orders/{oid}")
                cancelled += 1
            except Exception:
                pass

        # Execute placements
        placed = 0
        errors: List[str] = []
        for action, price, qty in to_place:
            try:
                limit_order(action, qty, price)
                placed += 1
            except urllib.error.HTTPError as e:
                errors.append(
                    f"{action} {qty}@{price:.2f} {e.code} "
                    f"{e.read().decode()[:60]}"
                )

        kept = len(matched)
        return cancelled, placed, kept, errors

    # ── main tick ──────────────────────────────────────────────────────
    def step(self) -> None:
        case = api_get("/case")
        tick = case["tick"]

        # Dedup — don't reprocess the same tick
        if tick == self.last_tick:
            return
        self.last_tick = tick

        securities = api_get("/securities")
        news = api_get("/news?limit=50")
        book = api_get(f"/securities/book?ticker={TICKER}&limit={BOOK_DEPTH}")

        bnz = next(s for s in securities if s["ticker"] == TICKER)
        x = int(round(bnz["position"]))

        # ── EMA update ────────────────────────────────────────────────
        raw_mid = self._raw_mid(bnz, self.ema_mid)
        n_harvests = personal_harvests_done(news)
        self._update_ema(raw_mid, n_harvests)

        ema_vol = self.ema_var ** 0.5
        half_spread = max(MIN_EDGE, BASE_SPREAD + VOL_MULT * ema_vol)

        # ── inventory skew (pre-trade) ────────────────────────────────
        inv_ratio_pre = max(-1.0, min(1.0, (x - TARGET) / MAX_INVENTORY))
        skew = -SKEW_FACTOR * inv_ratio_pre
        center = self.ema_mid + skew

        # ── log prefix ────────────────────────────────────────────────
        prefix = (
            f"t={tick:>3}/{case['ticks_per_period']} x={x:>5d} "
            f"mid={raw_mid:.2f} ema={self.ema_mid:.3f} "
            f"\u03c3={ema_vol:.4f} hs={half_spread:.3f} skew={skew:+.3f}"
        )

        if case["status"] != "ACTIVE":
            print(f"{prefix}  HOLD ({case['status']})")
            return

        # ── blackout: pull everything around news events ──────────────
        if _in_blackout(tick):
            if self.live:
                try:
                    cancel_my_ticker_orders(TICKER)
                except Exception:
                    pass
            reason = "warmup" if tick <= MIN_TICK else "news"
            print(f"{prefix}  BLACKOUT ({reason})")
            return

        # ── taker: sweep crossable edge ───────────────────────────────
        own = trader_id()
        ext_asks = [a for a in (book.get("asks") or [])
                    if a.get("trader_id") != own]
        ext_bids = [b for b in (book.get("bids") or [])
                    if b.get("trader_id") != own]

        # Threshold = our would-be best bid / best ask
        taker_buy_thr = center - half_spread
        taker_sell_thr = center + half_spread

        # Position-limit aware max qty
        max_buy  = max(0, MAX_POSITION - x)
        max_sell = max(0, MAX_POSITION + x)

        buy_qty, buy_px   = walk_asks(ext_asks, taker_buy_thr, max_buy)
        sell_qty, sell_px  = walk_bids(ext_bids, taker_sell_thr, max_sell)

        post_x = x
        if buy_qty > 0:
            post_x += buy_qty
            action_tag = _do_market("BUY", buy_qty, buy_px, self.live)
        elif sell_qty > 0:
            post_x -= sell_qty
            action_tag = _do_market("SELL", sell_qty, sell_px, self.live)
        else:
            action_tag = "no-cross"

        # ── maker: compute desired ladder, sync differentially ────────
        inv_ratio_post = max(-1.0, min(1.0, (post_x - TARGET) / MAX_INVENTORY))
        skew_post = -SKEW_FACTOR * inv_ratio_post
        center_post = self.ema_mid + skew_post

        bids, asks = self._compute_ladder(post_x, center_post, half_spread)

        if self.live:
            cancelled, placed, kept, errors = self._sync_ladder(bids, asks)
            sync_tag = f"sync:-{cancelled}/+{placed}/={kept}"
        else:
            cancelled, placed, kept, errors = 0, 0, 0, []
            sync_tag = "[dry]"

        # ── log ───────────────────────────────────────────────────────
        bid_str = " ".join(f"{p:.2f}x{q}" for p, q in bids) or "-"
        ask_str = " ".join(f"{p:.2f}x{q}" for p, q in asks) or "-"
        print(
            f"{prefix}  {action_tag}  {sync_tag}  "
            f"B[{bid_str}] A[{ask_str}]"
        )
        for err in errors:
            print(f"     ! {err}")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════
def _do_market(action: str, qty: int, last_px: float, live: bool) -> str:
    if not live:
        return f"[dry] MKT {action} {qty} (~{last_px:.2f})"
    try:
        resp = market_order(action, qty)
        return (
            f"MKT {action} {qty} "
            f"filled={resp.get('quantity_filled')} "
            f"vwap={resp.get('vwap')} "
            f"status={resp.get('status')}"
        )
    except urllib.error.HTTPError as e:
        return f"MKT {action} {qty} FAIL {e.code} {e.read().decode()[:80]}"


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════
def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--interval", type=float, default=1.0,
                   help="poll interval in seconds (default 1 — every tick)")
    p.add_argument("--live", action="store_true",
                   help="actually place orders (default: dry-run)")
    p.add_argument("--once", action="store_true",
                   help="single tick, then exit")
    args = p.parse_args()

    mm = MarketMaker(live=args.live)
    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"trade.py -> {BASE}  mode={mode}  interval={args.interval}s")
    if args.live:
        print(f"trader_id = {trader_id()!r}")
    print(
        f"params: N_RUNGS={N_RUNGS} BASE_LOT={BASE_LOT} "
        f"LOT_INC={LOT_INCREMENT} BASE_SPREAD={BASE_SPREAD} "
        f"VOL_MULT={VOL_MULT} SKEW={SKEW_FACTOR} "
        f"MAX_INV={MAX_INVENTORY} EMA_α={EMA_ALPHA}"
    )

    try:
        while True:
            try:
                mm.step()
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
