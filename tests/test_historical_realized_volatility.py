from __future__ import annotations

import math
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import httpx

from app.db import Database
from app.services.historical_realized_volatility import (
    BASELINE_DAYS,
    CoinbaseRealizedVolatilityService,
    CURRENT_READING_MAX_AGE_SECONDS,
    GRANULARITY_SECONDS,
    completed_minute_epoch,
    midrank_percentile,
    normalize_candles,
    realized_volatility,
    texas_readiness,
)


def rows(start: int, count: int, *, ratio: float = 1.001) -> list[tuple[int, float]]:
    return [(start + index * 60, 100.0 * ratio ** index) for index in range(count)]


def test_realized_volatility_requires_exact_consecutive_h_plus_one() -> None:
    candles = rows(1_700_000_000, 6, ratio=1.01)
    value = realized_volatility(candles, 5)
    assert value == pytest.approx(100 * math.sqrt(5 * math.log(1.01) ** 2))
    assert realized_volatility(candles[:-1], 5) is None
    assert realized_volatility(candles[:3] + candles[4:], 5) is None


def test_normalization_rejects_future_partial_duplicate_and_bad_candles() -> None:
    now = 1_700_000_040
    raw = [
        [now - 60, 0, 0, 0, 100], [now, 0, 0, 0, 101],
        [now - 120, 0, 0, 0, 99], [now - 120, 0, 0, 0, 99],
        [now - 180, 0, 0, 0, -1], [now - 30, 0, 0, 0, 90],
    ]
    assert normalize_candles(raw, now_epoch=now - 60) == [(now - 60, 100.0)]


def test_percentile_is_midrank_and_prior_only_minimum_is_explicit() -> None:
    assert midrank_percentile(2, [1, 2, 2, 3], minimum_samples=4) == pytest.approx(50.0)
    assert midrank_percentile(2, [1, 2, 3], minimum_samples=4) is None


def test_summary_has_only_the_active_texas_horizon_and_never_invents_zero(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db, object())  # type: ignore[arg-type]
    now = completed_minute_epoch(datetime(2026, 1, 1, tzinfo=UTC))
    # More than 7 days allows a percentile for the active Texas horizon.
    service._store(rows(now - 8 * 24 * 60 * 60, 8 * 24 * 60 + 1, ratio=1.0001))
    # Pin wall time so the stored newest candle is the summary's current close.
    import app.services.historical_realized_volatility as module
    original = module.completed_minute_epoch
    module.completed_minute_epoch = lambda: now
    try:
        state = service._summary()
    finally:
        module.completed_minute_epoch = original
    assert state["horizons"]["15"]["rv_pct"] is not None
    assert state["horizons"]["15"]["percentile"] is not None
    assert set(state["horizons"]) == {"15"}
    assert db.fetch_one("SELECT version FROM coinbase_realized_volatility_state")["version"] == "coinbase-rv-1"


def test_partial_history_is_loading_not_false_zero(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db, object())  # type: ignore[arg-type]
    state = service.dashboard_state()
    assert state["horizons"]["15"]["rv_pct"] is None
    assert state["horizons"]["15"]["percentile"] is None
    assert state["baseline_days"] == BASELINE_DAYS
    assert GRANULARITY_SECONDS == 60


def test_background_failure_is_contained_and_visible(tmp_path) -> None:
    class BrokenFeed:
        async def coinbase_candles(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    async def check() -> None:
        db = Database(tmp_path / "rv.sqlite")
        db.initialize()
        service = CoinbaseRealizedVolatilityService(db, BrokenFeed())
        task = asyncio.create_task(service.run())
        await asyncio.sleep(.05)
        assert service.dashboard_state()["status"] == "error"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())


def test_failure_persistence_error_does_not_kill_supervisor_state(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db)
    original_connect = db.connect
    db.connect = lambda: (_ for _ in ()).throw(OSError("disk unavailable"))  # type: ignore[method-assign]
    try:
        asyncio.run(service._record_failure("network down"))
    finally:
        db.connect = original_connect  # type: ignore[method-assign]
    assert service.dashboard_state()["status"] == "error"
    assert service.dashboard_state()["horizons"]["15"]["rv_pct"] is None


def test_backfill_failure_keeps_a_fresh_current_reading_visible_and_auditable(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db)
    service._state = {
        **service._state, "status": "ready", "current_stale": False,
        "as_of": datetime.now(UTC).isoformat(),
        "horizons": {"15": {"rv_pct": .20, "percentile": 80, "sample_count": 99,
                              "current_valid": True}},
    }
    asyncio.run(service._record_backfill_failure("RuntimeError: empty upstream page"))
    state = service.dashboard_state()
    assert state["status"] == "ready"
    assert state["horizons"]["15"]["rv_pct"] == .20
    assert state["historical_note"].startswith("Historical repair delayed")
    event = db.fetch_one("SELECT kind,detail FROM coinbase_realized_volatility_events")
    assert event == {"kind": "BACKFILL_FAILURE", "detail": "RuntimeError: empty upstream page"}


def test_blank_exception_gets_a_useful_diagnostic() -> None:
    assert CoinbaseRealizedVolatilityService._failure_detail(RuntimeError()) == "RuntimeError: RuntimeError()"


