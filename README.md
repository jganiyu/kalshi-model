# Kalshi Model

Kalshi Model is a local Mac application for analyzing Kalshi's 15-minute Bitcoin
Up or Down markets (`KXBTC15M`). It combines live Bitcoin and Kalshi data, estimates
the probability of an Up settlement, compares that estimate with executable market
prices, and tests the decision logic through paper trading.

The app cannot place real Kalshi orders. It is a research and simulation tool, not
financial advice.

## Features

- Live Bitcoin composite built from Coinbase, Kraken, and Bitstamp.
- Fluid chart with the current Kalshi threshold, BTC price, delta, and market timer.
- Live Up and Down order books.
- Model probability, Kalshi probability, edge, expected value, and confidence.
- `Buy Up`, `Hold`, and `Buy Down` model signals.
- Manual market and limit paper orders using live executable prices.
- Optional automatic signal-based paper trading.
- Paper bankroll, positions, open orders, trade history, drawdown controls, and round reset.
- Calibration history with reliability buckets and model-version tracking.
- Light and dark themes.
- Local SQLite storage and local database backups.

## How the model works

1. The app takes the median of the available BTC exchange prices to reduce the
   effect of a single unusual feed.
2. It measures BTC's distance from the Kalshi threshold, time remaining, recent
   volatility, short-term momentum, and uncertainty between the spot composite and
   Kalshi's BRTI settlement source.
3. The cold-start model converts that volatility-adjusted distance into an Up
   probability. Its estimate becomes more sensitive to the threshold as settlement
   approaches.
4. Once enough settled observations exist, the app can use a regularized logistic
   model. Its features include distance, time, volatility, momentum, recent range,
   volume acceleration, exchange dispersion, order-book imbalance, and Kalshi's
   market probability.
5. The app evaluates both Up and Down using the current ask, configured slippage,
   and Kalshi's taker fee. It only produces a buy signal when expected value is
   positive and the executable probability edge clears the configured minimum.
6. Suggested paper size uses capped fractional Kelly sizing plus bankroll,
   per-trade, position, liquidity, and session-drawdown limits.

Missing or stale feeds, excessive exchange dispersion, a transitioning contract,
or an unavailable executable price cause the app to hold instead of forcing a
decision. Model direction remains visible when paper-trading risk controls pause
execution.

## How calibration improves over time

After a market settles, the app pairs its result with the latest stored model
prediction for that contract. It then measures:

- **Brier score:** the average squared error of the probability forecasts. Lower is
  better.
- **Calibration error:** the difference between predicted probability and actual
  outcomes within probability buckets. Lower is better.

Candidate training begins after 12 settled observations. The candidate is a
regularized logistic model trained on up to the latest 1,000 contracts and tested
with expanding-window, one-step-forward validation, so future outcomes never enter
an earlier training fold.

A candidate can replace the active model only when all of these are true:

- At least 120 settled contracts are available.
- Those contracts cover at least 7 UTC days.
- Candidate Brier score improves by at least `0.005` on the comparison window.
- Candidate calibration error is no more than `0.01` worse than the incumbent.

If a candidate fails those checks, the current model stays active. Calibration runs
after each of the first 20 stored settlements and then when a new settlement arrives
on a UTC day without a report. Automatic or manual historical bootstrap also updates
the report. Resetting a paper-trading round does not erase model evidence or
calibration history.

## Run on a Mac

Requirements:

- macOS
- Python 3.11 or newer
- Internet connection

```bash
git clone https://github.com/jganiyu/kalshi-model.git
cd kalshi-model
./start.sh
```

The first launch creates a virtual environment, installs dependencies, creates the
local database, and opens [http://127.0.0.1:8765](http://127.0.0.1:8765). Later
launches reuse the same environment. Stop the app with `Control-C` in Terminal.

## Optional Kalshi streaming credentials

Public REST data works without credentials. Fluid Kalshi WebSocket updates require
your own Kalshi API Key ID and RSA private key.

```bash
cp .env.example .env
```

Then set these values in `.env`:

```bash
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/your-private-key.pem
```

Keep the private key outside this repository and restrict it with `chmod 600`.
Never commit `.env` or the private key.

## Local data

Each installation creates its own `data/kalshi_model.db`. Market history, model
evidence, settings, paper trades, and backups remain on that Mac and are ignored by
Git. A fresh clone therefore starts with a fresh paper account and builds its own
calibration history.

Kalshi settles these markets from CF Benchmarks BRTI, while this app uses a
multi-exchange spot composite as a live proxy. The model includes basis uncertainty,
but the two sources can still differ.

## Tests

```bash
.venv/bin/python -m pytest
```

The test suite covers probability and fee math, decision gates, risk controls,
paper-order execution and persistence, settlement, calibration, model promotion,
and streaming order-book behavior.
