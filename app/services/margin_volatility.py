from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from app.db import Database
from app.domain import parse_time


CALCULATION_VERSION = "mvi-2"
WINDOW_SECONDS = 30 * 60
SAMPLE_SECONDS = 5
EXPECTED_CHANGES = WINDOW_SECONDS // SAMPLE_SECONDS
MINIMUM_COVERAGE = 0.80
MINIMUM_BASELINE_SAMPLES = 30
# Components are dollars / sqrt(second), so preserve the former 25-cent
# five-second threshold in the same units instead of silently changing the
# reversal filter while correcting elapsed-time normalization.
NOISE_FLOOR_DOLLARS_PER_SQRT_SECOND = 0.25 / math.sqrt(SAMPLE_SECONDS)


def historical_percentile_index(
    raw_score: float,
    baseline: Iterable[float],
    *,
    minimum_samples: int = MINIMUM_BASELINE_SAMPLES,
) -> float | None:
    """Map a raw score to a mid-rank historical percentile on a 0-10 scale."""
    values = [float(value) for value in baseline if math.isfinite(float(value))]
    if len(values) < minimum_samples or not math.isfinite(raw_score):
        return None
    tolerance = max(1e-12, abs(raw_score) * 1e-9)
    below = sum(value < raw_score - tolerance for value in values)
    equal = sum(abs(value - raw_score) <= tolerance for value in values)
    percentile = (below + 0.5 * equal) / len(values)
    return max(0.0, min(10.0, percentile * 10.0))


