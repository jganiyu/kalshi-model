# Research Platform Audit

This app already has useful safety and observability foundations, but the
historical research workflow needed a cleaner boundary from live execution.

## Solid

- BRTI is treated as the canonical live Bitcoin reference for dashboard,
  threshold, and Texas logic.
- Demo and Live execution paths have explicit reconciliation, kill-switch,
  arming, execution-owner, and reduce-only protection controls.
- Forecast display and trade assessment are conceptually separate: a model
  estimates probability, while decision logic checks executable price after
  fees and slippage.
- The database preserves many operational events, fills, account snapshots,
  trade-review points, and configuration snapshots.
- Existing training uses time-ordered forward validation and promotion gates
  instead of blindly replacing the active model.

## Refactor

- `AnalysisEngine` still coordinates live data, feature hydration, dashboard
  state, training summaries, strategy logic, and execution wiring. Future work
  should move research-only computation out of the live loop.
- Legacy tables such as `btc_ticks`, `kalshi_snapshots`, and
  `signal_snapshots` are useful for the app, but their semantics are not
  strict enough for reusable historical research. Use `research_observations`
  and `research_feature_values` for new experiments.
- `BacktestService` is a quick production-history replay. It should remain a
  diagnostic, not the primary research lab.
- Feature definitions should migrate from broad JSON payloads in signal rows
  toward isolated feature definitions with versioned values and source row IDs.

## Remove Or Retire

- Do not promote shadow or exploratory features into execution without a
  stored hypothesis, baseline comparison, and out-of-sample validation.
- Avoid strategy-specific research logic inside production strategy modules.
  New signal work should start in `app.research`.
- Do not use settlement, final market state, or future order-book observations
  as feature inputs at an earlier timestamp.

## New Research Spine

Migration 33 adds a production-isolated research namespace:

- `research_observations`: source-qualified point-in-time observations with
  `dataset`, optional `market_ticker`, `instrument`, `observed_at`, `source`,
  `source_observed_at`, `received_at`, `available_at`, `ingested_at`,
  `price_type`, `value`, `source_sequence`, and provenance.
- `research_data_quality_events`: explicit missing/stale/gap/disagreement
  evidence.
- `research_feature_definitions`: small, replaceable feature contracts.
- `research_feature_values`: versioned feature values with as-of timestamps and
  source observation IDs.
- `research_market_timeline_points`: one-second market-relative timelines for
  KXBTC15M open and settlement windows.
- `research_hypotheses`: signal ideas treated as hypotheses.
- `research_experiments`: immutable split boundaries, baselines, metrics, and
  parameters.

`app.research.ResearchLab` provides the first API over those tables:

- Ingest observations once with validated price types and provenance.
- Ingest through a source-adapter protocol so CoinAPI, Binance, Bybit, OKX,
  CME, or options vendors can normalize payloads without changing the research
  engine.
- Run quality checks for stale prices, timestamp gaps, duplicate events,
  out-of-order timestamps, impossible time relationships, source disagreement,
  instrument mismatch, and missing or malformed provenance.
- Compute point-in-time return features using `available_at`, not merely the
  source event timestamp.
- Build market timelines so external observations can be aligned to
  `market open`, `T+1s`, ..., `T+14:59`, settlement, and official outcome.
- Register hypotheses.
- Evaluate incremental Brier improvement against a baseline probability feature
  across explicit train, validation, and test boundaries.

## External Data Contract

Every external source adapter should emit `ResearchObservation` rows. Treat
timestamps as separate facts:

- `observed_at`: when the exchange/vendor event happened.
- `source_observed_at`: optional original timestamp embedded by the source when
  distinct from normalized `observed_at`.
- `received_at`: when the collector received the event or REST response.
- `available_at`: the earliest time the research system is allowed to use the
  observation in a point-in-time feature.
- `ingested_at`: when the local database stored the normalized row.

For WebSocket data, `received_at` and `available_at` are usually near the event
time. For REST backfills, `observed_at` can be far earlier than `available_at`;
features at time T must exclude that row until `available_at <= T`.

Provenance must identify the adapter and should preserve vendor, venue, symbol,
channel, file/page, sequence/event ID, and any raw-source reference needed to
rebuild the row. If feeds disagree, preserve both observations and let quality
events record disagreement.

## Signal Lifecycle

A new signal should progress through this path:

1. Raw vendor payload is normalized by a `ResearchSourceAdapter`.
2. Normalized observations are stored in `research_observations`.
3. Data-quality checks record gaps, stale rows, duplicates, time anomalies, and
   source disagreement.
4. A small feature is registered in `research_feature_definitions`.
5. Point-in-time feature values are written to `research_feature_values` with
   source row IDs.
6. A hypothesis is registered.
7. The hypothesis is compared against a simple baseline with explicit
   train/validation/test boundaries.
8. The result is persisted in `research_experiments`.

This is intentionally a laboratory, not a trading bot. "No trade" remains a
valid downstream outcome because research evaluates probability quality before
execution economics.
