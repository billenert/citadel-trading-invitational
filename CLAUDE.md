# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Systematic trading bot for a Rotman Interactive Trader (RIT) **bean farming** simulation. The bot trades a single bean contract against ~77 other human/bot participants and must survive "winter" with at least 100 beans.

## Game mechanics

- Start: spring, 0 beans.
- Two harvests occur during the year, each yielding an integer in `Uniform{0, ..., 100}` i.i.d.
- At winter end, the player needs 100 beans. Shortfall is auto-purchased from the government at **$1/bean**. Excess beans are worthless (no salvage value).
- Market price ticks / news updates arrive roughly every **other minute** of sim time.
- The buyback price of $1 caps rational market price; overpaying above $1 is strictly dominated by waiting for the government buyback.

## Decision model

The bot values each bean by its expected marginal effect on winter shortfall:

```
mv(x) = Q * P(S <= 100 - x)
```

where `S` is the sum of the remaining `n` harvests and `Q` is the per-bean buyback price. `mv(x)` is the expected value of the *x*-th bean in the portfolio — i.e., the probability that harvests won't already cover us, times the buyback price we'd otherwise pay. `mv` is monotonically nonincreasing in `x` (more beans → less marginal benefit).

The bot **walks the order book** at each poll (with its own resting orders filtered out by `trader_id`):
- Walk asks ascending: buy one unit per level while `mv(x+1) > ask_price`. Stop when the next ask exceeds the marginal value.
- Walk bids descending: sell one unit per level while `bid_price > mv(x)`.

Walking level-by-level (rather than using a single price) respects the actual depth available at each quote, so the bot never assumes infinite volume at top-of-book. Crossable quantity is executed via a MARKET order.

After the active walk the bot **lays a resting LIMIT ladder** — cancelled and refreshed each poll. Tunable via the `RESTING_BIDS` / `RESTING_ASKS` constants at the top of `trade.py` (default includes 50-lot "fat-finger" orders at $0.00 and $1.00, plus a handful of intermediate rungs). Each rung is filtered by the same profitability test: bids only if `price < mv(x+1)`, asks only if `price > mv(x)`. The ladder self-suppresses at extreme positions (e.g. at `x=100, n=0` all rungs are filtered out).

### Swappable buyback estimator `Q`

`Q` is supplied by a function `flat_buyback(state) -> float` in `trade.py`. Default = `$1.00` (government rate — the hard ceiling). Replace this to model smarter beliefs (e.g., late-sim clearing price inferred from mid, aggregate market shortfall from `** Harvest One **` news totals, etc.) without touching the trade loop. The `state` dict passed in carries `case`, `bnz`, `news`, `book`, `x`, `n`.

### `valuation.py` — reference only

`valuation.py` contains an earlier single-price formulation (`optimal_trade(P, x, n, Q)`). It assumes infinite liquidity at one price and is **not** used by the trading loop. Kept as a handy one-shot CLI for hand-checking marginal-value intuition; do not import from it in new code.

## API

- **Base URL:** `http://localhost:9999/v1` (the RIT Client runs locally; the `rit.306w.ca` host only serves Swagger docs, it does NOT proxy the API).
- **Auth:** header `X-API-Key: 18P31QOO`.
- **Case name:** `Citadel Beans`. **Ticker:** `BNZ` (single stock, beans = position).
- **Full endpoint reference with live payload samples and parsing hooks: [`docs/rit_api.md`](docs/rit_api.md)** — read this before writing any new API code.

Critical gotcha worth calling out here: the `H<n> - <qty> Beans` news headline is informational — the harvested qty is **already credited** to the `BNZ` position when the news drops. Do not treat it as a buy signal.

## Commands

All scripts are stdlib-only (no `pip install`). Env overrides: `RIT_HOST`, `RIT_PORT`, `RIT_API_KEY`.

```bash
python test_connection.py           # smoke-test /case, /securities, /news
python valuation.py -P 0.80 -x 50 -n 1   # one-shot optimal-trade calculator
python trade.py                     # trading loop, DRY-RUN (default — logs only)
python trade.py --live              # trading loop, LIVE (places market orders)
python trade.py --once              # single iteration, useful for sanity checks
python trade.py --interval 10       # custom poll cadence in seconds
```

`trade.py` computes a target BNZ position each poll using `optimal_trade` at the ask (buy case) and bid (sell case), then sends a MARKET order toward the target. The winter buyback price `Q` is supplied by a pluggable estimator — see `flat_buyback` in `trade.py` and swap it out when modelling smarter clearing-price beliefs.

## Architecture notes for future work

There is no bot code yet — only `valuation.py`. When building the trading loop:

1. **State to track each tick:** cash, bean position `x`, remaining harvests `n`, last mid price, news feed cursor.
2. **Harvest detection:** harvests are announced via `/news`. Decrement `n` and credit beans to position when a harvest news item is parsed. Do not infer harvests from position changes alone — trades also move position.
3. **Signal:** feed current `(P, x, n, Q)` into `optimal_trade` (or a live variant) and convert `q` into market/limit orders, respecting order-size caps and the order book.
4. **Q is the free parameter.** $1 is the hard ceiling, but competitive pressure often pushes the effective clearing price below it. Consider estimating `Q` from observed mid-price late in the sim rather than hard-coding 1.0.
