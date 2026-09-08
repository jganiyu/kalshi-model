from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import AppConfig, DEFAULT_SETTINGS
from app.db import MIGRATIONS, Database
from app.domain import iso_now
from app.engine import AnalysisEngine
from app.services.market_data import CompositeQuote, ExchangeQuote
from app.services.decision import Decision
from app.services.forecast import make_forecast
from app.services.margin_volatility import (
    CALCULATION_VERSION,
    MarginVolatilityService,
    cushion_metrics,
    historical_source_reliable,
    historical_percentile_index,
    in_mvi_bucket,
    volatility_components,
)
from app.services.paper import PaperTradingService
from app.services.decision import make_trade_assessment


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "mvi.db")
    db.initialize()
    db.update_settings({"directional_momentum_gate_enabled": False})
    return db


def observations(changes: list[float], *, rollover_at: int | None = None) -> list[dict]:
    start = datetime(2026, 8, 28, 12, tzinfo=UTC)
    margin = 0.0
    result = [{"observed_at": start.isoformat(), "ticker": "A", "margin": margin}]
    for index, change in enumerate(changes, start=1):
        ticker = "B" if rollover_at is not None and index >= rollover_at else "A"
        if rollover_at is not None and index == rollover_at:
            margin = -500.0
        else:
            margin += change
        result.append(
            {
                "observed_at": (start + timedelta(seconds=index * 5)).isoformat(),
                "ticker": ticker,
                "margin": margin,
            }
        )
    return result


def test_mvi_scale_normalization_and_movement_shape() -> None:
    baseline = [float(value) for value in range(1, 100)]
    assert historical_percentile_index(50.0, baseline) == pytest.approx(5.0)
    assert 0 <= historical_percentile_index(-100.0, baseline) <= 10
    assert 0 <= historical_percentile_index(1000.0, baseline) <= 10

    calm = volatility_components(observations([0.05, -0.05] * 180))
    directional = volatility_components(observations([1.0] * 360))
    choppy = volatility_components(observations([1.0, -1.0] * 180))
    assert float(calm["raw_score"] or 0) < float(choppy["raw_score"] or 0)
    assert float(choppy["raw_score"] or 0) > float(directional["raw_score"] or 0)
    assert float(choppy["reversal_component"] or 0) > 0.9


def test_contract_rollover_does_not_create_false_spike() -> None:
    steady = volatility_components(observations([0.5, -0.5] * 180))
    rollover = volatility_components(
        observations([0.5, -0.5] * 180, rollover_at=180)
    )
    assert rollover["raw_score"] == pytest.approx(steady["raw_score"], rel=0.02)
    assert int(rollover["change_count"] or 0) == int(steady["change_count"] or 0) - 1


def test_mvi_uses_qualified_contiguous_samples_and_actual_intervals() -> None:
    start = datetime(2026, 8, 28, 12, tzinfo=UTC)
    def points(intervals: list[float], deltas: list[float]) -> list[dict]:
        timestamp, margin = start, 0.0
        result = [{"observed_at": timestamp.isoformat(), "ticker": "A", "margin": margin,
                   "source_reliable": True}]
        for elapsed, delta in zip(intervals, deltas):
            timestamp += timedelta(seconds=elapsed)
            margin += delta
            result.append({"observed_at": timestamp.isoformat(), "ticker": "A", "margin": margin,
                           "source_reliable": True})
        return result

    # Constant velocity at irregular receipt intervals has no residual
    # volatility after drift is estimated in dollars/second.
    irregular = [2.0, 8.0, 3.0, 7.0] * 20
    constant_velocity = volatility_components(points(irregular, [4.0 * dt for dt in irregular]))
    assert float(constant_velocity["raw_realized_volatility"] or 0) < 1e-10
    assert constant_velocity["reversal_component"] == 0

    # Zero-drift ±sqrt(dt) shocks have the same innovation scale regardless
    # of irregular spacing; this is not the vacuous zero-vs-zero case.
    regular_intervals = [5.0] * 160
    regular_deltas = [sign * math.sqrt(5.0) for sign in [1.0, 1.0, -1.0, -1.0] * 40]
    irregular_intervals = [2.0, 8.0, 2.0, 8.0] * 40
    irregular_deltas = [
        sign * math.sqrt(dt)
        for dt, sign in zip(irregular_intervals, [1.0, 1.0, -1.0, -1.0] * 40)
    ]
    regular = volatility_components(points(regular_intervals, regular_deltas))
    sparse = volatility_components(points(irregular_intervals, irregular_deltas))
    assert float(regular["raw_realized_volatility"] or 0) > .1
    assert sparse["raw_realized_volatility"] == pytest.approx(
        regular["raw_realized_volatility"], rel=.02
    )

    full = volatility_components(points([5.0] * 360, [1.0, -1.0] * 180))
    dropout = points([5.0] * 360, [1.0, -1.0] * 180)
    for point in dropout[100:160]:
        point["source_reliable"] = False
    dropped = volatility_components(dropout)
    assert float(dropped["coverage"] or 0) < float(full["coverage"] or 0)
    assert int(dropped["change_count"] or 0) < int(full["change_count"] or 0)

    # A source gap is a hard segment boundary: do not count the sign on either
    # side as an invented reversal.
    a = math.sqrt(5.0)
    segmented = points([5.0, 5.0, 20.0, 5.0, 5.0], [a, -a, 0.0, -a, a])
    components = volatility_components(segmented)
    assert components["reversal_comparisons"] == 2
    assert components["reversal_count"] == 2


