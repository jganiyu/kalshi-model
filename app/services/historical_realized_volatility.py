"""Read-only Coinbase candle volatility context.

This deliberately does not share data with MVI or the live BTC composite.  It
answers a narrower question: how large was the realized *path* over a closed
5/15/60 minute candle window, relative to earlier Coinbase BTC-USD windows?
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from datetime import UTC, datetime
from typing import Any, Iterable

import httpx

from app.db import Database
from app.domain import iso_now


logger = logging.getLogger(__name__)
VERSION = "coinbase-rv-1"
SOURCE = "Coinbase"
PRODUCT = "BTC-USD"
GRANULARITY_SECONDS = 60
HORIZONS = (5, 15, 60)
TEXAS_HORIZON = 15
BASELINE_DAYS = 90
MINIMUM_BASELINE_DAYS = 7
MINIMUM_BASELINE_SAMPLES = MINIMUM_BASELINE_DAYS * 24 * 60
# Coinbase permits at most 300 candles; leave a small endpoint-boundary margin.
PAGE_MINUTES = 290
CANDLE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CANDLE_TIMEOUT = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=0.5)
CANDLE_LIMITS = httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=15.0)
# One rule for every consumer of the current 15-minute reading.  A completed
# minute candle is no longer usable for a Texas entry or HUD once its close is
# over two minutes old.  Never allow the display to look fresher than the gate.
CURRENT_READING_MAX_AGE_SECONDS = 2 * GRANULARITY_SECONDS


def utc_now() -> datetime:
    """Small seam for deterministic tests of the wall-clock freshness rule."""
    return datetime.now(UTC)


def current_reading_age_seconds(
    as_of: object, *, now: datetime | None = None,
) -> float:
    try:
        timestamp = datetime.fromisoformat(str(as_of).replace("Z", "+00:00")).astimezone(UTC)
        return ((now or utc_now()).astimezone(UTC) - timestamp).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return float("inf")


def current_reading_is_fresh(as_of: object, *, now: datetime | None = None) -> bool:
    age = current_reading_age_seconds(as_of, now=now)
    return math.isfinite(age) and -1.0 <= age <= CURRENT_READING_MAX_AGE_SECONDS


def completed_minute_epoch(now: datetime | None = None) -> int:
    """The newest *closed* UTC minute; never include the still-forming candle."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return int(now.replace(second=0, microsecond=0).timestamp()) - 60


def normalize_candles(rows: Iterable[Iterable[Any]], *, now_epoch: int) -> list[tuple[int, float]]:
    """Validate Coinbase rows without bridging holes or accepting future bars."""
    result: dict[int, float] = {}
    duplicates: set[int] = set()
    for row in rows:
        try:
            values = list(row)
            epoch = int(values[0])
            close = float(values[4])
        except (TypeError, ValueError, IndexError):
            continue
        if epoch % GRANULARITY_SECONDS or epoch > now_epoch or close <= 0 or not math.isfinite(close):
            continue
        # Duplicate epochs are intentionally rejected rather than silently
        # choosing whichever network response arrived last.
        if epoch in result or epoch in duplicates:
            result.pop(epoch, None)
            duplicates.add(epoch)
            continue
        result[epoch] = close
    return sorted(result.items())


def realized_volatility(closes: Iterable[tuple[int, float]], horizon: int) -> float | None:
    """100*sqrt(sum(log-return^2)); needs exactly H consecutive returns."""
    rows = list(closes)
    if len(rows) != horizon + 1:
        return None
    if any(rows[index][0] - rows[index - 1][0] != GRANULARITY_SECONDS for index in range(1, len(rows))):
        return None
    returns = [math.log(current[1] / previous[1]) for previous, current in zip(rows, rows[1:])]
    return 100.0 * math.sqrt(sum(value * value for value in returns))


def midrank_percentile(current: float, prior: Iterable[float], *, minimum_samples: int = MINIMUM_BASELINE_SAMPLES) -> float | None:
    values = [float(value) for value in prior if math.isfinite(float(value)) and value >= 0]
    if not math.isfinite(current) or len(values) < minimum_samples:
        return None
    tolerance = max(1e-12, abs(current) * 1e-9)
    below = sum(value < current - tolerance for value in values)
    equal = sum(abs(value - current) <= tolerance for value in values)
    return max(0.0, min(100.0, 100.0 * (below + .5 * equal) / len(values)))


