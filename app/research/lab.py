from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable, Protocol, Sequence

from app.db import Database
from app.domain import iso_now, parse_time


ALLOWED_PRICE_TYPES = {
    "brti",
    "last",
    "mid",
    "bid",
    "ask",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "settlement",
    "probability",
}


@dataclass(frozen=True)
class ResearchObservation:
    dataset: str
    instrument: str
    observed_at: str
    source: str
    price_type: str
    value: float
    source_observed_at: str | None = None
    received_at: str | None = None
    available_at: str | None = None
    source_sequence: str | None = None
    market_ticker: str | None = None
    provenance: dict[str, Any] | None = None


class ResearchSourceAdapter(Protocol):
    """Normalize vendor payloads without coupling vendors to the research lab."""

    source: str

    def normalize(self, payload: Any) -> Iterable[ResearchObservation]:
        """Return source-qualified observations or raise for malformed payloads."""


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    version: str
    description: str
    inputs: Sequence[str]


@dataclass(frozen=True)
class SplitPlan:
    train_end: str
    validation_end: str
    test_end: str | None = None


@dataclass(frozen=True)
class MarketWindow:
    dataset: str
    market_ticker: str
    open_time: str
    close_time: str
    settlement_time: str | None = None


class ResearchLab:
    """Historical research API isolated from live execution.

    The lab stores immutable source-qualified observations, computes small
    replaceable features as point-in-time values, and records experiments as
    hypothesis tests against baselines.  It intentionally does not place,
    schedule, or size trades.
    """

    def __init__(self, db: Database):
        self.db = db

    def ingest_from_adapter(self, adapter: ResearchSourceAdapter, payloads: Iterable[Any]) -> int:
        observations: list[ResearchObservation] = []
        for payload in payloads:
            observations.extend(adapter.normalize(payload))
        return self.ingest_observations(observations)

    def ingest_observations(self, observations: Iterable[ResearchObservation]) -> int:
        rows = []
        quality_events: list[dict[str, Any]] = []
        latest_seen: dict[tuple[str, str, str, str], Any] = {}
        now = iso_now()
        for observation in observations:
            price_type = observation.price_type.lower()
            if price_type not in ALLOWED_PRICE_TYPES:
                raise ValueError(f"unsupported price_type: {observation.price_type}")
            if not math.isfinite(float(observation.value)):
                raise ValueError("research observation value must be finite")
            observed = parse_time(observation.observed_at)
            if observed is None:
                raise ValueError(f"invalid observed_at: {observation.observed_at}")
            if observation.source_observed_at and parse_time(observation.source_observed_at) is None:
                raise ValueError(f"invalid source_observed_at: {observation.source_observed_at}")
            received = parse_time(observation.received_at) if observation.received_at else observed
            available = parse_time(observation.available_at) if observation.available_at else received
            if received is None:
                raise ValueError(f"invalid received_at: {observation.received_at}")
            if available is None:
                raise ValueError(f"invalid available_at: {observation.available_at}")
            provenance = observation.provenance or {}
            if not provenance:
                quality_events.append(
                    self._quality_event(
                        observation.dataset, observation.market_ticker, observation.instrument,
                        observed.isoformat(), "missing_provenance", "WARN", {}
                    )
                )
            elif not isinstance(provenance, dict) or not provenance.get("adapter"):
                quality_events.append(
                    self._quality_event(
                        observation.dataset, observation.market_ticker, observation.instrument,
                        observed.isoformat(), "malformed_provenance", "WARN",
                        {"provenance": provenance},
                    )
                )
            if provenance.get("instrument") and provenance.get("instrument") != observation.instrument:
                quality_events.append(
                    self._quality_event(
                        observation.dataset, observation.market_ticker, observation.instrument,
                        observed.isoformat(), "instrument_mismatch", "ERROR",
                        {"provenance_instrument": provenance.get("instrument")},
                    )
                )
            if observation.market_ticker and provenance.get("market_ticker") and provenance.get("market_ticker") != observation.market_ticker:
                quality_events.append(
                    self._quality_event(
                        observation.dataset, observation.market_ticker, observation.instrument,
                        observed.isoformat(), "instrument_mismatch", "ERROR",
                        {"provenance_market_ticker": provenance.get("market_ticker")},
                    )
                )
            if received < observed or available < observed:
                quality_events.append(
                    self._quality_event(
                        observation.dataset, observation.market_ticker, observation.instrument,
                        observed.isoformat(), "impossible_time_relationship", "ERROR",
                        {
                            "observed_at": observed.isoformat(),
                            "received_at": received.isoformat(),
                            "available_at": available.isoformat(),
                        },
                    )
                )
            existing = self.db.fetch_one(
                """
                SELECT id FROM research_observations
                WHERE dataset=? AND instrument=? AND observed_at=? AND source=?
                  AND price_type=? AND source_sequence=?
                """,
                (
                    observation.dataset, observation.instrument, observed.isoformat(),
                    observation.source, price_type, observation.source_sequence or "",
                ),
            )
            if existing:
                quality_events.append(
                    self._quality_event(
                        observation.dataset, observation.market_ticker, observation.instrument,
                        observed.isoformat(), "duplicate_event", "WARN",
                        {
                            "source": observation.source,
                            "price_type": price_type,
                            "source_sequence": observation.source_sequence or "",
                        },
                    )
                )
            latest_key = (observation.dataset, observation.instrument, observation.source, price_type)
            latest = None if latest_key in latest_seen else self.db.fetch_one(
                """
                SELECT observed_at FROM research_observations
                WHERE dataset=? AND instrument=? AND source=? AND price_type=?
                ORDER BY observed_at DESC LIMIT 1
                """,
                (observation.dataset, observation.instrument, observation.source, price_type),
            )
            latest_time = latest_seen.get(latest_key) or (parse_time(str(latest["observed_at"])) if latest else None)
            if latest_time and observed < latest_time:
                quality_events.append(
                    self._quality_event(
                        observation.dataset, observation.market_ticker, observation.instrument,
                        observed.isoformat(), "out_of_order_timestamp", "WARN",
                        {"latest_observed_at": latest_time.isoformat()},
                    )
                )
            if latest_time is None or observed > latest_time:
                latest_seen[latest_key] = observed
            rows.append(
                (
                    observation.dataset,
                    observation.market_ticker,
                    observation.instrument,
                    observed.isoformat(),
                    observation.source,
                    observation.source_observed_at,
                    received.isoformat(),
                    available.isoformat(),
                    now,
                    price_type,
                    float(observation.value),
                    observation.source_sequence or "",
                    json.dumps(provenance, sort_keys=True),
                )
            )
        self.db.executemany(
            """
            INSERT OR IGNORE INTO research_observations(
                dataset,market_ticker,instrument,observed_at,source,source_observed_at,
                received_at,available_at,ingested_at,price_type,value,source_sequence,
                provenance_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        self._persist_quality_events(quality_events)
        return len(rows)

    def run_quality_checks(
        self,
        dataset: str,
        *,
        instrument: str | None = None,
        max_gap_seconds: float = 60.0,
        stale_after_seconds: float = 30.0,
        disagreement_pct: float = 0.001,
    ) -> list[dict[str, Any]]:
        where = ["dataset=?"]
        params: list[Any] = [dataset]
        if instrument:
            where.append("instrument=?")
            params.append(instrument)
        rows = self.db.fetch_all(
            f"""
            SELECT *
            FROM research_observations
            WHERE {' AND '.join(where)}
            ORDER BY instrument, price_type, observed_at, source
            """,
            tuple(params),
        )
        events: list[dict[str, Any]] = []
        previous: dict[tuple[str, str, str], dict[str, Any]] = {}
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            observed = parse_time(str(row["observed_at"]))
            source_observed = parse_time(row.get("source_observed_at"))
            key = (str(row["instrument"]), str(row["price_type"]), str(row["source"]))
            if observed is None:
                continue
            if not row.get("market_ticker") and str(row["instrument"]).startswith("KXBTC15M"):
                events.append(
                    self._quality_event(
                        dataset, None, str(row["instrument"]), observed.isoformat(),
                        "instrument_mismatch", "WARN",
                        {"reason": "KXBTC15M-like instrument has no market_ticker association"},
                    )
                )
            prior = previous.get(key)
            if prior:
                prior_time = parse_time(str(prior["observed_at"]))
                if prior_time:
                    gap = (observed - prior_time).total_seconds()
                    if gap > max_gap_seconds:
                        events.append(
                            self._quality_event(
                                dataset, row.get("market_ticker"), str(row["instrument"]),
                                observed.isoformat(),
                                "timestamp_gap", "WARN",
                                {"price_type": row["price_type"], "source": row["source"], "gap_seconds": gap},
                            )
                        )
            previous[key] = row
            if source_observed:
                age = (observed - source_observed).total_seconds()
                if age > stale_after_seconds:
                    events.append(
                        self._quality_event(
                            dataset, row.get("market_ticker"), str(row["instrument"]),
                            observed.isoformat(),
                            "stale_price", "WARN",
                            {"price_type": row["price_type"], "source": row["source"], "age_seconds": age},
                        )
                    )
            received = parse_time(str(row.get("received_at") or ""))
            available = parse_time(str(row.get("available_at") or ""))
            if received and received < observed or available and available < observed:
                events.append(
                    self._quality_event(
                        dataset, row.get("market_ticker"), str(row["instrument"]),
                        observed.isoformat(), "impossible_time_relationship", "ERROR",
                        {
                            "observed_at": observed.isoformat(),
                            "received_at": received.isoformat() if received else None,
                            "available_at": available.isoformat() if available else None,
                        },
                    )
                )
            try:
                provenance = json.loads(str(row.get("provenance_json") or "{}"))
            except json.JSONDecodeError:
                provenance = None
            if not provenance:
                events.append(
                    self._quality_event(
                        dataset, row.get("market_ticker"), str(row["instrument"]),
                        observed.isoformat(), "missing_provenance", "WARN", {}
                    )
                )
            elif not isinstance(provenance, dict) or not provenance.get("adapter"):
                events.append(
                    self._quality_event(
                        dataset, row.get("market_ticker"), str(row["instrument"]),
                        observed.isoformat(), "malformed_provenance", "WARN",
                        {"provenance": provenance},
                    )
                )
            grouped.setdefault(
                (str(row["instrument"]), str(row["price_type"]), observed.isoformat()),
                [],
            ).append(row)

        for (row_instrument, price_type, observed_at), samples in grouped.items():
            if len(samples) < 2:
                continue
            values = [float(sample["value"]) for sample in samples]
            center = sum(values) / len(values)
            if center and (max(values) - min(values)) / abs(center) > disagreement_pct:
                events.append(
                    self._quality_event(
                        dataset, samples[0].get("market_ticker"), row_instrument,
                        observed_at, "source_disagreement", "WARN",
                        {
                            "price_type": price_type,
                            "sources": [sample["source"] for sample in samples],
                            "min": min(values),
                            "max": max(values),
                            "relative_range": (max(values) - min(values)) / abs(center),
                        },
                    )
                )
        self._persist_quality_events(events)
        return events

    def register_feature(self, definition: FeatureDefinition) -> None:
        self.db.execute(
            """
            INSERT INTO research_feature_definitions(name,version,description,inputs_json,created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                version=excluded.version,
                description=excluded.description,
                inputs_json=excluded.inputs_json
            """,
            (
                definition.name,
                definition.version,
                definition.description,
                json.dumps(list(definition.inputs)),
                iso_now(),
            ),
        )

    def compute_return_feature(
        self,
        *,
        dataset: str,
        instrument: str,
        price_type: str,
        feature_name: str,
        horizon_seconds: float,
        source: str | None = None,
    ) -> int:
        definition = FeatureDefinition(
            feature_name,
            "1",
            f"Point-in-time return over the prior {horizon_seconds:g} seconds.",
            [price_type],
        )
        self.register_feature(definition)
        params: list[Any] = [dataset, instrument, price_type]
        source_filter = ""
        if source:
            source_filter = "AND source=?"
            params.append(source)
        rows = self.db.fetch_all(
            f"""
            SELECT id, market_ticker, observed_at, available_at, value
            FROM research_observations
            WHERE dataset=? AND instrument=? AND price_type=? {source_filter}
              AND valid=1
            ORDER BY available_at ASC, observed_at ASC, id ASC
            """,
            tuple(params),
        )
        values: list[tuple[Any, str | None, str, str, float]] = [
            (
                row["id"], row.get("market_ticker"), str(row["observed_at"]),
                str(row["available_at"]), float(row["value"]),
            )
            for row in rows
        ]
        feature_rows = []
        left = 0
        for index, (row_id, market_ticker, observed_at, available_at, value) in enumerate(values):
            available = parse_time(available_at)
            if available is None:
                continue
            target = available - timedelta(seconds=horizon_seconds)
            while left + 1 < index:
                candidate_time = parse_time(values[left + 1][3])
                if candidate_time and candidate_time <= target:
                    left += 1
                else:
                    break
            prior_id, _, _, prior_available_at, prior_value = values[left]
            prior_available = parse_time(prior_available_at)
            if prior_available is None or prior_available > target or prior_value <= 0:
                feature_value = None
                flags = ["missing_prior"]
                source_ids = [row_id]
            else:
                feature_value = (value / prior_value) - 1.0
                flags = []
                source_ids = [prior_id, row_id]
            feature_rows.append(
                (
                    dataset, market_ticker, instrument, definition.name, definition.version,
                    available_at, feature_value, available_at,
                    json.dumps(source_ids), json.dumps(flags), iso_now(),
                )
            )
        self.db.executemany(
            """
            INSERT OR REPLACE INTO research_feature_values(
                dataset,market_ticker,instrument,feature_name,feature_version,observed_at,value,
                asof_observed_at,source_observation_ids_json,quality_flags_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            feature_rows,
        )
        return len(feature_rows)

    def build_market_timeline(
        self,
        window: MarketWindow,
        *,
        step_seconds: int = 1,
        settlement_seconds: int = 60,
    ) -> int:
        open_time = parse_time(window.open_time)
        close_time = parse_time(window.close_time)
        settlement_time = parse_time(window.settlement_time) if window.settlement_time else close_time
        if open_time is None or close_time is None or settlement_time is None:
            raise ValueError("market window timestamps must be valid ISO datetimes")
        if close_time <= open_time:
            raise ValueError("market close_time must be after open_time")
        rows = []
        current = open_time
        second = 0
        while current < close_time:
            rows.append(
                (
                    window.dataset, window.market_ticker, second,
                    current.isoformat(), "OPEN", iso_now(),
                )
            )
            second += step_seconds
            current = open_time + timedelta(seconds=second)
        for offset in range(0, settlement_seconds, step_seconds):
            rows.append(
                (
                    window.dataset, window.market_ticker, offset,
                    (settlement_time + timedelta(seconds=offset)).isoformat(),
                    "SETTLEMENT", iso_now(),
                )
            )
        self.db.executemany(
            """
            INSERT OR IGNORE INTO research_market_timeline_points(
                dataset,market_ticker,timeline_second,observed_at,phase,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            rows,
        )
        return len(rows)

    def point_in_time_observations(
        self,
        *,
        dataset: str,
        market_ticker: str,
        asof: str,
        price_type: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = ["dataset=?", "market_ticker=?", "available_at<=?"]
        params: list[Any] = [dataset, market_ticker, asof]
        if price_type:
            filters.append("price_type=?")
            params.append(price_type)
        return self.db.fetch_all(
            f"""
            SELECT *
            FROM research_observations
            WHERE {' AND '.join(filters)}
            ORDER BY available_at DESC, observed_at DESC, id DESC
            """,
            tuple(params),
        )

    def create_hypothesis(
        self,
        *,
        name: str,
        description: str,
        feature_name: str,
        expected_direction: str = "two_sided",
    ) -> int:
        if expected_direction not in {"positive", "negative", "two_sided"}:
            raise ValueError("expected_direction must be positive, negative, or two_sided")
        return self.db.execute(
            """
            INSERT INTO research_hypotheses(
                name,description,feature_name,expected_direction,status,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (name, description, feature_name, expected_direction, "PROPOSED", iso_now()),
        )

    def evaluate_incremental_value(
        self,
        *,
        name: str,
        dataset: str,
        hypothesis_id: int,
        outcome_price_type: str = "settlement",
        probability_feature: str,
        split: SplitPlan,
    ) -> dict[str, Any]:
        hypothesis = self.db.fetch_one(
            "SELECT * FROM research_hypotheses WHERE id=?", (hypothesis_id,)
        )
        if not hypothesis:
            raise ValueError(f"unknown hypothesis_id: {hypothesis_id}")
        candidate_feature = str(hypothesis["feature_name"])
        rows = self.db.fetch_all(
            """
            SELECT p.instrument, p.observed_at, p.asof_observed_at,
                   p.value AS probability,
                   c.value AS candidate_value, o.value AS outcome
            FROM research_feature_values p
            JOIN research_feature_values c
              ON c.dataset=p.dataset
             AND c.instrument=p.instrument
             AND c.observed_at=p.observed_at
            JOIN research_observations o
              ON o.dataset=p.dataset
             AND o.instrument=p.instrument
             AND o.price_type=?
            WHERE p.dataset=?
              AND p.feature_name=?
              AND c.feature_name=?
              AND p.value IS NOT NULL
              AND c.value IS NOT NULL
              AND o.value IN (0,1)
              AND p.asof_observed_at <= p.observed_at
              AND c.asof_observed_at <= c.observed_at
              AND p.observed_at <= o.observed_at
            ORDER BY p.observed_at ASC
            """,
            (outcome_price_type, dataset, probability_feature, candidate_feature),
        )
        scored = [
            row for row in rows
            if str(row["observed_at"]) > split.train_end
            and str(row["observed_at"]) <= (split.test_end or split.validation_end)
        ]
        validation = [
            row for row in scored if str(row["observed_at"]) <= split.validation_end
        ]
        test = [
            row for row in scored if str(row["observed_at"]) > split.validation_end
        ]
        metrics = {
            "validation": self._incremental_metrics(validation),
            "test": self._incremental_metrics(test),
            "guardrails": [
                "outcome rows are joined only at or after feature observation time",
                "train/validation/test boundaries are stored with the experiment",
                "no-trade is preserved because this evaluates probabilities, not trade count",
            ],
        }
        baseline = {
            "name": probability_feature,
            "requirement": "candidate must improve Brier score on validation and remain non-worse on test",
        }
        self.db.execute(
            """
            INSERT INTO research_experiments(
                name,hypothesis_id,dataset,created_at,train_start,train_end,
                validation_start,validation_end,test_start,test_end,
                baseline_json,metrics_json,parameters_json,notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                name,
                hypothesis_id,
                dataset,
                iso_now(),
                None,
                split.train_end,
                split.train_end,
                split.validation_end,
                split.validation_end if split.test_end else None,
                split.test_end,
                json.dumps(baseline, sort_keys=True),
                json.dumps(metrics, sort_keys=True),
                json.dumps(
                    {
                        "candidate_feature": candidate_feature,
                        "probability_feature": probability_feature,
                        "outcome_price_type": outcome_price_type,
                    },
                    sort_keys=True,
                ),
                "Incremental value screen. This is not an execution backtest.",
            ),
        )
        return {"baseline": baseline, "metrics": metrics}

    def _persist_quality_events(self, events: Sequence[dict[str, Any]]) -> None:
        if not events:
            return
        self.db.executemany(
            """
            INSERT INTO research_data_quality_events(
                dataset,market_ticker,instrument,observed_at,check_name,severity,detail_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    event["dataset"], event.get("market_ticker"), event["instrument"],
                    event["observed_at"], event["check_name"], event["severity"],
                    json.dumps(event["detail"], sort_keys=True), event["created_at"],
                )
                for event in events
            ],
        )

    @staticmethod
    def _quality_event(
        dataset: str,
        market_ticker: str | None,
        instrument: str | None,
        observed_at: str,
        check_name: str,
        severity: str,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "dataset": dataset,
            "market_ticker": market_ticker,
            "instrument": instrument,
            "observed_at": observed_at,
            "check_name": check_name,
            "severity": severity,
            "detail": detail,
            "created_at": iso_now(),
        }

    @staticmethod
    def _incremental_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"sample_size": 0, "baseline_brier": None, "candidate_brier": None, "delta_brier": None}
        baseline_errors = []
        candidate_errors = []
        for row in rows:
            probability = min(0.99, max(0.01, float(row["probability"])))
            outcome = float(row["outcome"])
            candidate_raw = float(row["candidate_value"])
            candidate_probability = min(0.99, max(0.01, probability + candidate_raw))
            baseline_errors.append((probability - outcome) ** 2)
            candidate_errors.append((candidate_probability - outcome) ** 2)
        baseline = sum(baseline_errors) / len(baseline_errors)
        candidate = sum(candidate_errors) / len(candidate_errors)
        return {
            "sample_size": len(rows),
            "baseline_brier": baseline,
            "candidate_brier": candidate,
            "delta_brier": baseline - candidate,
            "improved": candidate < baseline,
        }
