from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from app.services.credentials import credential_directory, resolve_credentials


FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def default_database_path() -> Path:
    configured = os.getenv("KALSHI_MODEL_DB")
    if configured:
        return Path(configured).expanduser()
    if FROZEN:
        return credential_directory() / "data" / "kalshi_model.db"
    return ROOT / "data" / "kalshi_model.db"


def credential_key_id() -> str | None:
    return resolve_credentials()[0]


def credential_key_path() -> Path | None:
    return resolve_credentials()[1]


def credential_source() -> str:
    return resolve_credentials()[2]


@dataclass(frozen=True)
class AppConfig:
    database_path: Path = field(default_factory=default_database_path)
    kalshi_api_base: str = os.getenv(
        "KALSHI_API_BASE", "https://external-api.kalshi.com/trade-api/v2"
    )
    kalshi_series: str = os.getenv("KALSHI_SERIES", "KXBTC15M")
    kalshi_ws_url: str = os.getenv(
        "KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    )
    kalshi_demo_api_base: str = os.getenv(
        "KALSHI_DEMO_API_BASE",
        "https://external-api.demo.kalshi.co/trade-api/v2",
    )
    kalshi_demo_ws_url: str = os.getenv(
        "KALSHI_DEMO_WS_URL",
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    )
    kalshi_live_api_base: str = os.getenv(
        "KALSHI_LIVE_API_BASE",
        "https://external-api.kalshi.com/trade-api/v2",
    )
    kalshi_live_ws_url: str = os.getenv(
        "KALSHI_LIVE_WS_URL",
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    )
    kalshi_api_key_id: str | None = field(default_factory=credential_key_id)
    kalshi_private_key_path: Path | None = field(default_factory=credential_key_path)
    kalshi_credentials_source: str = field(default_factory=credential_source)
    host: str = os.getenv("KALSHI_MODEL_HOST", "127.0.0.1")
    port: int = int(os.getenv("KALSHI_MODEL_PORT", "8765"))
    mobile_port: int = int(os.getenv("KALSHI_MODEL_MOBILE_PORT", "8767"))
    poll_seconds: float = max(2.0, float(os.getenv("KALSHI_MODEL_POLL_SECONDS", "5")))
    live_update_seconds: float = max(
        0.1, float(os.getenv("KALSHI_MODEL_LIVE_UPDATE_SECONDS", "0.1"))
    )
    open_browser: bool = os.getenv(
        "KALSHI_MODEL_OPEN_BROWSER", "1" if FROZEN else "0"
    ) == "1"


DEFAULT_SETTINGS: dict[str, object] = {
    "mobile_monitor_enabled": False,
    "starting_bankroll": 1000.0,
    "paper_trading_enabled": True,
    "risk_controls_enabled": True,
    "selected_side": "YES",
    "buy_edge": 0.10,
    "minimum_buy_probability": 0.65,
    "sell_edge": 0.03,
    "hold_buffer": 0.005,
    "min_edge": 0.10,
    "fractional_kelly": 0.25,
    "max_position_pct": 0.05,
    "max_risk_per_trade_pct": 0.05,
    "max_session_drawdown_pct": 0.50,
    "slippage_cents": 0.5,
    "default_stop_loss_cents": None,
    "global_profit_take_enabled": True,
    "global_profit_take_price": 0.99,
    "threshold_breach_exit_enabled": True,
    "threshold_breach_exit_buffer_dollars": 0.0,
    "automatic_entry_window_minutes": 15,
    "automatic_confirmation_seconds": 5,
    "automatic_buy_duration_pct": 0.50,
    "automatic_min_confidence": "Moderate",
    "threshold_margin_gate_dollars": 50.0,
    "maximum_margin_volatility": 0.0,
    "early_threshold_enabled": True,
    "early_bankroll_pct": 0.03,
    "early_min_probability": 0.65,
    "early_min_net_ev": 0.005,
    "early_entry_window_seconds": 60,
    "early_threshold_stability_seconds": 1,
    "early_confirmation_seconds": 2,
    "early_max_spread": 0.20,
    "early_min_liquidity_contracts": 1,
    "late_conviction_enabled": True,
    "late_bankroll_pct": 0.03,
    "late_max_seconds_remaining": 120,
    "late_min_probability": 0.79,
    "late_min_net_ev": 0.005,
    "late_confirmation_seconds": 3,
    "late_min_settlement_coverage": 0.80,
    "late_min_z_distance": 2.0,
    "late_max_spread": 0.03,
    "late_min_liquidity_contracts": 1,
    "swing_enabled": False,
    "swing_entry_window_seconds": 300,
    "swing_max_entry_price": 0.05,
    "swing_target_exit_price": 0.10,
    "swing_bankroll_pct": 0.01,
    "swing_min_model_advantage": 0.03,
    "swing_fallback_mode": "Exit",
    "swing_fallback_seconds_remaining": 120,
    "swing_stop_loss_cents": None,
    "swing_max_spread": 0.03,
    "swing_min_liquidity_contracts": 1,
    "swing_confirmation_seconds": 0,
    "minimum_liquidity_contracts": 1,
    "max_data_age_seconds": 20,
    "max_exchange_dispersion_pct": 0.40,
    "minimum_exchange_feeds": 2,
    "closing_guard_seconds": 10,
    "settlement_min_coverage_pct": 0.50,
    "confidence_moderate_edge": 0.06,
    "confidence_high_edge": 0.10,
    "confidence_moderate_max_spread": 0.03,
    "confidence_high_max_spread": 0.02,
    "confidence_moderate_max_variant_spread": 0.07,
    "confidence_high_max_variant_spread": 0.04,
    "confidence_high_min_samples": 150,
    "confidence_high_max_calibration_error": 0.07,
    "training_min_samples": 12,
    "benchmark_calibration_min_samples": 20,
    "training_history_days": 365,
    "benchmark_history_samples": 120,
    "benchmark_uncertainty_floor_pct": 0.00015,
    "training_max_samples": 1000,
    "promotion_min_samples": 120,
    "promotion_min_days": 7,
    "minimum_brier_improvement": 0.005,
    "calibration_tolerance": 0.01,
    "retraining_cadence_hours": 24,
    "initial_retrain_settlements": 20,
    "chart_window_minutes": 5,
    "kalshi_series": "KXBTC15M",
    "trading_mode": "PAPER",
    "demo_automatic_trading_enabled": False,
    "live_automatic_trading_enabled": False,
    "demo_bankroll_cap_pct": 1.0,
    "live_bankroll_cap_pct": 1.0,
    "demo_max_total_allocated_capital": 1000000.0,
    "live_max_total_allocated_capital": 1000000.0,
    "demo_max_amount_per_order": 1000000.0,
    "live_max_amount_per_order": 1000000.0,
    "demo_max_exposure_per_market": 1000000.0,
    "live_max_exposure_per_market": 1000000.0,
    "demo_max_total_open_exposure": 1000000.0,
    "live_max_total_open_exposure": 1000000.0,
    "demo_max_open_orders": 100,
    "live_max_open_orders": 100,
    "demo_max_daily_loss": 1000000.0,
    "live_max_daily_loss": 1000000.0,
    "demo_max_daily_order_count": 1000,
    "live_max_daily_order_count": 1000,
    "demo_max_entry_price": 0.99,
    "live_max_entry_price": 0.99,
    "demo_max_spread": 0.20,
    "live_max_spread": 0.20,
    "demo_min_liquidity": 1,
    "live_min_liquidity": 1,
    "demo_min_data_quality": "Moderate",
    "live_min_data_quality": "Moderate",
    "demo_entry_timeout_seconds": 15,
    "live_entry_timeout_seconds": 15,
}
