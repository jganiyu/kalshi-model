from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db import Database
from app.research import FeatureDefinition, MarketWindow, ResearchLab, ResearchObservation, SplitPlan


def make_lab(tmp_path: Path) -> tuple[Database, ResearchLab]:
    db = Database(tmp_path / "research.db")
    db.initialize()
    return db, ResearchLab(db)


def iso(offset_seconds: int) -> str:
    return (datetime(2026, 1, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds)).isoformat()


def test_research_observations_preserve_source_price_type_and_provenance(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)

    inserted = lab.ingest_observations(
        [
            ResearchObservation(
                dataset="fixture",
                instrument="BTC-15M-1",
                observed_at=iso(0),
                source="kalshi_ws",
                source_observed_at=iso(-1),
                received_at=iso(1),
                available_at=iso(2),
                price_type="yes_bid",
                value=0.42,
                source_sequence="book-1",
                market_ticker="KXBTC15M-TEST",
                provenance={"channel": "orderbook_delta", "raw_file": "sample.jsonl"},
            )
        ]
    )

    row = db.fetch_one("SELECT * FROM research_observations WHERE dataset='fixture'")
    assert inserted == 1
    assert row is not None
    assert row["source"] == "kalshi_ws"
    assert row["price_type"] == "yes_bid"
    assert row["market_ticker"] == "KXBTC15M-TEST"
    assert row["received_at"] == iso(1)
    assert row["available_at"] == iso(2)
    assert json.loads(row["provenance_json"]) == {
        "channel": "orderbook_delta",
        "raw_file": "sample.jsonl",
    }


def test_research_ingest_is_idempotent_without_source_sequence(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)
    observation = ResearchObservation(
        dataset="fixture",
        instrument="BTC",
        observed_at=iso(0),
        source="brti",
        price_type="brti",
        value=100.0,
    )

    lab.ingest_observations([observation])
    lab.ingest_observations([observation])

    count = db.fetch_one("SELECT COUNT(*) AS count FROM research_observations")
    assert count is not None
    assert count["count"] == 1


def test_research_ingest_rejects_ambiguous_price_type(tmp_path: Path) -> None:
    _, lab = make_lab(tmp_path)

    with pytest.raises(ValueError, match="unsupported price_type"):
        lab.ingest_observations(
            [
                ResearchObservation(
                    dataset="fixture",
                    instrument="BTC",
                    observed_at=iso(0),
                    source="unknown",
                    price_type="priceish",
                    value=1.0,
                )
            ]
        )


def test_quality_checks_detect_stale_prices_gaps_and_source_disagreement(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)
    lab.ingest_observations(
        [
            ResearchObservation("fixture", "BTC", iso(0), "brti", "brti", 100.0, iso(-40)),
            ResearchObservation("fixture", "BTC", iso(0), "coinbase", "brti", 101.0, iso(0)),
            ResearchObservation("fixture", "BTC", iso(120), "brti", "brti", 102.0, iso(120)),
        ]
    )

    events = lab.run_quality_checks(
        "fixture",
        instrument="BTC",
        stale_after_seconds=30,
        max_gap_seconds=60,
        disagreement_pct=0.005,
    )

    checks = {event["check_name"] for event in events}
    assert {"stale_price", "timestamp_gap", "source_disagreement"} <= checks
    persisted = db.fetch_all("SELECT * FROM research_data_quality_events")
    assert len(persisted) >= len(events)


def test_return_feature_is_point_in_time_and_records_source_rows(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)
    lab.ingest_observations(
        [
            ResearchObservation("fixture", "BTC", iso(0), "brti", "brti", 100.0),
            ResearchObservation("fixture", "BTC", iso(60), "brti", "brti", 105.0),
            ResearchObservation("fixture", "BTC", iso(120), "brti", "brti", 103.0),
        ]
    )

    count = lab.compute_return_feature(
        dataset="fixture",
        instrument="BTC",
        price_type="brti",
        feature_name="brti_return_60s",
        horizon_seconds=60,
        source="brti",
    )

    rows = db.fetch_all(
        "SELECT * FROM research_feature_values WHERE feature_name='brti_return_60s' ORDER BY observed_at"
    )
    assert count == 3
    assert json.loads(rows[0]["quality_flags_json"]) == ["missing_prior"]
    assert rows[1]["value"] == pytest.approx(0.05)
    assert json.loads(rows[1]["source_observation_ids_json"]) == [1, 2]
    assert rows[2]["value"] == pytest.approx((103.0 / 105.0) - 1.0)