def in_mvi_bucket(value: object, lower: int) -> bool:
    """Return whether a finite MVI is in [lower, lower+1), with 10 in 9-10."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(score) or not 0 <= lower <= 9:
        return False
    upper = lower + 1
    return lower <= score < upper or (lower == 9 and score == 10.0)


def quotes_are_fresh_and_qualified(
    quotes: list[Any], observed: datetime, settings: dict[str, Any],
) -> bool:
    """Require receipt-time provenance for every quote in a composite."""
    minimum = int(settings.get("minimum_exchange_feeds", 2))
    if len(quotes) < minimum:
        return False
    maximum_age = float(settings.get("max_data_age_seconds", 20))
    for quote in quotes:
        value = quote.get("price") if isinstance(quote, dict) else getattr(quote, "price", None)
        receipt = quote.get("observed_at") if isinstance(quote, dict) else getattr(quote, "observed_at", None)
        timestamp = parse_time(receipt)
        try:
            price = float(value)
        except (TypeError, ValueError):
            return False
        age = (observed - timestamp).total_seconds() if timestamp else math.inf
        if not math.isfinite(price) or price <= 0 or age < 0 or age > maximum_age:
            return False
    return True


def historical_source_reliable(
    tick: dict[str, Any], observed: datetime, settings: dict[str, Any],
) -> bool:
    """Validate persisted quote provenance without reconstructing a composite.

    Old ticks may contain only an aggregate count/dispersion.  That is useful
    chart history but cannot certify an mvi-2 source sample because a stale
    venue could have contributed to the recorded composite.
    """
    if (
        int(tick.get("exchange_count") or 0)
        < int(settings.get("minimum_exchange_feeds", 2))
        or float(tick.get("dispersion_pct") or 0)
        > float(settings.get("max_exchange_dispersion_pct", 0.40))
    ):
        return False
    try:
        source = json.loads(str(tick.get("source_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    quotes = source.get("quotes") if isinstance(source, dict) else None
    if not isinstance(quotes, list):
        return False
    return quotes_are_fresh_and_qualified(quotes, observed, settings)


def volatility_components(
    observations: list[dict[str, Any]],
) -> dict[str, float | int | None]:
    """Calculate quality-qualified, elapsed-time-normalized movement.

    Contract changes are deliberately skipped so a new threshold never becomes a
    synthetic price move. The surrounding contract segments still contribute.
    """
    increments: list[tuple[float, float]] = []
    covered_seconds = 0.0
    for previous, current in zip(observations, observations[1:]):
        # An unreliable composite is not allowed to become valid merely
        # because a later quote is good.  Older mvi-1 rows do not have this
        # field; they remain read-only and are never fed into mvi-2 history.
        if (
            ("source_reliable" in previous and not bool(previous.get("source_reliable")))
            or ("source_reliable" in current and not bool(current.get("source_reliable")))
        ):
            continue
        if (
            previous.get("ticker") != current.get("ticker")
            or (
                previous.get("threshold") is not None
                and current.get("threshold") is not None
                and float(previous["threshold"]) != float(current["threshold"])
            )
        ):
            continue
        previous_time = parse_time(previous.get("observed_at"))
        current_time = parse_time(current.get("observed_at"))
        if previous_time is None or current_time is None:
            continue
        elapsed = (current_time - previous_time).total_seconds()
        if not 2.0 <= elapsed <= SAMPLE_SECONDS * 2.5:
            continue
        increments.append((float(current["margin"]) - float(previous["margin"]), elapsed))
        covered_seconds += elapsed

    coverage = min(1.0, covered_seconds / WINDOW_SECONDS)
    if not increments:
        return {
            "raw_realized_volatility": None,
            "movement_intensity": None,
            "reversal_component": None,
            "raw_score": None,
            "coverage": coverage,
            "change_count": 0,
            "reversal_count": 0,
            "reversal_comparisons": 0,
        }

    # First remove the elapsed-time-aware drift.  Dividing raw changes by
    # sqrt(dt) before doing this turns a perfectly constant velocity sampled
    # at irregular intervals into false volatility.
    drift = sum(change for change, _elapsed in increments) / sum(
        elapsed for _change, elapsed in increments
    )
    changes = [
        (change - drift * elapsed) / math.sqrt(elapsed)
        for change, elapsed in increments
    ]
    raw_changes = [change / math.sqrt(elapsed) for change, elapsed in increments]
    robust_center = statistics.median(changes)
    deviations = [abs(change - robust_center) for change in changes]
    mad = statistics.median(deviations)
    robust_scale = max(NOISE_FLOOR_DOLLARS_PER_SQRT_SECOND, 1.4826 * mad)
    winsor_limit = 6.0 * robust_scale
    winsorized = [
        max(robust_center - winsor_limit, min(robust_center + winsor_limit, change))
        for change in changes
    ]
    movement = math.sqrt(
        sum(change * change for change in winsorized) / len(winsorized)
    )

    noise = max(
        NOISE_FLOOR_DOLLARS_PER_SQRT_SECOND,
        statistics.median([abs(v) for v in raw_changes]) * 0.08,
    )
    # The surviving increment list is discontinuous after rejected pairs.
    # Preserve those boundaries so a sign before an outage/market rollover is
    # never called a reversal against a sign after it.
    reversals = 0
    comparisons = 0
    previous_sign: int | None = None
    increment_index = 0
    for previous, current in zip(observations, observations[1:]):
        valid = (
            not (("source_reliable" in previous and not bool(previous.get("source_reliable")))
                 or ("source_reliable" in current and not bool(current.get("source_reliable"))))
            and previous.get("ticker") == current.get("ticker")
            and not (
                previous.get("threshold") is not None
                and current.get("threshold") is not None
                and float(previous["threshold"]) != float(current["threshold"])
            )
        )
        previous_time = parse_time(previous.get("observed_at"))
        current_time = parse_time(current.get("observed_at"))
        elapsed = (
            (current_time - previous_time).total_seconds()
            if previous_time is not None and current_time is not None else 0.0
        )
        valid = valid and 2.0 <= elapsed <= SAMPLE_SECONDS * 2.5
        if not valid:
            previous_sign = None
            continue
        change = raw_changes[increment_index]
        increment_index += 1
        if abs(change) < noise:
            continue
        sign = 1 if change > 0 else -1
        if previous_sign is not None:
            comparisons += 1
            reversals += int(previous_sign != sign)
        previous_sign = sign
    reversal = reversals / comparisons if comparisons else 0.0
    raw_score = movement * (1.0 + 0.75 * reversal)
    return {
        "raw_realized_volatility": movement,
        "movement_intensity": movement,
        "reversal_component": reversal,
        "raw_score": raw_score,
        "coverage": coverage,
        "change_count": len(changes),
        "reversal_count": reversals,
        "reversal_comparisons": comparisons,
    }


def cushion_metrics(
    margin: float,
    raw_realized_volatility: float | None,
    seconds_remaining: float,
) -> tuple[float | None, float | None]:
    if (
        raw_realized_volatility is None
        or not math.isfinite(raw_realized_volatility)
        or raw_realized_volatility <= 0
        or not math.isfinite(seconds_remaining)
        or seconds_remaining < 0
    ):
        return None, None
    expected = raw_realized_volatility * math.sqrt(seconds_remaining)
    if not math.isfinite(expected) or expected <= 1e-12:
        return None, None
    ratio = abs(float(margin)) / expected
    return expected, ratio if math.isfinite(ratio) else None


class MarginVolatilityService:
    def __init__(self, db: Database):
        self.db = db
        # Five-second admission limits writes; the full key makes a changed
        # market or degraded source recompute rather than reuse a safe result
        # from an earlier quote in the same bucket.
        self._last_observation_key: tuple[int, str, float, bool] | None = None
        self._cached_observation: dict[str, Any] | None = None

    def backfill_recent(self, hours: int = 2) -> int:
        """Seed the new metric from recent local history without rewriting it."""
        existing = self.db.fetch_one(
            "SELECT 1 AS present FROM margin_volatility_observations WHERE calculation_version=? LIMIT 1",
            (CALCULATION_VERSION,),
        ) or {}
        if existing.get("present"):
            return 0
        since = (datetime.now(UTC) - timedelta(hours=max(1, hours))).isoformat()
        ticks = self.db.fetch_all(
            """
            SELECT observed_at,composite_price,dispersion_pct,exchange_count,source_json
            FROM btc_ticks WHERE observed_at>=? ORDER BY observed_at ASC
            """,
            (since,),
        )
        markets = self.db.fetch_all(
            """
            SELECT ticker,strike,open_time,close_time FROM markets
            WHERE strike IS NOT NULL AND close_time>=?
            ORDER BY open_time ASC
            """,
            (since,),
        )
        intervals: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for market in markets:
            opened = parse_time(market.get("open_time"))
            closed = parse_time(market.get("close_time"))
            if opened is not None and closed is not None:
                intervals.append((opened, closed, market))
        settings = self.db.settings()
        buckets: dict[int, dict[str, Any]] = {}
        for tick in ticks:
            timestamp = parse_time(tick.get("observed_at"))
            if timestamp is not None:
                buckets[math.floor(timestamp.timestamp() / SAMPLE_SECONDS)] = tick
        history: list[dict[str, Any]] = []
        baseline: list[float] = []
        rows: list[tuple[Any, ...]] = []
        interval_index = 0
        for _bucket, tick in sorted(buckets.items()):
            # The bucket controls admission only.  Retain the actual receipt
            # timestamp so volatility uses real elapsed intervals.
            observed = parse_time(tick.get("observed_at"))
            if observed is None:
                continue
            while interval_index < len(intervals) and observed > intervals[interval_index][1]:
                interval_index += 1
            if interval_index >= len(intervals):
                break
            opened, closed, market = intervals[interval_index]
            if observed < opened or observed > closed:
                continue
            threshold = float(market["strike"])
            proxy = float(tick["composite_price"])
            source_reliable = historical_source_reliable(tick, observed, settings)
            candidate = {
                "observed_at": observed.isoformat(),
                "ticker": str(market["ticker"]),
                "margin": proxy - threshold,
                "threshold": threshold,
                "source_reliable": source_reliable,
            }
            cutoff = observed - timedelta(seconds=WINDOW_SECONDS + SAMPLE_SECONDS)
            history = [
                item for item in history
                if (parse_time(item["observed_at"]) or observed) >= cutoff
            ]
            history.append(candidate)
            components = volatility_components(history)
            coverage = float(components["coverage"] or 0.0)
            window_ready = source_reliable and coverage >= MINIMUM_COVERAGE
            raw_score = components.get("raw_score") if window_ready else None
            mvi = historical_percentile_index(float(raw_score), baseline) if raw_score is not None else None
            seconds_remaining = max(0.0, (closed - observed).total_seconds())
            expected, cushion = cushion_metrics(
                float(candidate["margin"]),
                components.get("raw_realized_volatility") if window_ready else None,
                seconds_remaining,
            )
            reliable = bool(window_ready and mvi is not None)
            state = "RELIABLE" if reliable else "UNAVAILABLE" if not source_reliable else "LEARNING"
            rows.append(
                (
                    observed.isoformat(), market["ticker"], threshold, proxy,
                    candidate["margin"],
                    components.get("raw_realized_volatility") if window_ready else None,
                    components.get("movement_intensity") if window_ready else None,
                    components.get("reversal_component") if window_ready else None,
                    raw_score, mvi, expected, cushion, seconds_remaining, coverage,
                    int(source_reliable), int(reliable), state, CALCULATION_VERSION,
                )
            )
            if raw_score is not None:
                baseline.append(float(raw_score))
                if len(baseline) > 5000:
                    baseline = baseline[-5000:]
        self.db.executemany(
            """
            INSERT OR IGNORE INTO margin_volatility_observations(
                observed_at,ticker,threshold,btc_proxy,margin,
                raw_realized_volatility,movement_intensity,reversal_component,
                raw_score,mvi,expected_remaining_move,cushion_ratio,
                seconds_remaining,coverage,source_reliable,reliable,reliability_state,calculation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        return len(rows)

    @staticmethod
    def gate(settings: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
        maximum = max(0.0, min(10.0, float(settings.get("maximum_margin_volatility", 0))))
        current = (state or {}).get("mvi")
        cushion = (state or {}).get("cushion_ratio")
        if maximum <= 0:
            return {
                "enabled": False,
                "passed": True,
                "current": current,
                "required": maximum,
                "cushion_ratio": cushion,
                "status": "OFF",
                "detail": "The volatility gate is off.",
            }
        reliable = bool((state or {}).get("reliable")) and current is not None
        if not reliable:
            learning = str((state or {}).get("reliability_state") or "LEARNING") == "LEARNING"
            return {
                "enabled": True,
                "passed": False,
                "current": current,
                "required": maximum,
                "cushion_ratio": cushion,
                "status": "LEARNING" if learning else "UNAVAILABLE",
                "detail": "Waiting for enough reliable volatility history."
                if learning else "Reliable margin volatility is unavailable.",
            }
        value = float(current)
        passed = value <= maximum + 1e-12
        return {
            "enabled": True,
            "passed": passed,
            "current": value,
            "required": maximum,
            "cushion_ratio": cushion,
            "status": "PASS" if passed else "BLOCKED",
            "detail": "Volatility is within the configured limit."
            if passed else f"Waiting: volatility is {value:.1f}; maximum is {maximum:.1f}.",
        }

    def _history(self, observed_at: str) -> list[dict[str, Any]]:
        observed = parse_time(observed_at) or datetime.now(UTC)
        since = (observed - timedelta(seconds=WINDOW_SECONDS + SAMPLE_SECONDS)).isoformat()
        return self.db.fetch_all(
            """
            SELECT observed_at,ticker,threshold,margin,source_reliable
            FROM margin_volatility_observations
            WHERE calculation_version=? AND observed_at>=? AND observed_at<?
            ORDER BY observed_at ASC
            """,
            (CALCULATION_VERSION, since, observed.isoformat()),
        )

    def _baseline(self, observed_at: str) -> list[float]:
        rows = self.db.fetch_all(
            """
            SELECT raw_score FROM margin_volatility_observations
            WHERE calculation_version=? AND raw_score IS NOT NULL
              AND observed_at<?
            ORDER BY id DESC LIMIT 5000
            """,
            (CALCULATION_VERSION, observed_at),
        )
        return [float(row["raw_score"]) for row in rows]

    def observe(
        self,
        *,
        observed_at: str,
        ticker: str,
        threshold: float,
        btc_proxy: float,
        seconds_remaining: float,
        source_reliable: bool,
    ) -> dict[str, Any]:
        observed = parse_time(observed_at) or datetime.now(UTC)
        bucket = math.floor(observed.timestamp() / SAMPLE_SECONDS)
        key = (bucket, str(ticker), float(threshold), bool(source_reliable))
        if self._last_observation_key == key:
            cached = self._cached_observation
            cached_at = parse_time(str(cached.get("observed_at", ""))) if cached else None
            # A bucket is only an admission boundary.  Replays within it may
            # ask for an earlier as-of instant, which must never inherit a
            # later observation merely because its cache key matches.
            if cached_at is not None and cached_at <= observed:
                return dict(cached)
        observed_time = observed.isoformat()
        margin = float(btc_proxy) - float(threshold)
        candidate = {
            "observed_at": observed_time,
            "ticker": str(ticker),
            "margin": margin,
            "threshold": float(threshold),
            "source_reliable": bool(source_reliable),
        }
        history = self._history(observed_time)
        history.append(candidate)
        components = volatility_components(history)
        coverage = float(components["coverage"] or 0.0)
        window_ready = coverage >= MINIMUM_COVERAGE
        raw_score = components.get("raw_score") if window_ready and source_reliable else None
        baseline = self._baseline(observed_time) if raw_score is not None else []
        mvi = historical_percentile_index(float(raw_score), baseline) if raw_score is not None else None
        expected, cushion = cushion_metrics(
            margin,
            components.get("raw_realized_volatility") if window_ready else None,
            float(seconds_remaining),
        )
        reliable = bool(source_reliable and window_ready and mvi is not None)
        reliability_state = (
            "RELIABLE" if reliable else "UNAVAILABLE" if not source_reliable else "LEARNING"
        )
        self.db.execute(
            """
            INSERT OR REPLACE INTO margin_volatility_observations(
                observed_at,ticker,threshold,btc_proxy,margin,
                raw_realized_volatility,movement_intensity,reversal_component,
                raw_score,mvi,expected_remaining_move,cushion_ratio,
                seconds_remaining,coverage,source_reliable,reliable,reliability_state,calculation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observed_time, ticker, threshold, btc_proxy, margin,
                components.get("raw_realized_volatility") if window_ready else None,
                components.get("movement_intensity") if window_ready else None,
                components.get("reversal_component") if window_ready else None,
                raw_score, mvi, expected, cushion, seconds_remaining, coverage,
                int(source_reliable), int(reliable), reliability_state, CALCULATION_VERSION,
            ),
        )
        row = self.db.fetch_one(
            """
            SELECT * FROM margin_volatility_observations
            WHERE calculation_version=? AND observed_at=? AND ticker=? AND threshold=?
            ORDER BY id DESC LIMIT 1
            """,
            (CALCULATION_VERSION, observed_time, str(ticker), float(threshold)),
        )
        if not row:
            # A failed/incomplete write must not install a cache key that
            # masks the next retry with an earlier market's reading.
            return self._unavailable(reliability_state)
        row["source_reliable"] = bool(row.get("source_reliable"))
        row["reliable"] = bool(row.get("reliable"))
        self._last_observation_key = key
        self._cached_observation = dict(row)
        return row

    def current(self, *, as_of: str | None = None) -> dict[str, Any] | None:
        """Return the newest reading, optionally bounded to a causal instant."""
        sql = """
            SELECT * FROM margin_volatility_observations
            WHERE calculation_version=?
        """
        params: tuple[Any, ...] = (CALCULATION_VERSION,)
        if as_of is not None:
            sql += " AND observed_at<=?"
            params += (as_of,)
        sql += " ORDER BY observed_at DESC,id DESC LIMIT 1"
        row = self.db.fetch_one(sql, params)
        if row:
            row["source_reliable"] = bool(row.get("source_reliable"))
            row["reliable"] = bool(row.get("reliable"))
        return row

    @staticmethod
    def _unavailable(state: str) -> dict[str, Any]:
        return {
            "mvi": None,
            "cushion_ratio": None,
            "expected_remaining_move": None,
            "reliable": False,
            "reliability_state": state,
            "calculation_version": CALCULATION_VERSION,
        }

    def chart(self, since: str) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT observed_at,mvi,raw_realized_volatility,movement_intensity,
                   reversal_component,expected_remaining_move,cushion_ratio,
                   coverage,source_reliable,reliable,reliability_state,calculation_version
            FROM margin_volatility_observations
            WHERE calculation_version=? AND observed_at>=?
            ORDER BY observed_at ASC
            """,
            (CALCULATION_VERSION, since),
        )

    def report(self, mode: str) -> dict[str, Any]:
        normalized_mode = str(mode or "PAPER").upper()
        observations = self.db.fetch_all(
            """
            SELECT mvi FROM margin_volatility_observations
            WHERE calculation_version=? AND reliable=1 AND mvi IS NOT NULL
            """,
            (CALCULATION_VERSION,),
        )
        if normalized_mode == "PAPER":
            entries = self.db.fetch_all(
                """
                SELECT e.margin_volatility_index AS mvi,
                       e.margin_cushion_ratio AS cushion_ratio,e.strategy,
                       t.outcome AS settlement_won,t.realized_pnl
                FROM paper_trades t
                JOIN paper_entries e ON e.id=(
                    SELECT first_entry.id FROM paper_entries first_entry
                    WHERE first_entry.trade_id=t.id
                      AND first_entry.source='automatic'
                      AND first_entry.margin_volatility_index IS NOT NULL
                      AND json_extract(first_entry.strategy_metadata_json,
                                       '$.margin_volatility_version')=?
                    ORDER BY first_entry.id ASC LIMIT 1
                )
                WHERE t.outcome IS NOT NULL
                """,
                (CALCULATION_VERSION,),
            )
        else:
            # Local import avoids the broker -> paper -> MVI construction
            # cycle; report generation is infrequent and never quote-hot.
            from app.services.broker import KalshiBroker

            confirmed_entries = self.db.fetch_all(
                """
                WITH ranked_entries AS (
                    SELECT i.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY i.mode,i.ticker,i.side,i.strategy
                               ORDER BY i.created_at ASC,i.id ASC
                           ) AS sequence
                    FROM broker_order_intents i
                    WHERE i.mode=? AND i.source='automatic' AND i.action='BUY'
                      AND i.margin_volatility_index IS NOT NULL
                      AND json_extract(i.decision_snapshot_json,
                                       '$.margin_volatility_version')=?
                      AND EXISTS (
                          SELECT 1 FROM broker_fills f
                          WHERE f.mode=i.mode AND f.action='BUY' AND (
                              (f.client_order_id IS NOT NULL
                               AND f.client_order_id=i.client_order_id)
                              OR (i.exchange_order_id IS NOT NULL
                                  AND f.exchange_order_id=i.exchange_order_id)
                          )
                      )
                )
                SELECT i.margin_volatility_index AS mvi,
                       i.margin_cushion_ratio AS cushion_ratio,i.strategy,
                       i.ticker,i.side
                FROM ranked_entries i
                WHERE i.sequence=1
                """,
                (normalized_mode, CALCULATION_VERSION),
            )
            # The broker's bounded FIFO projection is the single source for
            # economic closed-via-sell and settled outcomes.  An intent alone
            # is never a round trip; matching it to its own durable fill above
            # prevents manual/imported ticker-side activity from attribution.
            ledger = KalshiBroker(normalized_mode, self.db).trade_ledger()
            by_key = {
                (str(row.get("ticker")), str(row.get("side"))): row
                for row in ledger
                if row.get("status") in {"CLOSED", "SETTLED"}
                and str(row.get("source") or "") == "automatic"
            }
            entries = []
            for entry in confirmed_entries:
                ledger_row = by_key.get((str(entry["ticker"]), str(entry["side"])))
                # Mixed/manual entry lots cannot safely receive the automatic
                # intent's MVI.  Exclude instead of inventing attribution.
                if not ledger_row or str(ledger_row.get("strategy") or "") != str(entry["strategy"]):
                    continue
                pnl = ledger_row.get("realized_pnl")
                if pnl is None:
                    continue
                won = ledger_row.get("position_won")
                entries.append({
                    "mvi": entry.get("mvi"),
                    "cushion_ratio": entry.get("cushion_ratio"),
                    "strategy": entry.get("strategy"),
                    "settlement_won": int(bool(won)) if won is not None else None,
                    "net_profitable": int(float(pnl) > 0),
                    "realized_pnl": pnl,
                })
        for entry in entries:
            # Paper outcomes settle at expiry; exchange CLOSED rows do not.
            # Net profitability is deliberately independent of that outcome.
            entry["net_profitable"] = int(float(entry.get("realized_pnl") or 0) > 0)
        blocked_rows = self.db.fetch_all(
            """
            SELECT margin_volatility_index AS mvi FROM signal_snapshots
            WHERE margin_volatility_max>0
              AND margin_volatility_index>margin_volatility_max
              AND json_extract(input_json,'$.margin_volatility_version')=?
            """,
            (CALCULATION_VERSION,),
        )

        buckets: list[dict[str, Any]] = []
        for lower in range(10):
            upper = lower + 1
            in_bucket = lambda value: in_mvi_bucket(value, lower)
            bucket_entries = [row for row in entries if in_bucket(row.get("mvi"))]
            settled = [row for row in bucket_entries if row.get("settlement_won") is not None]
            wins = sum(bool(row.get("settlement_won")) for row in settled)
            profitable = sum(bool(row.get("net_profitable")) for row in bucket_entries)
            pnl = sum(float(row.get("realized_pnl") or 0) for row in bucket_entries)
            buckets.append(
                {
                    "label": f"{lower}-{upper}",
                    "observations": sum(in_bucket(row.get("mvi")) for row in observations),
                    "entries": len(bucket_entries),
                    "settled": len(settled),
                    "wins": wins,
                    "win_rate": wins / len(settled) if settled else None,
                    "net_profitable_round_trips": profitable,
                    "net_profit_rate": profitable / len(bucket_entries) if bucket_entries else None,
                    "realized_pnl": pnl,
                    "blocked_opportunities": sum(
                        in_bucket(row.get("mvi")) for row in blocked_rows
                    ),
                    "average_cushion": (
                        statistics.fmean(
                            float(row["cushion_ratio"])
                            for row in bucket_entries
                            if row.get("cushion_ratio") is not None
                        )
                        if any(row.get("cushion_ratio") is not None for row in bucket_entries)
                        else None
                    ),
                }
            )
        strategies: dict[str, dict[str, Any]] = {}
        for row in entries:
            strategy = str(row.get("strategy") or "UNKNOWN")
            result = strategies.setdefault(
                strategy,
                {
                    "mode": normalized_mode, "entries": 0, "settled": 0,
                    "wins": 0, "net_profitable_round_trips": 0,
                    "realized_pnl": 0.0,
                },
            )
            result["entries"] += 1
            result["net_profitable_round_trips"] += int(bool(row.get("net_profitable")))
            result["realized_pnl"] += float(row.get("realized_pnl") or 0)
            if row.get("settlement_won") is not None:
                result["settled"] += 1
                result["wins"] += int(bool(row.get("settlement_won")))
        for result in strategies.values():
            result["win_rate"] = (
                result["wins"] / result["settled"] if result["settled"] else None
            )
            result["net_profit_rate"] = (
                result["net_profitable_round_trips"] / result["entries"]
                if result["entries"] else None
            )
        alternatives = []
        for maximum in range(1, 11):
            included = [row for row in entries if float(row["mvi"]) <= maximum]
            settled = [row for row in included if row.get("settlement_won") is not None]
            wins = sum(bool(row.get("settlement_won")) for row in settled)
            profitable = sum(bool(row.get("net_profitable")) for row in included)
            alternatives.append(
                {
                    "maximum": float(maximum),
                    "entries": len(included),
                    "settled": len(settled),
                    "win_rate": wins / len(settled) if settled else None,
                    "net_profitable_round_trips": profitable,
                    "net_profit_rate": profitable / len(included) if included else None,
                    "realized_pnl": sum(
                        float(row.get("realized_pnl") or 0) for row in included
                    ),
                }
            )
        settled_total = sum(row.get("settlement_won") is not None for row in entries)
        completed_total = len(entries)
        return {
            "mode": normalized_mode,
            "calculation_version": CALCULATION_VERSION,
            "scope": (
                "Version-matched MVI evidence only. Texas Hold’em 2.1 uses a "
                "separate lower MVI entry minimum; this report does not tune it. "
                + ("Demo/Live outcomes use the most recent 100 economic ledger rows. "
                   if normalized_mode != "PAPER" else "")
                + "Settlement win rate and net-profit rate are separate measures."
            ),
            "reliable_observations": len(observations),
            "entries": len(entries),
            "completed_round_trips": completed_total,
            "settled_entries": settled_total,
            "economic_ledger_window_rows": 100 if normalized_mode != "PAPER" else None,
            "buckets": buckets,
            "strategies": strategies,
            "alternative_maximums": alternatives,
            "live_limit_ready": len(observations) >= 1000 and completed_total >= 100,
            "guidance": (
                "Enough version-matched evidence is available to review the Standard Edge upper-volatility limit. "
                "Texas Hold’em 2.1's entry gate remains separate."
                if len(observations) >= 1000 and completed_total >= 100
                else "Collect more version-matched confirmed round trips before reviewing the Standard Edge "
                "upper-volatility limit. This does not recommend disabling Texas Hold’em 2.1's separate entry gate."
            ),
        }
