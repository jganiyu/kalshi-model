from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from app.db import Database
from app.domain import parse_time


CALCULATION_VERSION = "mvi-1"
WINDOW_SECONDS = 30 * 60
SAMPLE_SECONDS = 5
EXPECTED_CHANGES = WINDOW_SECONDS // SAMPLE_SECONDS
MINIMUM_COVERAGE = 0.80
MINIMUM_BASELINE_SAMPLES = 30
NOISE_FLOOR_DOLLARS = 0.25


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


def volatility_components(
    observations: list[dict[str, Any]],
) -> dict[str, float | int | None]:
    """Calculate robust 5-second movement and reversal components.

    Contract changes are deliberately skipped so a new threshold never becomes a
    synthetic price move. The surrounding contract segments still contribute.
    """
    changes: list[float] = []
    for previous, current in zip(observations, observations[1:]):
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
        changes.append(float(current["margin"]) - float(previous["margin"]))

    coverage = min(1.0, len(changes) / EXPECTED_CHANGES)
    if not changes:
        return {
            "raw_realized_volatility": None,
            "movement_intensity": None,
            "reversal_component": None,
            "raw_score": None,
            "coverage": coverage,
            "change_count": 0,
        }

    robust_center = statistics.median(changes)
    center = statistics.fmean(changes)
    deviations = [abs(change - robust_center) for change in changes]
    mad = statistics.median(deviations)
    robust_scale = max(NOISE_FLOOR_DOLLARS, 1.4826 * mad)
    winsor_limit = 6.0 * robust_scale
    winsorized = [
        max(-winsor_limit, min(winsor_limit, change - center))
        for change in changes
    ]
    movement = math.sqrt(
        sum(change * change for change in winsorized) / len(winsorized)
    ) / math.sqrt(SAMPLE_SECONDS)

    noise = max(NOISE_FLOOR_DOLLARS, statistics.median([abs(v) for v in changes]) * 0.08)
    signs = [1 if change > 0 else -1 for change in changes if abs(change) >= noise]
    reversals = sum(left != right for left, right in zip(signs, signs[1:]))
    reversal = reversals / max(1, len(signs) - 1) if len(signs) >= 2 else 0.0
    raw_score = movement * (1.0 + 0.75 * reversal)
    return {
        "raw_realized_volatility": movement,
        "movement_intensity": movement,
        "reversal_component": reversal,
        "raw_score": raw_score,
        "coverage": coverage,
        "change_count": len(changes),
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
        self._last_bucket: int | None = None

    def backfill_recent(self, hours: int = 2) -> int:
        """Seed the new metric from recent local history without rewriting it."""
        existing = self.db.fetch_one(
            "SELECT 1 AS present FROM margin_volatility_observations LIMIT 1"
        ) or {}
        if existing.get("present"):
            return 0
        since = (datetime.now(UTC) - timedelta(hours=max(1, hours))).isoformat()
        ticks = self.db.fetch_all(
            """
            SELECT observed_at,composite_price,dispersion_pct,exchange_count
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
        for bucket, tick in sorted(buckets.items()):
            observed = datetime.fromtimestamp(bucket * SAMPLE_SECONDS, UTC)
            while interval_index < len(intervals) and observed > intervals[interval_index][1]:
                interval_index += 1
            if interval_index >= len(intervals):
                break
            opened, closed, market = intervals[interval_index]
            if observed < opened or observed > closed:
                continue
            threshold = float(market["strike"])
            proxy = float(tick["composite_price"])
            candidate = {
                "observed_at": observed.isoformat(),
                "ticker": str(market["ticker"]),
                "margin": proxy - threshold,
                "threshold": threshold,
            }
            cutoff = observed - timedelta(seconds=WINDOW_SECONDS + SAMPLE_SECONDS)
            history = [
                item for item in history
                if (parse_time(item["observed_at"]) or observed) >= cutoff
            ]
            history.append(candidate)
            components = volatility_components(history)
            coverage = float(components["coverage"] or 0.0)
            source_reliable = bool(
                int(tick.get("exchange_count") or 0)
                >= int(settings.get("minimum_exchange_feeds", 2))
                and float(tick.get("dispersion_pct") or 0)
                <= float(settings.get("max_exchange_dispersion_pct", 0.40))
            )
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
                    int(reliable), state, CALCULATION_VERSION,
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
                seconds_remaining,coverage,reliable,reliability_state,calculation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            SELECT observed_at,ticker,threshold,margin FROM margin_volatility_observations
            WHERE calculation_version=? AND observed_at>=? AND observed_at<=?
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
        if self._last_bucket == bucket:
            return self.current() or self._unavailable("LEARNING")
        self._last_bucket = bucket
        bucket_time = datetime.fromtimestamp(bucket * SAMPLE_SECONDS, UTC).isoformat()
        margin = float(btc_proxy) - float(threshold)
        candidate = {
            "observed_at": bucket_time,
            "ticker": str(ticker),
            "margin": margin,
            "threshold": float(threshold),
        }
        history = self._history(bucket_time)
        history.append(candidate)
        components = volatility_components(history)
        coverage = float(components["coverage"] or 0.0)
        window_ready = coverage >= MINIMUM_COVERAGE
        raw_score = components.get("raw_score") if window_ready and source_reliable else None
        baseline = self._baseline(bucket_time) if raw_score is not None else []
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
                seconds_remaining,coverage,reliable,reliability_state,calculation_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                bucket_time, ticker, threshold, btc_proxy, margin,
                components.get("raw_realized_volatility") if window_ready else None,
                components.get("movement_intensity") if window_ready else None,
                components.get("reversal_component") if window_ready else None,
                raw_score, mvi, expected, cushion, seconds_remaining, coverage,
                int(reliable), reliability_state, CALCULATION_VERSION,
            ),
        )
        return self.current() or self._unavailable(reliability_state)

    def current(self) -> dict[str, Any] | None:
        row = self.db.fetch_one(
            """
            SELECT * FROM margin_volatility_observations
            WHERE calculation_version=? ORDER BY observed_at DESC,id DESC LIMIT 1
            """,
            (CALCULATION_VERSION,),
        )
        if row:
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
                   coverage,reliable,reliability_state,calculation_version
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
                       t.outcome AS won,t.realized_pnl
                FROM paper_entries e JOIN paper_trades t ON t.id=e.trade_id
                WHERE e.source='automatic' AND e.margin_volatility_index IS NOT NULL
                """
            )
        else:
            entries = self.db.fetch_all(
                """
                SELECT i.margin_volatility_index AS mvi,
                       i.margin_cushion_ratio AS cushion_ratio,i.strategy,
                       s.position_won AS won,s.realized_pnl
                FROM broker_order_intents i
                LEFT JOIN broker_settlements s ON s.mode=i.mode AND s.ticker=i.ticker
                WHERE i.mode=? AND i.source='automatic' AND i.action='BUY'
                  AND i.margin_volatility_index IS NOT NULL
                  AND i.status NOT IN ('REJECTED','CANCELED','CANCELLED')
                """,
                (normalized_mode,),
            )
        blocked_rows = self.db.fetch_all(
            """
            SELECT margin_volatility_index AS mvi FROM signal_snapshots
            WHERE margin_volatility_max>0
              AND margin_volatility_index>margin_volatility_max
            """
        )

        buckets: list[dict[str, Any]] = []
        for lower in range(10):
            upper = lower + 1
            in_bucket = lambda value: value is not None and (
                lower <= float(value) < upper or (lower == 9 and float(value) <= 10)
            )
            bucket_entries = [row for row in entries if in_bucket(row.get("mvi"))]
            settled = [row for row in bucket_entries if row.get("won") is not None]
            wins = sum(bool(row.get("won")) for row in settled)
            pnl = sum(float(row.get("realized_pnl") or 0) for row in settled)
            buckets.append(
                {
                    "label": f"{lower}-{upper}",
                    "observations": sum(in_bucket(row.get("mvi")) for row in observations),
                    "entries": len(bucket_entries),
                    "settled": len(settled),
                    "wins": wins,
                    "win_rate": wins / len(settled) if settled else None,
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
                {"mode": normalized_mode, "entries": 0, "settled": 0, "wins": 0, "realized_pnl": 0.0},
            )
            result["entries"] += 1
            if row.get("won") is not None:
                result["settled"] += 1
                result["wins"] += int(bool(row.get("won")))
                result["realized_pnl"] += float(row.get("realized_pnl") or 0)
        for result in strategies.values():
            result["win_rate"] = (
                result["wins"] / result["settled"] if result["settled"] else None
            )
        alternatives = []
        for maximum in range(1, 11):
            included = [row for row in entries if float(row["mvi"]) <= maximum]
            settled = [row for row in included if row.get("won") is not None]
            wins = sum(bool(row.get("won")) for row in settled)
            alternatives.append(
                {
                    "maximum": float(maximum),
                    "entries": len(included),
                    "settled": len(settled),
                    "win_rate": wins / len(settled) if settled else None,
                    "realized_pnl": sum(
                        float(row.get("realized_pnl") or 0) for row in settled
                    ),
                }
            )
        settled_total = sum(row.get("won") is not None for row in entries)
        return {
            "mode": normalized_mode,
            "calculation_version": CALCULATION_VERSION,
            "reliable_observations": len(observations),
            "entries": len(entries),
            "settled_entries": settled_total,
            "buckets": buckets,
            "strategies": strategies,
            "alternative_maximums": alternatives,
            "live_limit_ready": len(observations) >= 1000 and settled_total >= 100,
            "guidance": (
                "Enough evidence is available for a reviewed limit."
                if len(observations) >= 1000 and settled_total >= 100
                else "Keep the Live gate off until more reliable observations and settled entries accumulate."
            ),
        }