def test_stale_cached_state_never_advertises_old_value(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db)
    service._state = {
        **service._state, "status": "ready", "current_stale": False,
        "as_of": "2000-01-01T00:00:00+00:00",
        "horizons": {"15": {"rv_pct": .18, "percentile": 82, "sample_count": 99,
                              "current_valid": True}},
    }
    state = service.dashboard_state()
    assert state["status"] == "stale"
    assert state["horizons"]["15"]["rv_pct"] is None


def test_dashboard_and_texas_share_the_same_current_reading_age_limit(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    import app.services.historical_realized_volatility as module
    monkeypatch.setattr(module, "utc_now", lambda: now)
    state = {
        "version": "coinbase-rv-1", "source": "Coinbase", "product": "BTC-USD",
        "granularity_seconds": 60, "status": "ready", "current_stale": False,
        "as_of": (now - timedelta(seconds=CURRENT_READING_MAX_AGE_SECONDS)).isoformat(),
        "horizons": {"15": {"rv_pct": .20, "current_valid": True}},
    }
    service._state = state
    assert service.dashboard_state()["status"] == "ready"
    assert texas_readiness(state, observed_at=now)["ready"] is True
    stale = {**state, "as_of": (now - timedelta(seconds=CURRENT_READING_MAX_AGE_SECONDS + 1)).isoformat()}
    service._state = stale
    assert service.dashboard_state()["status"] == "stale"
    assert texas_readiness(stale, observed_at=now)["ready"] is False


def test_empty_page_advances_durable_cursor_and_marks_hole(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db)
    now = completed_minute_epoch()
    start, end = now - 2 * 290 * 60, now - 290 * 60
    service._mark_requested_range(start, end, False, False)
    stored = db.fetch_one(
        "SELECT backfill_cursor_epoch,gap_json FROM coinbase_realized_volatility_state WHERE version=?",
        ("coinbase-rv-1",),
    )
    assert stored["backfill_cursor_epoch"] == start
    assert f'"{start}:{end}"' in stored["gap_json"]
    next_start, next_end, is_gap = service._next_backfill_plan()
    assert is_gap is False
    assert next_end == start and next_start < next_end


def test_exhausted_gap_gets_a_later_retry_instead_of_becoming_permanent(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db)
    now = completed_minute_epoch()
    start, end = now - 290 * 60, now
    for _ in range(3):
        service._mark_requested_range(start, end, False, True)
    stored = db.fetch_one(
        "SELECT gap_json FROM coinbase_realized_volatility_state WHERE version=?",
        ("coinbase-rv-1",),
    )
    gap = service._gaps(stored["gap_json"])[f"{start}:{end}"]
    assert gap["attempts"] == 3
    assert gap["retry_after_epoch"] > 0


def test_texas_readiness_requires_exact_current_coinbase_contract() -> None:
    now = "2026-01-01T00:01:00+00:00"
    valid = {
        "version": "coinbase-rv-1", "source": "Coinbase", "product": "BTC-USD",
        "granularity_seconds": 60, "status": "ready", "current_stale": False,
        "as_of": "2026-01-01T00:00:00+00:00",
        "horizons": {"15": {"rv_pct": .20, "current_valid": True}},
    }
    assert texas_readiness(valid, observed_at=now)["ready"] is True
    for key, value in (("version", "mvi-2"), ("source", "Other"), ("product", "ETH-USD"), ("granularity_seconds", 300)):
        wrong = {**valid, key: value}
        assert texas_readiness(wrong, observed_at=now)["ready"] is False
    stale = {**valid, "as_of": "2025-12-31T23:50:00+00:00"}
    assert texas_readiness(stale, observed_at=now)["ready"] is False
    future = {**valid, "as_of": "2026-01-01T00:02:00+00:00"}
    assert texas_readiness(future, observed_at=now)["ready"] is False
    hole = {**valid, "horizons": {"15": {"rv_pct": .20, "current_valid": False}}}
    assert texas_readiness(hole, observed_at=now)["ready"] is False


def test_worker_summary_exposes_bounded_closed_candle_chart_cache(tmp_path) -> None:
    db = Database(tmp_path / "rv.sqlite")
    db.initialize()
    service = CoinbaseRealizedVolatilityService(db, object())  # type: ignore[arg-type]
    now = completed_minute_epoch(datetime(2026, 1, 1, tzinfo=UTC))
    service._store(rows(now - 9 * 24 * 60 * 60, 9 * 24 * 60 + 1, ratio=1.0001))
    import app.services.historical_realized_volatility as module
    original = module.completed_minute_epoch
    original_utc_now = module.utc_now
    module.completed_minute_epoch = lambda: now
    module.utc_now = lambda: datetime.fromtimestamp(now + 60, UTC)
    try:
        service._state = service._summary()
        chart = service.chart_state()
    finally:
        module.completed_minute_epoch = original
        module.utc_now = original_utc_now
    assert chart["status"] == "ready"
    assert set(chart["series"]) == {"15"}
    assert all(len(points) <= 361 for points in chart["series"].values())
    assert chart["series"]["15"][-1]["closed_at"] == datetime.fromtimestamp(now + 60, UTC).isoformat()
    assert chart["series"]["15"][-1]["rv_pct"] == pytest.approx(
        service._state["horizons"]["15"]["rv_pct"]
    )
