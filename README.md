# Kalshi Model

A local Mac application for analyzing Kalshi's `KXBTC15M` Bitcoin 15-minute
Above/Below markets. It estimates settlement probability, compares that estimate
with executable Kalshi prices, logs every material signal, and forward-tests the
decision system with realistic paper trades.

**It cannot place real orders.** The code contains no authenticated trading client
and makes no order requests. This is research software, not financial advice.

## Start on a Mac

Requirements: macOS, Python 3.11 or newer, and an internet connection.

```bash
cd /Users/jonathanganiyu/Coding/kalshi-model
./start.sh
```

The first launch creates `.venv`, installs the pinned dependencies, initializes
SQLite, starts the collector, and opens [http://127.0.0.1:8765](http://127.0.0.1:8765).
Stop it with `Control-C` in Terminal. Later launches reuse the virtual environment.

No API key is required. Kalshi's public market and order-book endpoints, Coinbase
Exchange, Kraken, and Bitstamp are all read-only and free. Optional environment
settings are listed in `.env.example`; `start.sh` does not load that file
automatically, so export any override before launch.

## What it does

- Builds a live median BTC reference from Coinbase, Kraken, and Bitstamp.
- Finds the current and upcoming `KXBTC15M` contracts through Kalshi's public API.
- Models the actual settlement condition: the closing 60-second average of CF
  Benchmarks BRTI must be at least the opening 60-second average/target.
- Starts with an interpretable distance-to-threshold/volatility probability model.
- Uses executable YES/NO asks, current taker fees, configurable slippage, and
  available ask size before showing a trade signal.
- Defaults to no trade unless positive EV and the configured executable-edge margin
  both survive costs.
- Sizes paper positions with capped fractional Kelly and enforces bankroll,
  per-trade, position, and session-drawdown limits.
- Stores BTC ticks, Kalshi snapshots, material signals, model versions, settlements,
  calibration reports, backtests, settings, and paper trades in local SQLite.
- Retrains a regularized logistic candidate with expanding-window one-step-forward
  validation. Promotion requires at least 40 settled observations, a Brier-score
  gain of at least `0.005`, and no material calibration-error regression.

## Data and settlement caveat

Kalshi settles `KXBTC15M` from CF Benchmarks' BRTI, not from any single exchange.
There is no assumed free historical BRTI order-book feed in this project. Live
analysis therefore uses a robust multi-exchange spot proxy and adds basis
uncertainty. If fewer than two feeds respond, exchange dispersion is too high,
the contract is transitioning, or an executable Kalshi ask is absent, the app
shows `NO TRADE — Data Unreliable`.

The automatic bootstrap uses recent Kalshi one-minute market candlesticks and
Coinbase one-minute BTC candles at a fixed point five minutes before settlement.
It never fills missing fields with invented data and skips incomplete observations.
Historical Kalshi candlesticks do not provide full order-book depth, so bootstrap
rows do not pretend that feature exists. Forward live collection gradually replaces
that limitation with proprietary local snapshots.

## Current API assumptions

Verified against live endpoints on August 23, 2026:

- Production API: `https://external-api.kalshi.com/trade-api/v2`
- Series: `KXBTC15M`
- Market discovery: `GET /markets?series_ticker=KXBTC15M&status=open`
- Order book: `GET /markets/{ticker}/orderbook`
- Current general taker fee: `ceil-to-$0.0001(0.07 × contracts × price × (1-price))`
- Settlement: 60 BRTI observations averaged immediately before each boundary,
  rounded to two decimals

Primary references:

- [Kalshi public market-data quick start](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Kalshi market schema](https://docs.kalshi.com/api-reference/market/get-markets)
- [Kalshi order-book semantics](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Kalshi historical data](https://docs.kalshi.com/getting_started/historical_data)
- [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [Kalshi fee schedule](https://kalshi.com/fee-schedule)
- [Coinbase public ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker)
- [Kraken Spot REST API](https://docs.kraken.com/api/docs/rest-api/get-ticker-information)
- [Bitstamp API](https://www.bitstamp.net/api/)

API schemas and fee schedules can change. Review these references before relying on
results after an update; the Settings page exposes the configured series and local
data health.

## Pages

`Dashboard` shows the contract, composite BTC chart, threshold, model/market
probabilities, edge, after-cost EV, confidence, and paper size. `Calibration History`
keeps dated reports and bucket reliability. `Paper Trading` separates forward results
from backtests. `Settings` controls bankroll, decision thresholds, risk limits,
slippage, bootstrap, and SQLite backups.

The database defaults to `data/kalshi_model.db`. Backups are written to
`data/backups/`. These files stay on the Mac and are ignored by Git.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests cover probability behavior, fee and EV math, symmetric YES/NO decisions,
Kelly caps, stale-data and drawdown gates, SQLite migration/persistence, paper
settlement, calibration metrics, and time-ordered model promotion.

## Architecture

The app is intentionally small: one FastAPI process, an asynchronous polling loop,
vanilla browser UI, NumPy for the candidate model, and SQLite in WAL mode. There are
no cloud services, paid feeds, containers, queues, or background daemons. API
interruptions do not stop the process; they suppress signals until critical data is
healthy again.
