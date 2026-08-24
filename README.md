# Kalshi Model

A local macOS research and paper-trading app for Kalshi's 15-minute Bitcoin Up or Down markets; it never places real-money orders.

> Research only, not financial advice.

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/dashboard-forecast-light.jpg" alt="Kalshi Model dashboard in light mode"></td>
    <td width="50%"><img src="docs/screenshots/dashboard-forecast-dark.jpg" alt="Kalshi Model dashboard in dark mode"></td>
  </tr>
  <tr>
    <td align="center"><strong>Light</strong></td>
    <td align="center"><strong>Dark</strong></td>
  </tr>
</table>

## Features

- **Dashboard:** Live BTC proxy, outcome forecast, Kalshi contract, chart, order books, and paper controls.
- **BTC proxy:** Median Coinbase, Kraken, and Bitstamp price with learned BRTI uncertainty.
- **Paper trading:** Manual, limit, and confirmed automatic entries with per-entry stop-losses.
- **Calibration:** Tune decision, automation, risk, data-quality, and promotion rules in one place.
- **Local data:** Settings, evidence, trades, snapshots, reports, and backups stay in SQLite on your Mac.

## Outcome forecast

![Outcome forecast panel](docs/screenshots/outcome-forecast.jpg)

- **Likely Up:** Up probability is 60% or higher.
- **Uncertain:** Up probability is above 40% and below 60%.
- **Likely Down:** Up probability is 40% or lower.
- The forecast always describes the expected outcome; price, edge, and trade action stay in **Trade assessment**.

## Calibration

![Calibration page](docs/screenshots/calibration.jpg)

- Apply saves one auditable configuration snapshot; Discard and Restore are reversible.
- Results show settled samples, Brier score, and calibration in 10-point probability ranges.
- Automatic entries require a valid sustained trade decision; the outcome forecast never opens a position by itself.

## Kalshi credentials

![Kalshi credentials setup](docs/screenshots/credentials-setup.jpg)

- Public REST data works without credentials; a Key ID and RSA private key enable live WebSocket prices.
- Add them in **Settings**; the private copy stays under `~/Library/Application Support/Kalshi Model/`.
- Never commit `.env`, your Key ID, or your private key.

<details>
<summary>Developer <code>.env</code> setup</summary>

```bash
cp .env.example .env
```

```bash
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/your-private-key.pem
```

</details>

## How the model works

Think of the model as answering two separate questions: **What is likely to happen?** and **Is the contract worth its price?**

### Outcome forecast

The model combines live Bitcoin prices, distance from the threshold, time remaining, volatility, momentum, and past settlement behavior to estimate the chance of Up.

- **Likely Up:** 60% or higher.
- **Uncertain:** Between 40% and 60%.
- **Likely Down:** 40% or lower.

### Trade assessment

The app then compares that probability with the contract's available price after estimated fees and slippage.

A likely outcome can still be overpriced, while an unlikely outcome can be underpriced; selecting Up or Down never changes the forecast.

### Safety and learning

Automatic paper entries require a valid price, enough win probability and edge, signal confirmation, liquidity, and risk approval.

Calibration tracks how often each probability range was correct, and a trained model replaces the baseline only after proving more accurate on settled markets.

### Using it well

- Separate forecast from price: a likely outcome can be a bad trade, while an unlikely outcome can be underpriced.
- Treat probability as uncertainty, not certainty; even a well-calibrated 70% forecast should lose about 3 times in 10.
- Judge the model across many settled markets, not a short winning or losing streak.
- Paper trade new settings before relying on them.

## Download

[**Download the latest Apple Silicon Mac app**](https://github.com/jganiyu/kalshi-model/releases/latest/download/Kalshi-Model-macOS-arm64.zip), move it to Applications, then right-click **Open** on first launch.

## Run from source

```bash
git clone https://github.com/jganiyu/kalshi-model.git
cd kalshi-model
./start.sh
```

Requires macOS and Python 3.11 or newer; the app opens at [http://127.0.0.1:8765](http://127.0.0.1:8765).

## Test

```bash
.venv/bin/python -m pytest
```

## Build the Mac app

```bash
./scripts/build_macos_app.sh
```

The signed local app and ZIP are written to `dist/`.
