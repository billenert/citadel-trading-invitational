# RIT REST API — Bean-Case Reference

Working notes on the Rotman Interactive Trader (RIT) REST API as observed
against the **Citadel Beans** case. Complements the upstream Swagger UI at
`https://rit.306w.ca/RIT-REST-API/1.0.3/?port=9999&key=18P31QOO`, which only
serves documentation — it does **not** proxy the API.

All payload shapes below are copied from live responses, not guessed.

## Connection

- **Base URL:** `http://localhost:9999/v1` — the RIT Client on the local
  machine exposes the API. Attempts to reach `https://rit.306w.ca/v1/*`
  return `404`; that host only serves Swagger.
- **Auth:** every request requires header `X-API-Key: <key>`. The key for
  the current sim is `18P31QOO` (same key embedded in the Swagger URL).
- **Rate limit:** per-security cap of 20 orders/sec (`api_orders_per_second`
  on `/securities`). There is no explicit GET cap observed.
- **Content type:** all JSON. Most list endpoints accept `limit=` and some
  accept filter params (`ticker`, `status`, etc.).

## Case constants

Captured from live responses:

| Field                | Value              |
|----------------------|--------------------|
| Case name            | `Citadel Beans`    |
| Players              | `78`               |
| Periods              | `1`                |
| Ticks per period     | `540`              |
| Tradable tickers     | `BNZ` (stock)      |
| Start price          | `$0.50`            |
| Price band           | `[-10.0, 10.0]`    |
| `min_trade_size`     | `0`                |
| `max_trade_size`     | `10000`            |
| `trading_fee`        | `0.0`              |
| `execution_delay_ms` | `0`                |
| Position limits      | `LIMIT-STOCK` gross/net 20000 (effectively unbounded) |

## Endpoints

### `GET /case`

Basic sim state. Call this every loop iteration to read the clock.

```json
{
  "name": "Citadel Beans",
  "period": 1,
  "tick": 288,
  "ticks_per_period": 540,
  "total_periods": 1,
  "status": "ACTIVE",
  "is_enforce_trading_limits": false
}
```

`status` is one of `ACTIVE`, `PAUSED`, `STOPPED`. Treat anything but `ACTIVE`
as "do not send orders."

### `GET /trader`

Identity + net liquidation value. Useful for logging.

```json
{"trader_id": "BilFe", "first_name": "Bill", "last_name": "Fei", "nlv": 56.44}
```

`trader_id` is the 5-char code that appears on orders in the book.

### `GET /limits`

```json
[{"name":"LIMIT-STOCK","gross":83.0,"net":83.0,"gross_limit":20000,"net_limit":20000,"gross_fine":0.0,"net_fine":0.0}]
```

`gross` / `net` are the current exposure; `*_limit` are the caps. `*_fine`
accrues if you breach — stay below.

### `GET /securities`

Position and top-of-book per instrument. This is the primary snapshot call.

```json
[{
  "ticker":"BNZ","type":"STOCK","size":1,
  "position":50.0,"vwap":0.0,"nlv":42.00,
  "last":0.0,"bid":0.84,"bid_size":30.0,"ask":0.85,"ask_size":10.0,
  "volume":0.0,"unrealized":42.00,"realized":0.0,
  "total_volume":0.0,"is_tradeable":true,"is_shortable":true,
  "start_period":1,"stop_period":1,
  "start_price":0.5,"min_price":-10.0,"max_price":10.0,
  "quoted_decimals":2,"trading_fee":0.0,"limit_order_rebate":0.0,
  "min_trade_size":0,"max_trade_size":10000,
  "api_orders_per_second":20,"execution_delay_ms":0
}]
```

Notes:
- `position` is a float but values are integer in this case.
- `bid` / `ask` may be `0` when one side of the book is empty — check
  `bid_size` / `ask_size` before quoting a mid.
- `nlv` includes mark-to-market at `last` (which can be stale).

### `GET /securities/book?ticker=BNZ&limit=N`

Full (or top-N) order book.

```json
{
  "bids":[
    {"order_id":81,"period":1,"tick":142,"trader_id":"VarKu","ticker":"BNZ",
     "quantity":1000.0,"price":0.68,"type":"LIMIT","action":"BUY",
     "quantity_filled":0.0,"vwap":null,"status":"OPEN"},
    ...
  ],
  "asks":[
    {"order_id":318,"period":1,"tick":377,"trader_id":"VarKu","ticker":"BNZ",
     "quantity":17.0,"price":0.84,"type":"LIMIT","action":"SELL",
     "quantity_filled":13.0,"vwap":0.84,"status":"OPEN"},
    ...
  ]
}
```

Bids are descending by price, asks ascending. `quantity_filled` is the
already-filled portion; remaining = `quantity - quantity_filled`. Use
`trader_id` to distinguish market-maker orders (e.g. `VarKu` parks 1000-lot
quotes at 0.68 / 0.92 in this sim) from student flow.

### `GET /securities/tas?ticker=BNZ&limit=N`

Time-and-sales (printed trades). Returned empty during an idle window;
shape not yet verified. Use `/securities/history` for OHLC candles if
TAS is sparse.