def test_return_feature_uses_available_time_not_future_exchange_event_time(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)
    lab.ingest_observations(
        [
            ResearchObservation(
                "fixture", "BTC-PERP", iso(0), "external", "mid", 100.0,
                received_at=iso(30), available_at=iso(30), market_ticker="KXBTC15M-TEST",
                provenance={"adapter": "fixture"},
            ),
            ResearchObservation(
                "fixture", "BTC-PERP", iso(10), "external", "mid", 110.0,
                received_at=iso(120), available_at=iso(120), market_ticker="KXBTC15M-TEST",
                provenance={"adapter": "fixture"},
            ),
            ResearchObservation(
                "fixture", "BTC-PERP", iso(130), "external", "mid", 121.0,
                received_at=iso(130), available_at=iso(130), market_ticker="KXBTC15M-TEST",
                provenance={"adapter": "fixture"},
            ),
        ]
    )

    lab.compute_return_feature(
        dataset="fixture",
        instrument="BTC-PERP",
        price_type="mid",
        feature_name="external_mid_return_60s",
        horizon_seconds=60,
        source="external",
    )

    rows = db.fetch_all(
        "SELECT observed_at,value,source_observation_ids_json FROM research_feature_values "
        "WHERE feature_name='external_mid_return_60s' ORDER BY observed_at"
    )
    assert rows[1]["observed_at"] == iso(120)
    assert json.loads(rows[1]["source_observation_ids_json"]) == [1, 2]
    assert rows[1]["value"] == pytest.approx(0.10)
    assert rows[2]["value"] == pytest.approx(0.21)
    assert json.loads(rows[2]["source_observation_ids_json"]) == [1, 3]


def test_point_in_time_query_excludes_late_rest_response(tmp_path: Path) -> None:
    _, lab = make_lab(tmp_path)
    lab.ingest_observations(
        [
            ResearchObservation(
                "fixture", "BTC-PERP", iso(0), "rest_vendor", "mid", 100.0,
                received_at=iso(300), available_at=iso(300),
                market_ticker="KXBTC15M-TEST", provenance={"adapter": "fixture"},
            ),
            ResearchObservation(
                "fixture", "BTC-PERP", iso(10), "ws_vendor", "mid", 101.0,
                received_at=iso(11), available_at=iso(11),
                market_ticker="KXBTC15M-TEST", provenance={"adapter": "fixture"},
            ),
        ]
    )

    rows = lab.point_in_time_observations(
        dataset="fixture",
        market_ticker="KXBTC15M-TEST",
        asof=iso(20),
        price_type="mid",
    )

    assert [row["source"] for row in rows] == ["ws_vendor"]


def test_market_timeline_alignment_builds_one_second_kxbtc_points(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)

    count = lab.build_market_timeline(
        MarketWindow(
            dataset="fixture",
            market_ticker="KXBTC15M-TEST",
            open_time=iso(0),
            close_time=iso(5),
            settlement_time=iso(5),
        ),
        step_seconds=1,
        settlement_seconds=2,
    )

    rows = db.fetch_all(
        "SELECT timeline_second,observed_at,phase FROM research_market_timeline_points "
        "WHERE market_ticker='KXBTC15M-TEST' ORDER BY id"
    )
    assert count == 7
    assert rows[:2] == [
        {"timeline_second": 0, "observed_at": iso(0), "phase": "OPEN"},
        {"timeline_second": 1, "observed_at": iso(1), "phase": "OPEN"},
    ]
    assert rows[-1]["phase"] == "SETTLEMENT"


class FixtureAdapter:
    source = "fixture_vendor"

    def normalize(self, payload: dict[str, object]) -> list[ResearchObservation]:
        return [
            ResearchObservation(
                dataset=str(payload["dataset"]),
                market_ticker=str(payload["market_ticker"]),
                instrument=str(payload["instrument"]),
                observed_at=str(payload["event_time"]),
                received_at=str(payload["received_at"]),
                available_at=str(payload["received_at"]),
                source=self.source,
                price_type=str(payload["metric"]),
                value=float(payload["value"]),
                source_sequence=str(payload["event_id"]),
                provenance={
                    "adapter": "FixtureAdapter",
                    "venue": payload["venue"],
                    "instrument": payload["instrument"],
                    "event_id": payload["event_id"],
                },
            )
        ]


