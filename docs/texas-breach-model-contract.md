# Texas breach model contract

Version: `texas-breach-reference-1`. This release repairs the live volatility
measurement and exposes a causal, diagnostic crossing reference. Existing Texas
2.0 gate, boost, phase targets/stops and thesis exits retain their saved rules.
The reference does not authorize orders or alter allocation.

## Event and inputs

Estimate a **future BTC proxy touch of the current market's fixed threshold**
between observation and expiry. This is different from official BRTI touching,
winning settlement, or obtaining a profitable executable Kalshi bid. The next
threshold forecast belongs to a different market and is excluded. The persisted
post-fill breach latch remains authoritative for the existing thesis exit.

Inputs are current distance in dollars, corrected dollar volatility per square
root second, actual time remaining, source-qualified trailing 60-second price
slope directed toward the line, MVI, reversal rate, observation coverage and source
age. Every observation must have been available by the decision timestamp.
The supplied price timestamp must belong to that price; every historical direction
sample must individually pass source quality. Output retains the raw price,
threshold and timestamp plus direction span, largest gap and window coverage.
Reject unknown clocks, stale sources and unreliable volatility; missing direction
remains missing. Do not replace missing evidence with a neutral score.

## Interpretable reference

Let `d = abs(BTC - threshold)`, `s` be dollars per square-root second and `T`
seconds remaining. Expected movement is `s * sqrt(T)` and normalized distance is
`d / (s * sqrt(T))`. A driftless Brownian reference gives:

`P(touch by expiry) = erfc(d / (sqrt(2) * s * sqrt(T)))`.

This follows the [reflection principle](https://web.stanford.edu/~montanar/TEACHING/Stat310B/NOTES/amir_notes.pdf).
BTC is not assumed to satisfy its distribution in practice; output is explicitly
uncalibrated. MVI and reversal overlap with volatility and are retained as features
without invented additive weights. A second, explicitly constant-trend scenario
uses the measured toward-threshold slope `mu`:

`P_trend = Phi((mu*T-d)/(s*sqrt(T))) + exp(2*mu*d/s^2)*Phi((-mu*T-d)/(s*sqrt(T)))`.

The [constant-drift first-passage result](https://www.stat.uchicago.edu/~yibi/teaching/stat317/2021/Lectures/Lecture25.pdf)
is numerically evaluated without overflowing its exponential. Continuing a
60-second slope through the remaining round is an audit-only sensitivity assumption; its
incremental predictive value still requires replay. The earlier conversational logistic
equation described a candidate learned model, not fitted coefficients.

## Validation and next decision

Replay all eligible rounds chronologically using only data available at each
candidate entry, with stable market/round IDs rather than intents or fill count.
Treat missing crossing observations as unknown, not proof of no crossing. Split
train/test by contiguous time, keep all observations of a round in one partition,
fit preprocessing only on training data and compare against the reference and
existing MVI rule. Report sample counts, coverage, calibration/Brier score,
discrimination and uncertainty. Historical outcome labels must not mix proxy
touch with executable target hits.

Measure time-to-touch, time-to-executable-target, net fees/P&L, drawdown and losses
on no-touch rounds separately. Replay every candidate, including untraded rounds,
to expose selection bias. Any eventual learned weights, gate or sizing transition
must be versioned and supported by held-out results. Sparse historical data can
justify further evidence collection but cannot justify a claimed calibrated win
probability. Gate/boost promotion remains a distinct reviewable strategy change.

## Integration and runtime requirements

Compute from the analysis worker's existing corrected snapshot and bounded recent
sample buffer; no additional exchange calls or hot-path database scans. Include
the object in analysis/dashboard data and durable existing decision evidence.
Keep it independent of the protective-exit worker. Show a compact label such as
`Proxy touch reference (uncalibrated)` with missing data shown as `--`. Preserve
Paper/Demo/Live isolation and avoid a green/red trading recommendation derived
from an unvalidated percentage.
