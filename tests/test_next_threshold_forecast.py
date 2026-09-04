from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import AppConfig
from app.db import Database
from app.domain import NextThresholdForecast
from app.engine import AnalysisEngine
from app.services.kalshi import market_strike


def stamp(value: datetime) -> str:
    return value.isoformat()


def market(ticker: str, opens: datetime, threshold: float | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"ticker": ticker, "open_time": stamp(opens)}
    if threshold is not None:
        payload["floor_strike"] = threshold
    return payload


def test_final_minute_uses_one_equal_weighted_proxy_sample_per_second() -> None:
    forecast = NextThresholdForecast()
    opens = datetime(2026, 9, 4, 12, 15, tzinfo=UTC)
    target = market("NEXT", opens)

    state, evidence = forecast.observe(
        next_market=target, known_markets=(None, target), proxy_price=100.0,
        observed_at=stamp(opens - timedelta(seconds=60) + timedelta(milliseconds=100)),
        official_threshold=market_strike,
    )
    assert evidence is None
    assert state and state["status"] == "active"
    # A faster second update cannot get extra weight.
    state, _ = forecast.observe(
        next_market=target, known_markets=(None, target), proxy_price=110.0,
        observed_at=stamp(opens - timedelta(seconds=60) + timedelta(milliseconds=900)),
        official_threshold=market_strike,
    )
    assert state and state["samples_collected"] == 1
    assert state["estimate"] == pytest.approx(100.0)
    state, _ = forecast.observe(
        next_market=target, known_markets=(None, target), proxy_price=102.0,
        observed_at=stamp(opens - timedelta(seconds=59)), official_threshold=market_strike,
    )
    assert state and state["estimate"] == pytest.approx(101.0)
    assert state["coverage"] == pytest.approx(1.0)
    assert "not official" in str(state["qualifier"])


def test_forecast_freezes_then_compares_to_the_published_threshold() -> None:
    forecast = NextThresholdForecast()
    opens = datetime(2026, 9, 4, 12, 15, tzinfo=UTC)
    target = market("OPENING", opens)
    for offset in range(60):
        forecast.observe(
            next_market=target, known_markets=(None, target), proxy_price=100.0 + offset,
            observed_at=stamp(opens - timedelta(seconds=60 - offset)),
            official_threshold=market_strike,
        )

    frozen, frozen_evidence = forecast.freeze_if_due(stamp(opens))
    assert frozen and frozen["status"] == "frozen"
    assert frozen["samples_collected"] == 60
    assert frozen_evidence and frozen_evidence["final_state"] == "FROZEN"

    successor = market("LATER", opens + timedelta(minutes=15))
    current = market("OPENING", opens, threshold=131.25)
    compared, evidence = forecast.observe(
        next_market=successor, known_markets=(current, successor), proxy_price=999.0,
        observed_at=stamp(opens + timedelta(seconds=1)), official_threshold=market_strike,
    )
    assert compared and compared["status"] == "comparing"
    assert compared["official_threshold"] == pytest.approx(131.25)
    assert compared["comparison"]["error_dollars"] == pytest.approx(129.5 - 131.25)
    assert evidence and evidence["final_state"] == "COMPARED"

    forecast.freeze_if_due(stamp(opens + timedelta(seconds=14)))
    inactive, _ = forecast.observe(
        next_market=successor, known_markets=(current, successor), proxy_price=101.0,
        observed_at=stamp(opens + timedelta(seconds=14)), official_threshold=market_strike,
    )
    assert inactive is None


def test_forecast_is_inactive_outside_the_final_minute_and_resets_for_new_target() -> None:
    forecast = NextThresholdForecast()
    opens = datetime(2026, 9, 4, 12, 15, tzinfo=UTC)
    first = market("FIRST", opens)
    state, _ = forecast.observe(
        next_market=first, known_markets=(None, first), proxy_price=100.0,
        observed_at=stamp(opens - timedelta(seconds=61)), official_threshold=market_strike,
    )
    assert state is None
    state, _ = forecast.observe(
        next_market=first, known_markets=(None, first), proxy_price=100.0,
        observed_at=stamp(opens - timedelta(seconds=1)), official_threshold=market_strike,
    )
    assert state and state["ticker"] == "FIRST" and state["samples_collected"] == 1

    later = market("SECOND", opens + timedelta(minutes=15))
    state, _ = forecast.observe(
        next_market=later, known_markets=(None, later), proxy_price=200.0,
        observed_at=stamp(opens + timedelta(minutes=14, seconds=1)), official_threshold=market_strike,
    )
    assert state and state["ticker"] == "SECOND"
    assert state["samples_collected"] == 1 and state["estimate"] == pytest.approx(200.0)


def test_engine_persists_only_read_only_terminal_forecast_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "forecast.db")
    db.initialize()
    engine = AnalysisEngine(AppConfig(database_path=db.path), db)
    opens = datetime(2026, 9, 4, 12, 15, tzinfo=UTC)
    target = market("NEXT", opens)
    engine._next_market = target  # type: ignore[assignment]
    engine._latest_btc = {"price": 100.0}
    engine._update_next_threshold_forecast(stamp(opens - timedelta(seconds=1)))
    engine._current_market = market("NEXT", opens, threshold=100.5)  # type: ignore[assignment]
    engine._next_market = market("AFTER", opens + timedelta(minutes=15))  # type: ignore[assignment]
    engine._update_next_threshold_forecast(stamp(opens + timedelta(seconds=1)))

    saved = db.fetch_one("SELECT * FROM next_threshold_forecasts WHERE ticker='NEXT'")
    assert saved is not None
    assert saved["status"] == "COMPARED"
    assert saved["official_threshold"] == pytest.approx(100.5)