def test_source_adapter_normalizes_external_observations(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)

    inserted = lab.ingest_from_adapter(
        FixtureAdapter(),
        [
            {
                "dataset": "fixture",
                "market_ticker": "KXBTC15M-TEST",
                "instrument": "BTC-PERP",
                "event_time": iso(0),
                "received_at": iso(1),
                "metric": "mid",
                "value": 100.0,
                "event_id": "a-1",
                "venue": "bybit",
            }
        ],
    )

    row = db.fetch_one("SELECT * FROM research_observations WHERE source='fixture_vendor'")
    assert inserted == 1
    assert row is not None
    assert row["market_ticker"] == "KXBTC15M-TEST"
    assert json.loads(row["provenance_json"])["venue"] == "bybit"


def test_bad_external_data_is_flagged_without_entering_execution_path(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)
    lab.ingest_observations(
        [
            ResearchObservation(
                "fixture", "BTC-PERP", iso(60), "vendor", "mid", 100.0,
                received_at=iso(61), available_at=iso(61), source_sequence="seq-2",
                market_ticker="KXBTC15M-TEST", provenance={"adapter": "fixture"},
            ),
            ResearchObservation(
                "fixture", "BTC-PERP", iso(30), "vendor", "mid", 99.0,
                received_at=iso(29), available_at=iso(29), source_sequence="seq-1",
                market_ticker="KXBTC15M-TEST", provenance={},
            ),
        ]
    )
    duplicate = ResearchObservation(
        "fixture", "BTC-PERP", iso(60), "vendor", "mid", 100.0,
        received_at=iso(61), available_at=iso(61), source_sequence="seq-2",
        market_ticker="KXBTC15M-TEST", provenance={"adapter": "fixture"},
    )
    lab.ingest_observations([duplicate])

    checks = {
        row["check_name"]
        for row in db.fetch_all("SELECT check_name FROM research_data_quality_events")
    }
    assert {
        "out_of_order_timestamp",
        "impossible_time_relationship",
        "missing_provenance",
        "duplicate_event",
    } <= checks


def test_hypothesis_experiment_tracks_splits_and_incremental_brier(tmp_path: Path) -> None:
    db, lab = make_lab(tmp_path)
    lab.register_feature(
        FeatureDefinition(
            "baseline_probability",
            "1",
            "Simple probability baseline.",
            ["market_probability"],
        )
    )
    lab.register_feature(
        FeatureDefinition(
            "candidate_adjustment",
            "1",
            "Small additive candidate signal.",
            ["brti_return_60s"],
        )
    )
    feature_rows = []
    outcome_rows = []
    for index, outcome in enumerate([1, 0, 1, 0, 1, 0]):
        observed_at = iso(index * 60)
        instrument = f"MKT-{index}"
        baseline = 0.55 if outcome else 0.45
        adjustment = 0.10 if outcome else -0.10
        feature_rows.extend(
            [
                ("fixture", None, instrument, "baseline_probability", "1", observed_at, baseline, observed_at, "[]", "[]", iso(999)),
                ("fixture", None, instrument, "candidate_adjustment", "1", observed_at, adjustment, observed_at, "[]", "[]", iso(999)),
            ]
        )
        outcome_rows.append(
            ResearchObservation(
                "fixture",
                instrument,
                iso(index * 60 + 900),
                "kalshi_settlement",
                "settlement",
                float(outcome),
            )
        )
    lab.ingest_observations(outcome_rows)
    db.executemany(
        """
        INSERT INTO research_feature_values(
            dataset,market_ticker,instrument,feature_name,feature_version,observed_at,value,
            asof_observed_at,source_observation_ids_json,quality_flags_json,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        feature_rows,
    )
    hypothesis_id = lab.create_hypothesis(
        name="candidate adjustment improves calibration",
        description="Fixture hypothesis.",
        feature_name="candidate_adjustment",
        expected_direction="two_sided",
    )

    result = lab.evaluate_incremental_value(
        name="fixture experiment",
        dataset="fixture",
        hypothesis_id=hypothesis_id,
        probability_feature="baseline_probability",
        split=SplitPlan(train_end=iso(60), validation_end=iso(180), test_end=iso(360)),
    )

    assert result["metrics"]["validation"]["improved"] is True
    assert result["metrics"]["test"]["improved"] is True
    experiment = db.fetch_one("SELECT * FROM research_experiments WHERE name='fixture experiment'")
    assert experiment is not None
    assert experiment["train_end"] == iso(60)
    assert json.loads(experiment["parameters_json"])["candidate_feature"] == "candidate_adjustment"
