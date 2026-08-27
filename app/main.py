from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, DEFAULT_SETTINGS, ROOT
from app.db import Database
from app.engine import AnalysisEngine
from app.services.backtest import BacktestService
from app.services.credentials import (
    CredentialStore,
    environment_credentials,
    masked_key_id,
)
from app.services.training import report_rows


config = AppConfig()
db = Database(config.database_path)
engine = AnalysisEngine(config, db)
credential_store = CredentialStore()
templates = Jinja2Templates(directory=ROOT / "app" / "templates")
static_root = ROOT / "app" / "static"
asset_digest = hashlib.sha256()
for asset_name in ("styles.css", "app.js"):
    asset_digest.update((static_root / asset_name).read_bytes())
asset_version = asset_digest.hexdigest()[:12]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    await engine.start()
    yield
    await engine.stop()


app = FastAPI(
    title="Kalshi Model",
    description="Local, read-only Kalshi BTC probability analysis and paper trading",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=static_root), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"asset_version": asset_version},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(ROOT / "app" / "static" / "icon-32.png", media_type="image/png")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "database": str(config.database_path), "system": engine.dashboard["system"]}


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    return engine.dashboard


@app.websocket("/ws/live")
async def live(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = engine.subscribe()
    try:
        await websocket.send_json({"type": "dashboard", "data": engine.dashboard})
        while True:
            await queue.get()
            await websocket.send_json({"type": "dashboard", "data": engine.dashboard})
    except WebSocketDisconnect:
        pass
    finally:
        engine.unsubscribe(queue)


@app.get("/api/chart")
async def chart(minutes: int = Query(default=5, ge=5, le=360)) -> dict[str, Any]:
    return {"minutes": minutes, "points": engine.chart(minutes)}


@app.get("/api/paper")
async def paper() -> dict[str, Any]:
    return engine.paper.portfolio()


@app.post("/api/paper/orders")
async def place_paper_order(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return await engine.place_manual_paper_order(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/paper/orders/{order_id}")
async def cancel_paper_order(order_id: int) -> dict[str, Any]:
    try:
        return await engine.cancel_manual_paper_order(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/paper/reset")
async def reset_paper_round() -> dict[str, Any]:
    return await engine.reset_paper_round()


@app.get("/api/signals")
async def signals(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    rows = db.fetch_all(
        "SELECT * FROM signal_snapshots ORDER BY observed_at DESC LIMIT ?", (limit,)
    )
    for row in rows:
        for key in ("input_json", "btc_state_json", "kalshi_state_json"):
            row[key.removesuffix("_json")] = json.loads(row.pop(key))
    return {"signals": rows}


@app.get("/api/signals/{signal_id}")
async def signal(signal_id: int) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM signal_snapshots WHERE id=?", (signal_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Signal snapshot not found")
    for key in ("input_json", "btc_state_json", "kalshi_state_json"):
        row[key.removesuffix("_json")] = json.loads(row.pop(key))
    return row


@app.get("/api/calibration")
async def calibration() -> dict[str, Any]:
    return {
        "summary": engine.calibration_summary(),
        "strategy_results": engine.paper.strategy_results(),
        "reports": report_rows(db),
        "configuration_snapshots": db.configuration_snapshots(),
    }


@app.get("/api/models")
async def models() -> dict[str, Any]:
    rows = db.fetch_all("SELECT * FROM model_versions ORDER BY created_at DESC")
    for row in rows:
        row["validation"] = json.loads(row.pop("validation_json"))
        row["parameters"] = json.loads(row.pop("parameters_json"))
    return {"active": engine.models.active(), "versions": rows}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return db.settings()


@app.get("/api/settings/defaults")
async def get_default_settings() -> dict[str, Any]:
    return DEFAULT_SETTINGS


def credential_status() -> dict[str, Any]:
    active = engine.config
    key_path = active.kalshi_private_key_path
    display_directory = str(credential_store.directory)
    home = str(Path.home())
    if display_directory.startswith(home):
        display_directory = display_directory.replace(home, "~", 1)
    return {
        "configured": bool(active.kalshi_api_key_id and key_path),
        "source": active.kalshi_credentials_source,
        "key_id_hint": masked_key_id(active.kalshi_api_key_id),
        "private_key_ready": bool(key_path and key_path.exists()),
        "storage_directory": display_directory,
        "local_credentials_saved": credential_store.load() is not None,
    }


@app.get("/api/credentials")
async def get_credentials() -> dict[str, Any]:
    return credential_status()


@app.post("/api/credentials")
async def save_credentials(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        saved = credential_store.save(
            str(payload.get("key_id") or ""),
            str(payload.get("private_key") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await engine.set_kalshi_credentials(
        saved.key_id, saved.private_key_path, "local form"
    )
    return credential_status()


@app.delete("/api/credentials")
async def remove_credentials() -> dict[str, Any]:
    credential_store.remove()
    key_id, key_path = environment_credentials()
    source = "environment" if key_id or key_path else "none"
    await engine.set_kalshi_credentials(key_id, key_path, source)
    return credential_status()


def clean_settings_payload(payload: dict[str, Any]) -> dict[str, Any]:
    numeric_ranges = {
        "starting_bankroll": (1.0, 100_000_000.0),
        "min_edge": (0.0, 0.50),
        "buy_edge": (0.0, 0.50),
        "minimum_buy_probability": (0.50, 0.99),
        "sell_edge": (0.0, 0.50),
        "hold_buffer": (0.0, 0.10),
        "fractional_kelly": (0.0, 1.0),
        "max_position_pct": (0.0, 1.0),
        "max_risk_per_trade_pct": (0.0, 1.0),
        "max_session_drawdown_pct": (0.0, 1.0),
        "slippage_cents": (0.0, 10.0),
        "automatic_entry_window_minutes": (0.25, 15.0),
        "automatic_confirmation_seconds": (1.0, 120.0),
        "automatic_buy_duration_pct": (0.50, 1.0),
        "early_bankroll_pct": (0.0, 1.0),
        "early_min_probability": (0.50, 0.99),
        "early_min_net_ev": (0.0, 0.50),
        "early_entry_window_seconds": (1.0, 300.0),
        "early_threshold_stability_seconds": (0.0, 120.0),
        "early_confirmation_seconds": (0.0, 120.0),
        "early_max_spread": (0.0, 0.50),
        "early_min_liquidity_contracts": (1, 1_000_000),
        "late_bankroll_pct": (0.0, 1.0),
        "late_max_seconds_remaining": (1.0, 900.0),
        "late_min_probability": (0.50, 0.99),
        "late_min_net_ev": (0.0, 0.50),
        "late_confirmation_seconds": (0.0, 120.0),
        "late_min_settlement_coverage": (0.0, 1.0),
        "late_min_z_distance": (0.0, 20.0),
        "late_max_spread": (0.0, 0.50),
        "late_min_liquidity_contracts": (1, 1_000_000),
        "minimum_liquidity_contracts": (1, 1_000_000),
        "max_data_age_seconds": (1.0, 300.0),
        "max_exchange_dispersion_pct": (0.01, 5.0),
        "minimum_exchange_feeds": (1, 3),
        "closing_guard_seconds": (1, 60),
        "settlement_min_coverage_pct": (0.10, 1.0),
        "confidence_moderate_edge": (0.0, 0.50),
        "confidence_high_edge": (0.0, 0.50),
        "confidence_moderate_max_spread": (0.0, 0.50),
        "confidence_high_max_spread": (0.0, 0.50),
        "confidence_moderate_max_variant_spread": (0.0, 1.0),
        "confidence_high_max_variant_spread": (0.0, 1.0),
        "confidence_high_min_samples": (1, 100_000),
        "confidence_high_max_calibration_error": (0.0, 1.0),
        "training_min_samples": (4, 100_000),
        "benchmark_calibration_min_samples": (4, 100_000),
        "benchmark_history_samples": (20, 100_000),
        "benchmark_uncertainty_floor_pct": (0.0, 0.01),
        "training_history_days": (1, 3650),
        "training_max_samples": (20, 1_000_000),
        "promotion_min_samples": (10, 1_000_000),
        "promotion_min_days": (1, 3650),
        "minimum_brier_improvement": (0.0, 0.25),
        "calibration_tolerance": (0.0, 0.50),
        "retraining_cadence_hours": (1, 720),
        "initial_retrain_settlements": (0, 10_000),
        "chart_window_minutes": (5, 360),
    }
    integer_settings = {
        "minimum_liquidity_contracts", "early_min_liquidity_contracts",
        "late_min_liquidity_contracts", "minimum_exchange_feeds",
        "closing_guard_seconds", "confidence_high_min_samples",
        "training_min_samples", "benchmark_calibration_min_samples",
        "training_history_days", "benchmark_history_samples", "training_max_samples",
        "promotion_min_samples", "promotion_min_days", "retraining_cadence_hours",
        "initial_retrain_settlements", "chart_window_minutes",
    }
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "default_stop_loss_cents":
            if value in (None, ""):
                cleaned[key] = None
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="default_stop_loss_cents must be numeric or blank") from exc
            if number == 0:
                cleaned[key] = None
                continue
            if not 1 <= number <= 99:
                raise HTTPException(
                    status_code=422,
                    detail="default_stop_loss_cents must be 0 (off) or between 1 and 99",
                )
            cleaned[key] = number
            continue
        if key in numeric_ranges:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"{key} must be numeric") from exc
            low, high = numeric_ranges[key]
            if not low <= number <= high:
                raise HTTPException(status_code=422, detail=f"{key} must be between {low} and {high}")
            if key in integer_settings and not number.is_integer():
                raise HTTPException(status_code=422, detail=f"{key} must be a whole number")
            cleaned[key] = int(number) if key in integer_settings else number
        elif key in {
            "paper_trading_enabled", "risk_controls_enabled",
            "early_threshold_enabled", "late_conviction_enabled",
        }:
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail=f"{key} must be true or false")
            cleaned[key] = value
        elif key == "automatic_min_confidence":
            if value not in {"Low", "Moderate", "High"}:
                raise HTTPException(status_code=422, detail="automatic_min_confidence is invalid")
            cleaned[key] = value
        elif key == "selected_side":
            side = str(value).upper()
            if side not in {"YES", "NO"}:
                raise HTTPException(status_code=422, detail="selected_side must be YES or NO")
            cleaned[key] = side
    if "min_edge" in cleaned and "buy_edge" not in cleaned:
        cleaned["buy_edge"] = cleaned["min_edge"]
    return cleaned


@app.put("/api/settings")
async def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return await engine.apply_settings(clean_settings_payload(payload))


@app.put("/api/model-side")
async def update_model_side(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    side = str(payload.get("side", "")).upper()
    if side not in {"YES", "NO"}:
        raise HTTPException(status_code=422, detail="Choose Up or Down.")
    await engine.apply_settings({"selected_side": side})
    return engine.dashboard


@app.post("/api/settings/restore/{snapshot_id}")
async def restore_settings(snapshot_id: int) -> dict[str, Any]:
    try:
        settings = await engine.restore_settings(snapshot_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"settings": settings, "snapshot_id": snapshot_id}


@app.post("/api/backtest")
async def run_backtest(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    settings = db.settings()
    min_edge = float(payload.get("min_edge", settings.get("buy_edge", settings["min_edge"])))
    if not 0 <= min_edge <= 0.5:
        raise HTTPException(status_code=422, detail="min_edge must be between 0 and 0.5")
    return BacktestService(db).run(min_edge, float(settings["starting_bankroll"]))


@app.post("/api/bootstrap")
async def run_bootstrap() -> dict[str, Any]:
    return await engine.run_bootstrap()


@app.get("/api/database")
async def database_info() -> dict[str, Any]:
    counts = {}
    for table in (
        "btc_ticks", "markets", "signal_snapshots", "settlements", "paper_trades",
        "paper_orders",
        "paper_entries", "threshold_observations", "configuration_snapshots",
        "calibration_reports", "model_versions",
    ):
        counts[table] = db.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
    return {
        "path": str(config.database_path),
        "size_bytes": config.database_path.stat().st_size if config.database_path.exists() else 0,
        "counts": counts,
    }


@app.post("/api/database/backup")
async def backup_database() -> dict[str, Any]:
    backup_dir = config.database_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"kalshi-model-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.db"
    with db.connect() as source:
        import sqlite3

        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    return {"created": str(destination), "size_bytes": destination.stat().st_size}
