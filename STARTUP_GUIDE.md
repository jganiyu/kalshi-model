# Startup Guide

Chief, your objective is simple: determine whether Kalshi has mispriced the next
15 minutes of Bitcoin. The app handles the probability work. You decide whether
the evidence deserves your attention.

## Launch

Open Terminal and run:

```bash
cd /Users/jonathanganiyu/Coding/kalshi-model
./start.sh
```

The dashboard will open at [http://127.0.0.1:8765](http://127.0.0.1:8765).
The first launch may take a minute while local dependencies and historical data
are prepared.

When the sidebar says **Streaming live**, BTC and dashboard updates are arriving
through the live feed. Kalshi prices also stream once the Key ID is configured.

## Read The Dashboard

- **BTC vs threshold:** Where Bitcoin is relative to the settlement target.
- **Model vs market:** Our estimated YES probability compared with Kalshi's price.
- **Edge and EV:** Whether the difference survives spread, fees, and slippage.
- **Signal:** `TRADE YES`, `TRADE NO`, or, most often, `NO TRADE`.
- **Position:** A conservative paper amount based on the configured bankroll.

Confidence measures data quality, calibration, model agreement, liquidity, and
spread. It is not a measure of how dramatic the number looks. Convenient, I know.

## Four Stations

- **Dashboard:** Live contract, chart, decision, and upcoming market.
- **Calibration History:** Whether past probabilities matched actual outcomes.
- **Paper Trading:** Simulated positions, bankroll, P&L, and drawdown.
- **Settings:** Bankroll, edge threshold, Kelly fraction, risk limits, and backups.

## Rules Of Engagement

This system cannot place real trades. It uses public read-only data and records
paper positions locally in SQLite.

If you see `NO TRADE - Data Unreliable`, hold position. A stale feed, missing ask,
market transition, or exchange disagreement has made the estimate unsafe.

To shut the app down, return to Terminal and press `Control-C`.

That's it, Chief. Watch the price, respect the edge, and don't confuse luck with
calibration.
