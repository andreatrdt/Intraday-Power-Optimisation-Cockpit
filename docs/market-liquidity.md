# Intraday Market & Executable Liquidity

Milestone 1C is descriptive and diagnostic. It does not recommend a trade, place
an order, dispatch a battery, or value BM or ancillary-service optionality.

## Provider policy

`market_intraday` remains the separately configured licensed executable market
adapter. Without credentials it is `ERROR / MISSING`, publishes no order-book
values, and explicitly rejects Elexon MID or reference prices as executable bids
or asks.

`market_order_book_sample` is a distinct `SAMPLE` adapter used for demonstrations.
The market layer selects one complete provider and never mixes price levels from
different providers. A fresh live provider takes precedence over sample data, but
an unavailable live provider is still shown as `ERROR`; activating the sample book
does not change or conceal that state.

## Execution calculation

- Negative exposure is short and maps to a buy hedge using asks.
- Positive exposure is long and maps to a sell hedge using bids.
- Exposure within 0.05 MWh is flat and requires no hedge.
- Buy levels are swept from lowest ask upward.
- Sell levels are swept from highest bid downward.
- The default diagnostic depth is the first three economic price levels.

```text
WAP = sum(filled volume_i * price_i) / sum(filled volume_i)
unfilled = required hedge volume - executable volume
```

Estimated hedge cashflow is positive for a sale and negative for a purchase. It
uses executable volume only; unfilled volume is reported separately.

## Gate Closure

The current GB BSC Gate Closure boundary is represented as one hour before the
start of each settlement period. The offset is a named service constant, not a UI
assumption. `APPROACHING` means 30 minutes or less remain, while `CLOSED` means the
boundary has passed.

## Readiness

- `READY`: complete bid/ask prices and depth are fresh, live and valid.
- `DEGRADED`: the book is sample-labelled or stale but remains calculable.
- `BLOCKED`: executable bids, asks or depth are missing or invalid.

## API

- `GET /api/v1/market-liquidity`
- `GET /api/v1/market-liquidity/{snapshot_id}`
- `GET /api/v1/markets/current`
- `GET /api/v1/lineage/{value_id}` for book inputs, spread, depth, WAP,
  executable/unfilled volume, liquidity score and hedge cashflow