def test_mvi_preserves_actual_receipt_time_and_invalidates_same_bucket_cache(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    service = MarginVolatilityService(db)
    # Simulate a previously safe result admitted in the current five-second bucket.
    db.execute(
        """INSERT INTO margin_volatility_observations(
            observed_at,ticker,threshold,btc_proxy,margin,coverage,source_reliable,
            reliable,reliability_state,calculation_version
        ) VALUES ('2026-09-01T12:00:00.100000+00:00','A',100,101,1,1,1,1,
                  'RELIABLE',?)""",
        (CALCULATION_VERSION,),
    )
    # A later persisted row must never leak into an earlier as-of calculation.
    db.execute(
        """INSERT INTO margin_volatility_observations(
            observed_at,ticker,threshold,btc_proxy,margin,coverage,source_reliable,
            reliable,reliability_state,calculation_version
        ) VALUES ('2026-09-01T12:01:00+00:00','FUTURE',100,101,1,1,1,1,
                  'RELIABLE',?)""",
        (CALCULATION_VERSION,),
    )
    as_of = service.observe(
        observed_at="2026-09-01T12:00:01.500000+00:00", ticker="A", threshold=100,
        btc_proxy=101, seconds_remaining=899, source_reliable=False,
    )
    assert as_of["ticker"] == "A"
    assert as_of["reliable"] is False
    assert service.current(as_of="2026-09-01T12:00:02+00:00") == as_of
    service._last_observation_key = (  # type: ignore[attr-defined]
        int(datetime(2026, 9, 1, 12, tzinfo=UTC).timestamp() // 5), "A", 100.0, True
    )
    degraded = service.observe(
        observed_at="2026-09-01T12:00:01.900000+00:00", ticker="A", threshold=100,
        btc_proxy=101, seconds_remaining=899, source_reliable=False,
    )
    assert degraded["reliable"] is False
    stored = db.fetch_one(
        "SELECT observed_at,source_reliable,reliable FROM margin_volatility_observations "
        "WHERE calculation_version=? AND ticker='A' ORDER BY observed_at DESC LIMIT 1", (CALCULATION_VERSION,)
    ) or {}
    assert stored["observed_at"] == "2026-09-01T12:00:01.900000+00:00"
    assert stored["source_reliable"] == stored["reliable"] == 0
    # A changed market identity in that same bucket also cannot reuse A's state.
    service.observe(
        observed_at="2026-09-01T12:00:02.100000+00:00", ticker="B", threshold=102,
        btc_proxy=101, seconds_remaining=898, source_reliable=True,
    )
    current = db.fetch_one(
        "SELECT ticker,threshold FROM margin_volatility_observations "
        "WHERE calculation_version=? AND ticker='B' ORDER BY observed_at DESC LIMIT 1", (CALCULATION_VERSION,)
    )
    assert current == {"ticker": "B", "threshold": 102.0}


def test_mvi_same_bucket_cache_never_leaks_later_observation_to_replay(tmp_path: Path) -> None:
    service = MarginVolatilityService(make_db(tmp_path))
    later = service.observe(
        observed_at="2026-09-01T12:00:04+00:00", ticker="A", threshold=100,
        btc_proxy=101, seconds_remaining=896, source_reliable=True,
    )
    assert later["observed_at"] == "2026-09-01T12:00:04+00:00"

    replay = service.observe(
        observed_at="2026-09-01T12:00:02+00:00", ticker="A", threshold=100,
        btc_proxy=101, seconds_remaining=898, source_reliable=True,
    )
    assert replay["observed_at"] == "2026-09-01T12:00:02+00:00"


def test_engine_retains_real_5m_15m_and_60m_windows_after_quiet_minute(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    now = datetime.now(UTC).replace(microsecond=0)
    engine._btc_samples_loaded = True
    # Turbulence ended two minutes ago; a one-minute-only buffer would erase it
    # and make every displayed horizon collapse to the quiet reading.
    engine._recent_btc_samples = []
    for index in range(0, 690):
        timestamp = now - timedelta(minutes=60) + timedelta(seconds=index * 5)
        amplitude = 200.0 if timestamp < now - timedelta(minutes=15) else 20.0
        engine._recent_btc_samples.append(
            (timestamp.timestamp(), 100_000.0 + (amplitude if index % 2 else -amplitude))
        )
    quiet_start = now - timedelta(minutes=2)
    engine._recent_btc_samples.extend(
        ((quiet_start + timedelta(seconds=index * 5)).timestamp(), 100_000.0)
        for index in range(25)
    )
    quote = CompositeQuote(
        price=100_000.0,
        dispersion_pct=.01,
        quotes=[
            ExchangeQuote("Coinbase", 100_000.0, None, None, None, 1),
            ExchangeQuote("Kraken", 100_000.0, None, None, None, 1),
        ],
        errors={},
    )
    state = asyncio.run(engine._save_bitcoin(quote, now.isoformat(), persist=False))
    assert state["volatility_5m"] != state["volatility_15m"]
    assert state["volatility_15m"] != state["volatility_60m"]
    assert len(engine._recent_btc_samples) > 600


def test_cushion_uses_raw_volatility_and_square_root_of_time() -> None:
    expected, cushion = cushion_metrics(50.0, 2.0, 100.0)
    assert expected == pytest.approx(20.0)
    assert cushion == pytest.approx(2.5)
    expected_longer, cushion_longer = cushion_metrics(50.0, 2.0, 400.0)
    assert expected_longer == pytest.approx(40.0)
    assert cushion_longer == pytest.approx(1.25)
    assert cushion_metrics(50.0, 0.0, 100.0) == (None, None)


def test_volatility_gate_off_exact_limit_low_and_learning() -> None:
    off = MarginVolatilityService.gate(
        {"maximum_margin_volatility": 0}, None
    )
    assert off["passed"] is True and off["status"] == "OFF"
    settings = {"maximum_margin_volatility": 7.5}
    exact = MarginVolatilityService.gate(
        settings, {"mvi": 7.5, "reliable": True, "cushion_ratio": 1.4}
    )
    low = MarginVolatilityService.gate(
        settings, {"mvi": 0.1, "reliable": True}
    )
    high = MarginVolatilityService.gate(
        settings, {"mvi": 7.5001, "reliable": True}
    )
    learning = MarginVolatilityService.gate(
        settings, {"mvi": None, "reliable": False, "reliability_state": "LEARNING"}
    )
    assert exact["passed"] is True
    assert low["passed"] is True
    assert high["passed"] is False
    assert learning["passed"] is False and learning["status"] == "LEARNING"


def add_market(db: Database, ticker: str) -> None:
    db.execute(
        """
        INSERT INTO markets(
            ticker,event_ticker,status,title,strike,open_time,close_time,
            expected_expiration_time,result,rules_primary,rules_secondary,raw_json,
            first_seen_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ticker, ticker, "active", "test", 100.0,
            "2026-08-28T12:00:00+00:00", "2026-08-28T12:15:00+00:00",
            "2026-08-28T12:15:00+00:00", None, "", "", "{}", iso_now(), iso_now(),
        ),
    )


def test_entry_and_signal_evidence_persist(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    add_market(db, "MVI-EVIDENCE")
    service = PaperTradingService(db)
    service._entry_volatility = {"mvi": 6.2, "cushion_ratio": 1.4}
    decision = Decision(
        "BUY", "BUY_EDGE", "Moderate", "test", 0.75, 0.50, 0.20,
        0.18, 0.51, 0.01, 0.02, 10.0, 10, "YES",
    )
    assert service.open_from_decision("MVI-EVIDENCE", decision)
    entry = db.fetch_one(
        "SELECT margin_volatility_index,margin_cushion_ratio FROM paper_entries"
    )
    assert entry == {
        "margin_volatility_index": pytest.approx(6.2),
        "margin_cushion_ratio": pytest.approx(1.4),
    }

    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    engine._save_signal(
        "MVI-EVIDENCE", make_forecast(0.75), decision, "test", {}, {}, {},
        "test", iso_now(), 0.75, "YES",
        {"mvi": 6.2, "cushion_ratio": 1.4},
    )
    signal = db.fetch_one(
        "SELECT margin_volatility_index,margin_cushion_ratio,margin_volatility_max FROM signal_snapshots"
    )
    assert signal["margin_volatility_index"] == pytest.approx(6.2)
    assert signal["margin_cushion_ratio"] == pytest.approx(1.4)
    assert signal["margin_volatility_max"] == pytest.approx(0.0)


def test_additive_migration_preserves_history_and_settings(tmp_path: Path) -> None:
    legacy = Database(tmp_path / "legacy.db")
    with legacy.transaction() as connection:
        for version, sql in MIGRATIONS[:12]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                (version, iso_now()),
            )
        connection.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
            ("starting_bankroll", "4321.0", iso_now()),
        )
    legacy.initialize()
    assert legacy.settings()["starting_bankroll"] == pytest.approx(4321.0)
    assert legacy.settings()["maximum_margin_volatility"] == pytest.approx(0.0)
    assert legacy.fetch_one("SELECT MAX(version) version FROM schema_migrations")["version"] == MIGRATIONS[-1][0]
    assert legacy.fetch_one("SELECT COUNT(*) count FROM margin_volatility_observations")["count"] == 0
    assert "margin_volatility_index" in {
        row["name"] for row in legacy.fetch_all("PRAGMA table_info(signal_snapshots)")
    }


def test_default_gate_is_off() -> None:
    assert DEFAULT_SETTINGS["maximum_margin_volatility"] == pytest.approx(0.0)
    assert CALCULATION_VERSION == "mvi-2"


def test_corrected_mvi_version_preserves_prior_readings(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    stamp = "2026-09-01T12:00:00+00:00"
    db.execute(
        """INSERT INTO margin_volatility_observations(
            observed_at,ticker,threshold,btc_proxy,margin,coverage,reliable,
            reliability_state,calculation_version
        ) VALUES (?, 'OLD',100,101,1,1,1,'RELIABLE','mvi-1')""",
        (stamp,),
    )
    MarginVolatilityService(db).observe(
        observed_at="2026-09-01T12:00:05+00:00", ticker="NEW", threshold=100,
        btc_proxy=101, seconds_remaining=895, source_reliable=True,
    )
    preserved = db.fetch_one(
        "SELECT ticker,calculation_version FROM margin_volatility_observations "
        "WHERE calculation_version='mvi-1'"
    )
    corrected = db.fetch_one(
        "SELECT calculation_version FROM margin_volatility_observations "
        "WHERE calculation_version=?", (CALCULATION_VERSION,)
    )
    assert preserved == {"ticker": "OLD", "calculation_version": "mvi-1"}
    assert corrected == {"calculation_version": "mvi-2"}


def test_mvi2_backfill_rejects_persisted_ticks_without_fresh_quote_provenance(
    tmp_path: Path,
) -> None:
    db = make_db(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    tick_time = now - timedelta(seconds=5)
    db.execute(
        """INSERT INTO markets(
            ticker,status,strike,open_time,close_time,raw_json,first_seen_at,updated_at
        ) VALUES ('PROVENANCE','active',100,?,?, '{}',?,?)""",
        (
            (now - timedelta(minutes=10)).isoformat(),
            (now + timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat(),
        ),
    )
    stale_source = json.dumps({"quotes": [
        {"price": 101.0, "observed_at": (tick_time - timedelta(seconds=30)).isoformat()},
        {"price": 101.0, "observed_at": (tick_time - timedelta(seconds=30)).isoformat()},
    ]})
    db.execute(
        """INSERT INTO btc_ticks(
            observed_at,composite_price,dispersion_pct,exchange_count,source_json
        ) VALUES (?,?,?,?,?)""",
        (tick_time.isoformat(), 101.0, .01, 2, stale_source),
    )
    assert not historical_source_reliable(
        db.fetch_one("SELECT * FROM btc_ticks") or {}, tick_time, db.settings()
    )
    assert MarginVolatilityService(db).backfill_recent(hours=1) == 1
    row = db.fetch_one(
        "SELECT source_reliable,reliable FROM margin_volatility_observations "
        "WHERE calculation_version=?", (CALCULATION_VERSION,)
    )
    assert row == {"source_reliable": 0, "reliable": 0}


def test_mvi_9_to_10_bucket_has_no_lower_score_leakage_and_counts_round_trips(
    tmp_path: Path,
) -> None:
    assert in_mvi_bucket(8.99, 8)
    assert not in_mvi_bucket(8.99, 9)
    assert in_mvi_bucket(9.0, 9)
    assert in_mvi_bucket(10.0, 9)
    assert not in_mvi_bucket(0.1, 9)

    db = make_db(tmp_path)
    stamp = "2026-09-01T12:00:00+00:00"
    # A canceled partial-fill plus retry is one completed round trip, not two.
    for client_order_id, mvi in (("entry-a", 9.5), ("entry-retry", 1.0)):
        db.execute(
            """INSERT INTO broker_order_intents(
                mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
                    status,strategy,source,created_at,updated_at,margin_volatility_index,
                    decision_snapshot_json
                ) VALUES ('LIVE',?,'ROUND','YES','BUY',1,.5,?,
                          'TEXAS_HOLDEM_2_0','automatic',?,?,?,
                          '{"margin_volatility_version":"mvi-2"}')""",
            (client_order_id, "CANCELED" if client_order_id == "entry-a" else "FILLED", stamp, stamp, mvi),
        )
    db.execute(
        """INSERT INTO broker_fills(
            mode,fill_id,client_order_id,ticker,side,action,contracts,price,strategy,source,filled_at
        ) VALUES ('LIVE','fill-a','entry-a','ROUND','YES','BUY',1,.5,
                  'TEXAS_HOLDEM_2_0','automatic',?)""",
        (stamp,),
    )
    db.execute(
        """INSERT INTO broker_fills(
            mode,fill_id,client_order_id,ticker,side,action,contracts,price,strategy,source,filled_at
        ) VALUES ('LIVE','exit-a','exit-a','ROUND','YES','SELL',1,.7,
                  'TEXAS_HOLDEM_2_0','texas_phase_exit',?)""",
        (stamp,),
    )
    # Same ticker/side alone is not enough: this manual round has no matching
    # automatic client/exchange order and must never borrow MVI evidence.
    db.execute(
        """INSERT INTO broker_fills(
            mode,fill_id,ticker,side,action,contracts,price,strategy,source,filled_at
        ) VALUES
          ('LIVE','manual-buy','MANUAL','YES','BUY',1,.5,'MANUAL','manual',?),
          ('LIVE','manual-sell','MANUAL','YES','SELL',1,.7,'MANUAL','manual',?)""",
        (stamp, stamp),
    )
    # A complete old-version automatic trade remains durable history but is
    # excluded from mvi-2 readiness rather than inflating the new estimator.
    db.execute(
        """INSERT INTO broker_order_intents(
            mode,client_order_id,ticker,side,action,requested_contracts,limit_price,
            status,strategy,source,created_at,updated_at,margin_volatility_index,
            decision_snapshot_json
        ) VALUES ('LIVE','old-entry','OLD','YES','BUY',1,.5,'FILLED',
                  'TEXAS_HOLDEM','automatic',?,?,8.0,
                  '{"margin_volatility_version":"mvi-1"}')""",
        (stamp, stamp),
    )
    db.execute(
        """INSERT INTO broker_fills(
            mode,fill_id,client_order_id,ticker,side,action,contracts,price,strategy,source,filled_at
        ) VALUES
          ('LIVE','old-buy','old-entry','OLD','YES','BUY',1,.5,'TEXAS_HOLDEM','automatic',?),
          ('LIVE','old-sell','old-exit','OLD','YES','SELL',1,.7,'TEXAS_HOLDEM','texas_phase_exit',?)""",
        (stamp, stamp),
    )
    report = MarginVolatilityService(db).report("LIVE")
    assert report["entries"] == report["completed_round_trips"] == 1
    assert report["settled_entries"] == 0
    assert report["buckets"][9]["entries"] == 1
    assert report["buckets"][9]["net_profitable_round_trips"] == 1
    assert report["buckets"][9]["settled"] == 0
    assert report["buckets"][1]["entries"] == 0
    assert report["buckets"][9]["realized_pnl"] == pytest.approx(.2)
    assert "Texas Hold’em 2.0's separate lower MVI gate" in report["guidance"]


@pytest.mark.parametrize("mode", ["PAPER", "DEMO", "LIVE"])
def test_every_execution_mode_uses_same_gate_and_resets_confirmation(
    tmp_path: Path, mode: str
) -> None:
    db = make_db(tmp_path)
    db.update_settings(
        {
            "paper_trading_enabled": True,
                # MVI is retired; an old non-zero cap is now a truthful
                # Standard Edge blocker rather than an active measurement.
                "maximum_margin_volatility": 0.0,
            "threshold_margin_gate_dollars": 0,
            "early_threshold_enabled": False,
            "late_conviction_enabled": False,
            "swing_enabled": False,
            "automatic_confirmation_seconds": 5,
            "automatic_min_confidence": "Moderate",
        }
    )
    market = {
        "yes_bid": 0.38, "yes_ask": 0.40, "no_bid": 0.60, "no_ask": 0.62,
        "yes_ask_size": 1000, "no_ask_size": 1000,
    }
    quality = {"reliable": True, "trade_allowed": True, "reason": "current"}
    side_assessments = {
        side: make_trade_assessment(
            up_probability=0.75,
            market=market,
            settings={"slippage_cents": 0.5},
            side=side,
            data_quality=quality,
        )
        for side in ("YES", "NO")
    }
    decision = Decision(
        "BUY", "BUY_EDGE", "Moderate", "fixture", 0.75, 0.39, 0.20,
        0.18, 0.405, 0.01, 0.02, 10.0, 10, "YES",
    )
    service = PaperTradingService(db)

    def run(now: float, mvi: float) -> dict:
        return service.consider_strategies(
            ticker="MVI-MODE", assessments=side_assessments,
            standard_decisions={"YES": decision, "NO": decision.__class__(
                "HOLD", "NO_EDGE", "Low", "fixture", 0.25, 0.61, -0.1,
                None, None, None, 0, 0, 0, "NO"
            )},
            seconds_remaining=300, market_status="active",
            market_open_time="2026-08-28T12:00:00+00:00",
            market_observed_at="2026-08-28T12:10:00+00:00",
            threshold_state=None, settlement_window={"coverage": 1.0},
            z_distance=3.0, threshold_margin_dollars=100,
            margin_volatility={
                "mvi": mvi, "reliable": True, "reliability_state": "RELIABLE",
                "cushion_ratio": 1.5,
            },
            model_version="test", now=now, execution_mode=mode,
            automatic_enabled=True,
            execution_risk_by_side={
                "YES": {"passed": True}, "NO": {"passed": True}
            },
            portfolio={
                "automatic_trade_allowed": True,
                "automatic_trade_block_reason": None,
                "available_cash": 1000,
            },
        )

    run(0, 6.0)
    progressing = run(3, 6.0)["standard_edge_readiness"]
    assert progressing["metrics"]["confirmation"]["progress"] > 0
    unchanged = run(4, 8.0)["standard_edge_readiness"]
    assert unchanged["mode"] == mode
    assert unchanged["gates"]["volatility"]["passed"] is True
    assert unchanged["metrics"]["confirmation"]["progress"] > 0
