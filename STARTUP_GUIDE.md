# Startup Guide

Use the app to separate two questions: what Bitcoin is likely to do, and whether
the contract price offers a worthwhile trade.

## Launch

Open Terminal and run:

```bash
cd /path/to/kalshi-model
./start.sh
```

The dashboard will open at [http://127.0.0.1:8765](http://127.0.0.1:8765).
The first launch may take a minute while local dependencies and historical data
are prepared.

When the sidebar says **Streaming live**, BTC and dashboard updates are arriving
through the live feed. Kalshi prices also stream once the Key ID is configured.

## Read the dashboard

- **BTC vs threshold:** Where Bitcoin is relative to the settlement target.
- **Outcome forecast:** Likely Up, Uncertain, or Likely Down.
- **Trade assessment:** Price, edge, EV, fees, and slippage for the selected side.
- **Standard Edge HUD:** The probability, EV, confirmation, and safety gates still needed before entry.

Forecast direction alone never places an order.

## Trading modes

- **Paper:** Simulated locally and enabled by default.
- **Demo:** Uses a separate write-enabled Demo key and Kalshi's test account.
- **Live:** Uses a separate Live key and real funds.

Live requires successful Demo verification, reviewed hard limits, typed session
arming, and a separate Automatic switch. Arming resets after restart, credential
changes, disconnects, failed reconciliation, or a triggered risk limit.

## Safety

- Orders always use an explicit worst acceptable price and may fill partially.
- The kill switch blocks submissions and attempts to cancel resting orders.
- Mode-specific caps and hard limits apply before every new exposure.
- Stop-losses are optional and off by default.
- Stop-losses and the 99¢ profit take require the app to remain running and connected.
- Reconcile and rearm after every restart or disconnect.

To shut the app down, return to Terminal and press `Control-C`.