### `GET /securities/history?ticker=BNZ&limit=N`

Per-tick OHLC candles.

```json
[{"tick":377,"open":0.84,"high":0.84,"low":0.84,"close":0.84}, ...]
```

Sorted most recent first.

### `GET /orders`, `GET /orders?status=OPEN`

My own orders. Empty array when I have none working. `status` can be
`OPEN`, `CANCELLED`, `TRANSACTED`.

### `POST /orders`

Not probed (placing would move the market). Per Swagger: body params
`ticker`, `type` (`MARKET` or `LIMIT`), `quantity`, `action` (`BUY`/`SELL`),
`price` (required for `LIMIT`), `dry_run` (optional test flag).

### `DELETE /orders/{id}` and `POST /commands/cancel`

Cancel a single order, or bulk cancel. Not probed.

### `GET /assets`

Returned `[]` — likely used for non-equity instruments in other cases.

### `GET /news?limit=N`

News feed. **The bot's primary event source** — harvests and clock warnings
all land here. Sorted most recent first.

```json
[{"news_id":10,"period":1,"tick":359,"ticker":"","headline":"Three minutes left to trade","body":""},
 {"news_id":9,"period":1,"tick":299,"ticker":"","headline":"H2 - 33 Beans","body":""},
 {"news_id":8,"period":1,"tick":289,"ticker":"","headline":"Personal harvest 2 numbers released in 10sec","body":"Market will not pause..."},
 ...]
```

## Observed news timeline (Citadel Beans, one period = 540 ticks)

| Tick | Headline                                          | Meaning                                             |
|------|---------------------------------------------------|-----------------------------------------------------|
| 1    | `WELCOME TO BEANZ` (body: `Number of players is 78`) | Sim start                                          |
| 50   | `Personal harvest 1 numbers released in 10sec`    | H1 imminent (10-sec warning, no trading halt)       |
| 60   | `H1 - <qty> Beans`                                | Personal H1 lands; `<qty>` auto-credited to `BNZ`   |
| 120  | `Seven minutes left to trade`                     | Clock marker                                        |
| 170  | `Release in 10sec, no trading halt`               | Aggregate release warning                           |
| 180  | `** Harvest One **` (body: `Total Harvest is <N>`)| Market-wide H1 total across 78 players              |
| 240  | `Five minutes left to trade`                      | Clock marker                                        |
| 289  | `Personal harvest 2 numbers released in 10sec`    | H2 imminent                                         |
| 299  | `H2 - <qty> Beans`                                | Personal H2 lands; `<qty>` auto-credited            |
| 359  | `Three minutes left to trade`                     | Clock marker                                        |
| 540  | (period end — settle winter)                      | Shortfall auto-bought from govt @ $1                |

### Parsing hooks

- Personal harvest headline regex: `^H(\d+) - (\d+) Beans$` → group 1 =
  harvest index (1 or 2), group 2 = personal quantity credited.
- Aggregate harvest headline: `** Harvest One **` / `** Harvest Two **`
  with body `Total Harvest is <N>`. The aggregate tells you how much
  supply exists market-wide — useful for refining the expected winter
  buyback price `Q` instead of using the nominal $1.
- Clock warnings (`Seven/Five/Three minutes left to trade`) are human
  hints — safe to ignore in automation; the tick/clock is in `/case`.
- `news_id` monotonically increases. Track the highest seen id and only
  process strictly greater on each poll.

## Gotchas

- **Harvests auto-credit.** The `H<n> - <qty> Beans` news is informational;
  the qty is already in the `BNZ` position. Do NOT treat it as a buy signal
  or you'll double-count.
- **Endowment prints.** Personal harvests appear in the transaction log as
  `ENDOWMENT` (per news body at tick 50/289), not as trades. They will not
  show up in `/securities/tas`.
- **Market doesn't pause** for harvest releases. Price can gap the instant
  the release hits; if you're resting orders, consider cancelling just
  before the 10-sec warning and re-quoting after.
- **$1 buyback is a hard ceiling**, not a floor — paying above $1 is
  strictly dominated by waiting for the government. Market prices in this
  sim have traded between ~$0.50 and ~$0.95.
- **`position` can be a float** in the JSON even though trades are
  integer-denominated. Cast/round defensively.
- **Empty side of book.** `bid` or `ask` can come back as `0` with
  `bid_size`/`ask_size` also `0`. Don't compute a mid from that.

## Housekeeping for the bot loop

A safe polling cadence given the 20 orders/sec cap and the "info every
other minute" case rhythm:

1. `GET /case` — if not `ACTIVE`, sleep and retry.
2. `GET /securities` — snapshot position and top-of-book.
3. `GET /news?limit=20` and filter by `news_id > last_seen`.
4. Reconcile: parse H1/H2 headlines → update `n_harvests_remaining`.
5. Value with `optimal_trade(P=mid, x=position, n=remaining, Q=<est>)`.
6. If `|q|` > threshold, place a `LIMIT` order near touch; otherwise rest.
7. Sleep a few seconds and loop.
