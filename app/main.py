from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, ROOT
from app.db import Database
from app.engine import AnalysisEngine
from app.services.backtest import BacktestService
from app.services.training import report_rows


config = AppConfig()
db = Database(config.database_path)
engine = AnalysisEngine(config, db)
templates = Jinja2Templates(directory=ROOT / "app" / "templates")


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
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


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
async def chart(minutes: int = Query(default=90, ge=15, le=360)) -> dict[str, Any]:
    return {"minutes": minutes, "points": engine.chart(minutes)}


@app.get("/api/paper")
async def paper() -> dict[str, Any]:
    return engine.paper.portfolio()


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
    return {"summary": engine.calibration_summary(), "reports": report_rows(db)}


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


@app.put("/api/settings")
async def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    numeric_ranges = {
        "starting_bankroll": (1.0, 100_000_000.0),
        "min_edge": (0.0, 0.50),
        "fractional_kelly": (0.0, 1.0),
        "max_position_pct": (0.0, 1.0),
        "max_risk_per_trade_pct": (0.0, 1.0),
        "max_session_drawdown_pct": (0.0, 1.0),
        "slippage_cents": (0.0, 10.0),
        "max_data_age_seconds": (5.0, 300.0),
        "max_exchange_dispersion_pct": (0.01, 5.0),
        "chart_window_minutes": (15, 360),
    }
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if key in numeric_ranges:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"{key} must be numeric") from exc
            low, high = numeric_ranges[key]
            if not low <= number <= high:
                raise HTTPException(status_code=422, detail=f"{key} must be between {low} and {high}")
            cleaned[key] = int(number) if key == "chart_window_minutes" else number
        elif key in {"paper_trading_enabled", "risk_controls_enabled"}:
            cleaned[key] = bool(value)
    return db.update_settings(cleaned)


@app.post("/api/backtest")
async def run_backtest(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    settings = db.settings()
    min_edge = float(payload.get("min_edge", settings["min_edge"]))
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
