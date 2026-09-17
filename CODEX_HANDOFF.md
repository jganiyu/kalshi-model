# Kalshi Model — Codex Handoff

Read this before making changes. It is a working map, not trading advice.

## Product and safety

- macOS dashboard and local API for Kalshi BTC 15-minute markets.
- Never place Live or Demo orders while investigating or verifying a change unless the user explicitly requests it.
- The app normally runs with `read_only: true`; verify this before runtime checks.
- A failed account reconciliation blocks new entries. Known positions retain their reduce-only protective-exit path.
- Use `apply_patch` for source edits. After user-facing changes: test, commit, push, rebuild, reinstall, and verify the running app.

## Current data architecture

- **BRTI / CF Benchmarks via Kalshi authenticated WebSocket** is the sole live market reference.
  It drives the dashboard BTC chart, threshold distance, Texas logic, and next-threshold estimate.
- **Coinbase** is retained only for closed one-minute candles and 15-minute realized volatility.
- Kalshi market WebSocket supplies executable contract quotes and order books. REST is its fallback.
- Historical Kalshi executable quotes are stored so the dashboard BTC chart can display a read-only crosshair.
- Historical research has a separate lab namespace (`research_observations`,
  `research_feature_values`, `research_hypotheses`, `research_experiments`,
  and related quality tables). New signal work should use `app.research`
  rather than production strategy/execution tables.
- External research observations distinguish source event time, receive time,
  availability time, and ingestion time. Point-in-time features must use
  `available_at <= T`, which protects against later REST/backfill data leaking
  into earlier market states.
- Future high-frequency data sources should enter through the
  `ResearchSourceAdapter` protocol and preserve both source-specific
  provenance and optional `market_ticker` association to KXBTC15M timelines.
- `docs/research-platform-audit.md` summarizes what is solid, what should be
  refactored, and what should be retired from the research path.

## Research platform guardrails

- Treat signals as hypotheses. A feature should show incremental predictive
  value against a simple baseline before it influences any execution path.
- Keep train, validation, and test boundaries explicit and persisted with each
  experiment.
- Every historical observation used for research should have source,
  observation time, ingestion time, price type, and provenance.
- Use point-in-time/as-of feature values. Do not let settlement or future book
  state enter earlier feature rows.
- Probability forecasting and execution economics remain separate. The model
  answers probability; execution decides whether a quoted price has edge after
  costs. No trade is always an acceptable outcome.

## Texas Hold’em 3.0

- Entry: wait for the first post-open BRTI threshold breach, then buy the opposite side at an all-in executable price of 45¢ or less.
- Timing: the breach and any fresh-quote IOC retries must occur within 90 seconds of the official market open.
- Identity: new rounds persist as `TEXAS_HOLDEM_3_0`; older Texas positions retain their saved strategy version and protection.
- Current volatility signal: **Coinbase 15-minute realized volatility**. MVI2 is retired from active strategy use; historic records remain for old-trade review.
- Defaults, editable per Paper/Demo/Live:
  - Entry gate: `>= 0.20%`
  - Allocation boost trigger: `>= 0.80%`
  - Boost size: `1.5x`
  - Thesis-loss rule: five minutes after first fill, exit when BRTI is more than $50 unfavorable after zero crossings or exactly two crossings; three or more crossings keep playing.
- The Dashboard Volatility view charts the same 15-minute Coinbase RV signal used by Texas. It is not a second volatility calculation.
- A solid red `Texas gate <value>%` line is always drawn and is included in the Y-axis range.

## Dashboard behavior

- Chart spans: 15m, 1h, 3h, 1d.
- BTC chart: drag/hover crosshair; 15m snaps to 15 seconds, broader views to one minute. Tooltip shows saved historical Up and Down bids, or `--` when no quote was recorded.
- Volatility chart: Coinbase 15-minute realized volatility at each past minute; no legacy composite-volatility series.
- Connection HUD:
  - Green BRTI: fresh official reference.
  - Yellow Kalshi market: REST fallback/reconnecting.
  - Yellow Kalshi account: account REST reachable but still reconciling.
  - Coinbase volatility is green for ready closed candles, yellow for loading/stale, red only for an actual worker error.

## Reconciliation fix (important)

The prior failure loop was application self-contention: reconciliation launched five background account reads concurrently, but the authenticated request controller deliberately admits one background read at once. Queued reads hit a local admission timeout before reaching Kalshi.

Current behavior in `app/services/broker.py`:

- Reconciliation reads serially: balance → orders → positions → fills → settlements.
- Positive account facts are persisted as they arrive, but the account is marked reconciled only after the full authoritative sweep succeeds.
- The execution lane remains reserved between account reads.

Do not “fix” future timeouts by simply widening limits or restoring parallel reads; that defeats the execution-reservation design.

## Verification checklist

1. Run focused tests relevant to the change. Common files:
   - `tests/test_trading_brokers.py`
   - `tests/test_broker_projection_resilience.py`
   - `tests/test_kalshi_request_controller.py`
   - `tests/test_historical_realized_volatility.py`
   - `tests/test_strategy_expansion.py`
2. Build: `scripts/build_macos_app.sh`.
3. Install the signed archive in `/Applications/Kalshi Model.app`; fully quit the old app first.
4. Check `/api/health` and `/api/dashboard` locally.
5. Inspect the actual app UI using CUA for visual/runtime changes.

## Recent commits

- `9a38934` — Texas volatility gate line on dashboard chart.
- `4a7eab7` — Serialize Kalshi account reconciliation reads.
- `a138a7d` — Accurate Coinbase volatility HUD state.
- `265c030` — BTC chart crosshair with historic Up/Down quotes.
- `a1f3bda` — 15m / 1h / 3h / 1d chart spans.
- `38eda42` — Dashboard uses the Texas 15-minute realized-volatility signal.
- `85cd609` — BRTI is canonical live market price.

## Practical starting point for a new chat

1. Read this file, then `README.md`.
2. Run `git status --short`; preserve unrelated user changes.
3. Query `/api/health` before diagnosing connectivity.
4. For a user report of a missed order or exit, inspect stored trade/audit evidence first; do not infer the cause from the dashboard label alone.