class CoinbaseRealizedVolatilityService:
    """Restart-resumable, low-priority public-candle worker with cached HUD state."""

    def __init__(self, db: Database, client: Any | None = None):
        self.db = db
        # A separate, one-connection client prevents cold candle work from
        # queuing behind (or occupying) live composite/Kalshi traffic.
        self._client = client
        self._owns_client = client is None
        self._stopped = False
        self._state: dict[str, Any] = self._empty_state("loading", "Loading Coinbase candle context.")

    def dashboard_state(self) -> dict[str, Any]:
        state = dict(self._state)
        if state.get("as_of") is None and state.get("status") == "loading":
            return state
        stale = not current_reading_is_fresh(state.get("as_of"))
        if stale and state.get("status") != "error":
            state.update({"status": "stale", "current_stale": True,
                          "reason": "Waiting for a fresh completed Coinbase candle."})
            state["horizons"] = {key: {**value, "rv_pct": None, "percentile": None,
                                        "current_valid": False}
                                 for key, value in (state.get("horizons") or {}).items()}
        return state

    def chart_state(self) -> dict[str, Any]:
        """Return the already-computed bounded chart cache; never query/recalculate.

        The public chart endpoint is called much more frequently than the
        minute candle worker.  Giving it a DB-backed calculation would make a
        visual toggle compete with execution, so only the worker writes this
        memory cache.
        """
        state = self.dashboard_state()
        chart = dict(state.get("chart") or {})
        chart["status"] = state.get("status")
        chart["reason"] = state.get("reason")
        if state.get("status") in {"stale", "error", "loading"}:
            # Do not let a beautiful but stale line imply a current tradable
            # reading.  Preserve timestamps as explicit renderer gaps.
            chart["series"] = {
                key: [{**point, "rv_pct": None} for point in values]
                for key, values in (chart.get("series") or {}).items()
            }
        return chart

    def stop(self) -> None:
        self._stopped = True

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _empty_state(status: str, reason: str | None = None) -> dict[str, Any]:
        return {
            "version": VERSION, "source": SOURCE, "product": PRODUCT,
            "granularity_seconds": GRANULARITY_SECONDS, "baseline_days": BASELINE_DAYS,
            "status": status, "reason": reason, "as_of": None,
            "current_stale": True, "historical_status": "not_started",
            "baseline_start": None, "baseline_end": None, "coverage_days": 0.0,
            "progress": {"completed_minutes": 0, "target_minutes": BASELINE_DAYS * 24 * 60},
            "horizons": {str(h): {"rv_pct": None, "percentile": None, "sample_count": 0,
                                  "current_valid": False} for h in HORIZONS},
        }

    async def run(self) -> None:
        """Fetch a small current window first, then backfill without blocking startup."""
        retry_delay = 1.0
        while not self._stopped:
            try:
                await self._refresh_recent()
                retry_delay = 1.0
                refreshed_at = time.monotonic()
                pages_since_summary = 0
                while not self._stopped:
                    complete = await self._backfill_page()
                    pages_since_summary += 1
                    if time.monotonic() - refreshed_at >= 45:
                        await self._refresh_recent()
                        refreshed_at = time.monotonic()
                    if complete or pages_since_summary >= 20:
                        await self._refresh_summary()
                        pages_since_summary = 0
                    if complete:
                        # Keep the current closed-candle measurement fresh after
                        # the historical archive is complete, without polling it
                        # on the quote/arming path.
                        await asyncio.sleep(30.0 + random.random() * 5.0)
                        await self._refresh_recent()
                        refreshed_at = time.monotonic()
                        continue
                    await asyncio.sleep(.35 + random.random() * .25)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Coinbase realized-volatility worker failed: %s", exc, exc_info=True)
                await self._record_failure(str(exc))
                # A history failure is informational. Retry in the background
                # with a bounded, jittered delay rather than leaving a dead task.
                await asyncio.sleep(retry_delay * (.8 + random.random() * .4))
                retry_delay = min(60.0, retry_delay * 2.0)

    async def _refresh_recent(self) -> None:
        end = completed_minute_epoch()
        start = end - 65 * GRANULARITY_SECONDS
        await self._fetch_and_store(start, end + GRANULARITY_SECONDS)
        await self._refresh_summary()

    async def _backfill_page(self) -> bool:
        plan = await asyncio.to_thread(self._next_backfill_plan)
        if plan is None:
            return True
        start, end, is_gap = plan
        await self._fetch_and_store(start, end)
        complete = await asyncio.to_thread(self._range_complete, start, end)
        await asyncio.to_thread(self._mark_requested_range, start, end, complete, is_gap)
        return False

    async def _fetch_and_store(self, start_epoch: int, end_epoch: int) -> int:
        start = datetime.fromtimestamp(start_epoch, UTC).isoformat()
        end = datetime.fromtimestamp(end_epoch, UTC).isoformat()
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=CANDLE_TIMEOUT, limits=CANDLE_LIMITS,
                    headers={"User-Agent": "kalshi-model/0.1 historical-volatility"},
                )
            response = await self._client.get(
                CANDLE_URL,
                params={"start": start, "end": end, "granularity": GRANULARITY_SECONDS},
            )
            response.raise_for_status()
            rows = sorted(response.json(), key=lambda row: row[0])
        except httpx.HTTPStatusError as exc:
            retry = exc.response.headers.get("Retry-After")
            if retry:
                try:
                    await asyncio.sleep(min(30.0, max(0.0, float(retry))))
                except ValueError:
                    pass
            raise
        cleaned = normalize_candles(rows, now_epoch=completed_minute_epoch())
        return await asyncio.to_thread(self._store, cleaned)

    def _store(self, rows: list[tuple[int, float]]) -> int:
        # Do not take Database's shared writer RLock: this cold worker uses a
        # tiny independent WAL transaction so an execution DB write can win.
        if not rows:
            return 0
        with self.db.connect() as connection:
            cursor = connection.executemany(
                """INSERT OR IGNORE INTO coinbase_realized_volatility_candles
                   (source,product,granularity_seconds,minute_epoch,close,fetched_at)
                   VALUES (?,?,?,?,?,?)""",
                [(SOURCE, PRODUCT, GRANULARITY_SECONDS, epoch, close, iso_now()) for epoch, close in rows],
            )
            connection.commit()

            return max(0, int(cursor.rowcount or 0))

    def _bounds(self) -> tuple[int | None, int | None]:
        row = self.db.fetch_one(
            """SELECT MIN(minute_epoch) oldest, MAX(minute_epoch) newest
               FROM coinbase_realized_volatility_candles
               WHERE source=? AND product=? AND granularity_seconds=?""",
            (SOURCE, PRODUCT, GRANULARITY_SECONDS),
        ) or {}
        return row.get("oldest"), row.get("newest")

    def _range_complete(self, start: int, end: int) -> bool:
        row = self.db.fetch_one(
            """SELECT COUNT(*) count FROM coinbase_realized_volatility_candles
               WHERE source=? AND product=? AND granularity_seconds=? AND minute_epoch>=? AND minute_epoch<=?""",
            (SOURCE, PRODUCT, GRANULARITY_SECONDS, start, end),
        ) or {}
        # The endpoint boundaries overlap pages; require every interior minute,
        # while allowing that shared edge to be fetched by its neighbor.
        return int(row.get("count") or 0) >= max(1, (end - start) // GRANULARITY_SECONDS)

    def _next_backfill_plan(self) -> tuple[int, int, bool] | None:
        """Advance a durable cursor even when Coinbase returns an empty page."""
        now = completed_minute_epoch()
        target = now - BASELINE_DAYS * 86400
        state = self.db.fetch_one(
            "SELECT backfill_cursor_epoch,gap_json FROM coinbase_realized_volatility_state WHERE version=?",
            (VERSION,),
        ) or {}
        cursor = state.get("backfill_cursor_epoch")
        if cursor is None:
            oldest, _ = self._bounds()
            cursor = oldest if oldest is not None else now - 65 * GRANULARITY_SECONDS
        cursor = int(cursor)
        if cursor > target:
            return max(target, cursor - PAGE_MINUTES * GRANULARITY_SECONDS), cursor, False
        # Once the main pass completes, repair only bounded, persisted holes.
        gaps = self._gaps(state.get("gap_json"))
        for key, value in sorted(gaps.items()):
            if int(value.get("attempts", 0)) < 3:
                return int(value["start"]), int(value["end"]), True
        hole = self._first_untracked_hole(target, now, gaps)
        if hole is not None:
            return hole[0], hole[1], True
        return None

    @staticmethod
    def _gaps(raw: Any) -> dict[str, dict[str, int]]:
        try:
            decoded = json.loads(str(raw or "{}"))
            return {str(key): {"start": int(value["start"]), "end": int(value["end"]),
                               "attempts": int(value.get("attempts", 0))}
                    for key, value in decoded.items()}
        except (ValueError, TypeError, KeyError):
            return {}

    def _first_untracked_hole(
        self, target: int, now: int, known: dict[str, dict[str, int]],
    ) -> tuple[int, int] | None:
        """Repair an offline/internal gap, but never retry an exhausted hole."""
        rows = self.db.fetch_all(
            """SELECT minute_epoch FROM coinbase_realized_volatility_candles
               WHERE source=? AND product=? AND granularity_seconds=? AND minute_epoch>=? AND minute_epoch<=?
               ORDER BY minute_epoch""",
            (SOURCE, PRODUCT, GRANULARITY_SECONDS, target, now),
        )
        present = {int(row["minute_epoch"]) for row in rows}
        epoch = target
        while epoch <= now:
            if epoch in present:
                epoch += GRANULARITY_SECONDS
                continue
            end = min(now + GRANULARITY_SECONDS, epoch + PAGE_MINUTES * GRANULARITY_SECONDS)
            key = f"{epoch}:{end}"
            if key not in known:
                return epoch, end
            epoch = end
        return None

    def _mark_requested_range(self, start: int, end: int, complete: bool, is_gap: bool) -> None:
        state = self.db.fetch_one(
            "SELECT gap_json FROM coinbase_realized_volatility_state WHERE version=?", (VERSION,)
        ) or {}
        gaps = self._gaps(state.get("gap_json"))
        key = f"{start}:{end}"
        if not complete:
            prior = gaps.get(key, {})
            gaps[key] = {"start": start, "end": end,
                         "attempts": min(3, int(prior.get("attempts", 0)) + 1)}
        else:
            gaps.pop(key, None)
        # Bound durable diagnostic state; these records are status only and
        # never used to fill/bridge returns.
        gaps = dict(list(sorted(gaps.items()))[-64:])
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO coinbase_realized_volatility_state
                   (version,status,reason,backfill_cursor_epoch,gap_json,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(version) DO UPDATE SET backfill_cursor_epoch=COALESCE(excluded.backfill_cursor_epoch,coinbase_realized_volatility_state.backfill_cursor_epoch),
                   gap_json=excluded.gap_json,updated_at=excluded.updated_at""",
                (VERSION, "backfilling", None, start if not is_gap else None,
                 json.dumps(gaps, separators=(",", ":")), iso_now()),
            )
            connection.commit()

    async def _refresh_summary(self) -> None:
        self._state = await asyncio.to_thread(self._summary)

    def _summary(self) -> dict[str, Any]:
        now = completed_minute_epoch()
        target = now - BASELINE_DAYS * 86400
        rows = self.db.fetch_all(
            """SELECT minute_epoch,close FROM coinbase_realized_volatility_candles
               WHERE source=? AND product=? AND granularity_seconds=? AND minute_epoch>=? AND minute_epoch<=?
               ORDER BY minute_epoch""",
            (SOURCE, PRODUCT, GRANULARITY_SECONDS, target, now),
        )
        candles = [(int(row["minute_epoch"]), float(row["close"])) for row in rows]
        epochs = {epoch for epoch, _ in candles}
        newest = max(epochs) if epochs else None
        # Build once; summary work is O(3N) for the three independent horizons,
        # not a repeated full dictionary construction per candidate window.
        by_epoch = dict(candles)
        coverage = len(epochs) / max(1, BASELINE_DAYS * 24 * 60)
        # minute_epoch is the candle's opening timestamp; consumers see the
        # close timestamp, so a fresh completed candle ages from +60 seconds.
        current_as_of = (
            datetime.fromtimestamp(newest + GRANULARITY_SECONDS, UTC).isoformat()
            if newest else None
        )
        # Summary is evaluated against the close of its newest requested
        # completed candle. dashboard_state() and texas_readiness() then apply
        # the same wall-clock rule when this cached state is consumed.
        current_stale = not current_reading_is_fresh(
            current_as_of,
            now=datetime.fromtimestamp(now + GRANULARITY_SECONDS, UTC),
        )
        persisted = self.db.fetch_one(
            "SELECT gap_json FROM coinbase_realized_volatility_state WHERE version=?", (VERSION,)
        ) or {}
        gaps = self._gaps(persisted.get("gap_json"))
        pending_holes = sum(int(item.get("attempts", 0)) < 3 for item in gaps.values())
        exhausted_holes = sum(int(item.get("attempts", 0)) >= 3 for item in gaps.values())
        horizons: dict[str, Any] = {}
        chart_series: dict[str, list[dict[str, Any]]] = {}
        for horizon in HORIZONS:
            end_rows = [(now - horizon * 60 + index * 60, None) for index in range(horizon + 1)]
            latest = [(epoch, by_epoch.get(epoch)) for epoch, _ in end_rows]
            current = None if any(close is None for _, close in latest) else realized_volatility([(e, float(c)) for e, c in latest], horizon)
            # Use only fully earlier, non-overlapping windows: their last return
            # ends before this current window begins.
            prior: list[float] = []
            current_start = now - horizon * 60
            # Independent, trailing windows keep the baseline from counting a
            # single move dozens of times. They are strictly prior to current.
            for end_epoch in range(target + horizon * 60, current_start, horizon * GRANULARITY_SECONDS):
                window = [(end_epoch - horizon * 60 + i * 60, by_epoch.get(end_epoch - horizon * 60 + i * 60)) for i in range(horizon + 1)]
                if any(close is None for _, close in window):
                    continue
                value = realized_volatility([(epoch, float(close)) for epoch, close in window], horizon)
                if value is not None:
                    prior.append(value)
            percentile = midrank_percentile(
                current, prior,
                minimum_samples=max(1, (MINIMUM_BASELINE_DAYS * 24 * 60) // horizon),
            ) if current is not None else None
            horizons[str(horizon)] = {
                "rv_pct": current, "percentile": percentile, "sample_count": len(prior),
                "current_valid": current is not None and not current_stale,
            }
            # Bounded, worker-computed cache for the Dashboard. A null point
            # is an explicit gap; the renderer must never connect across it.
            series: list[dict[str, Any]] = []
            chart_start = max(target + horizon * 60, now - 360 * 60)
            for end_epoch in range(chart_start, now + 1, GRANULARITY_SECONDS):
                window = [(end_epoch - horizon * 60 + index * 60,
                           by_epoch.get(end_epoch - horizon * 60 + index * 60))
                          for index in range(horizon + 1)]
                value = None if any(close is None for _, close in window) else realized_volatility(
                    [(epoch, float(close)) for epoch, close in window], horizon
                )
                series.append({"closed_at": datetime.fromtimestamp(end_epoch + GRANULARITY_SECONDS, UTC).isoformat(),
                               "rv_pct": value})
            chart_series[str(horizon)] = series
        status = "ready" if not current_stale else "stale"
        if current_stale and not candles:
            status = "loading"
        state = {
            "version": VERSION, "source": SOURCE, "product": PRODUCT,
            "granularity_seconds": GRANULARITY_SECONDS, "baseline_days": BASELINE_DAYS,
            "status": status,
            "reason": None if status == "ready" else "Waiting for completed Coinbase candles.",
            "as_of": current_as_of,
            "current_stale": current_stale,
            "historical_status": "partial" if exhausted_holes else ("complete" if coverage >= .999 else "backfilling"),
            "baseline_start": datetime.fromtimestamp(min(epochs), UTC).isoformat() if epochs else None,
            "baseline_end": datetime.fromtimestamp(max(epochs), UTC).isoformat() if epochs else None,
            "coverage_days": coverage * BASELINE_DAYS,
            "progress": {"completed_minutes": len(epochs), "target_minutes": BASELINE_DAYS * 24 * 60,
                         "pending_holes": pending_holes, "exhausted_holes": exhausted_holes},
            "horizons": horizons,
            "chart": {"version": VERSION, "source": SOURCE, "product": PRODUCT,
                      "granularity_seconds": GRANULARITY_SECONDS, "series": chart_series,
                      "as_of": current_as_of,
                      "status": status},
        }
        # As with candle inserts, state is cold telemetry and must not take the
        # shared execution writer lock.
        with self.db.connect() as connection:
            connection.execute(
            """INSERT INTO coinbase_realized_volatility_state
               (version,status,reason,oldest_minute_epoch,newest_minute_epoch,target_oldest_minute_epoch,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(version) DO UPDATE SET status=excluded.status,reason=excluded.reason,
                 oldest_minute_epoch=excluded.oldest_minute_epoch,newest_minute_epoch=excluded.newest_minute_epoch,
                 target_oldest_minute_epoch=excluded.target_oldest_minute_epoch,updated_at=excluded.updated_at""",
                (VERSION, status, state["reason"], min(epochs) if epochs else None, newest, target, iso_now()),
            )
            connection.commit()
        return state

    async def _record_failure(self, detail: str) -> None:
        try:
            await asyncio.to_thread(self._set_failure, detail)
        except Exception:
            # A disk/SQLite failure cannot silently kill the background
            # supervisor; memory is already fail-closed and retries continue.
            logger.warning("Could not persist Coinbase RV worker failure", exc_info=True)

    def _set_failure(self, detail: str) -> None:
        # Worker errors are visible but must never poison public-feed health,
        # arming, entry gates, or protective exits.
        self._state = {
            **self._state, "status": "error", "current_stale": True,
            "reason": f"Coinbase history unavailable: {detail[:120]}", "historical_status": "paused",
            "horizons": {str(h): {"rv_pct": None, "percentile": None, "sample_count": 0,
                                   "current_valid": False} for h in HORIZONS},
        }
        with self.db.connect() as connection:
            connection.execute(
            """INSERT INTO coinbase_realized_volatility_state(version,status,reason,updated_at)
               VALUES (?,?,?,?) ON CONFLICT(version) DO UPDATE SET status=excluded.status,
               reason=excluded.reason,updated_at=excluded.updated_at""",
                (VERSION, "error", self._state["reason"], iso_now()),
            )
            connection.commit()


def texas_readiness(
    state: dict[str, Any] | None, *, observed_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Validate cached Coinbase 15m RV for a new Texas entry (no MVI fallback)."""
    metric = dict(state or {})
    now = observed_at if isinstance(observed_at, datetime) else None
    if now is None and observed_at:
        try:
            now = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError:
            now = None
    now = (now or datetime.now(UTC)).astimezone(UTC)
    base = {"ready": False, "reason": "Coinbase realized volatility is unavailable.",
            "version": metric.get("version"), "source": metric.get("source"),
            "product": metric.get("product"), "granularity_seconds": metric.get("granularity_seconds"),
            "window_minutes": TEXAS_HORIZON, "rv_pct": None, "age_seconds": None,
            "current_valid": False}
    if (metric.get("version") != VERSION or metric.get("source") != SOURCE
            or metric.get("product") != PRODUCT
            or int(metric.get("granularity_seconds") or 0) != GRANULARITY_SECONDS):
        base["reason"] = "Coinbase realized-volatility source/version is unavailable."
        return base
    row = dict((metric.get("horizons") or {}).get(str(TEXAS_HORIZON)) or {})
    try:
        value = float(row.get("rv_pct"))
    except (TypeError, ValueError):
        value = float("nan")
    age = current_reading_age_seconds(metric.get("as_of"), now=now)
    base.update({"rv_pct": value if math.isfinite(value) and value >= 0 else None,
                 "age_seconds": age if math.isfinite(age) else None,
                 "current_valid": bool(row.get("current_valid"))})
    if bool(metric.get("current_stale")) or metric.get("status") not in {"ready", "partial"}:
        base["reason"] = str(metric.get("reason") or "Coinbase realized volatility is stale.")
    elif not base["current_valid"] or base["rv_pct"] is None:
        base["reason"] = "Waiting for 16 consecutive closed Coinbase candles."
    elif age < -1:
        base["reason"] = "Coinbase realized volatility has a future candle."
    elif age > CURRENT_READING_MAX_AGE_SECONDS:
        base["reason"] = "Coinbase realized volatility is stale."
    else:
        base.update({"ready": True, "reason": None})
    return base
