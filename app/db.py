from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.config import DEFAULT_SETTINGS
from app.domain import iso_now


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS exchange_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            exchange TEXT NOT NULL,
            price REAL NOT NULL,
            bid REAL,
            ask REAL,
            volume REAL,
            latency_ms REAL
        );
        CREATE INDEX IF NOT EXISTS idx_exchange_quotes_time ON exchange_quotes(observed_at);

        CREATE TABLE IF NOT EXISTS btc_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            composite_price REAL NOT NULL,
            dispersion_pct REAL NOT NULL,
            exchange_count INTEGER NOT NULL,
            volatility_5m REAL,
            volatility_15m REAL,
            volatility_60m REAL,
            momentum_1m REAL,
            momentum_5m REAL,
            volume_acceleration REAL,
            source_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_btc_ticks_time ON btc_ticks(observed_at);

        CREATE TABLE IF NOT EXISTS markets (
            ticker TEXT PRIMARY KEY,
            event_ticker TEXT,
            status TEXT NOT NULL,
            title TEXT,
            strike REAL,
            open_time TEXT,
            close_time TEXT,
            expected_expiration_time TEXT,
            result TEXT,
            rules_primary TEXT,
            rules_secondary TEXT,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kalshi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            spread REAL,
            liquidity REAL,
            open_interest REAL,
            volume REAL,
            yes_bid_size REAL,
            yes_ask_size REAL,
            imbalance REAL,
            rapid_repricing REAL,
            orderbook_json TEXT,
            FOREIGN KEY(ticker) REFERENCES markets(ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_kalshi_snapshots_ticker_time
            ON kalshi_snapshots(ticker, observed_at);

        CREATE TABLE IF NOT EXISTS signal_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            signal TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            confidence TEXT NOT NULL,
            explanation TEXT NOT NULL,
            model_probability REAL,
            market_probability REAL,
            edge REAL,
            expected_value REAL,
            suggested_fraction REAL,
            suggested_dollars REAL,
            suggested_contracts INTEGER,
            model_version TEXT NOT NULL,
            input_json TEXT NOT NULL,
            btc_state_json TEXT NOT NULL,
            kalshi_state_json TEXT NOT NULL,
            material_reason TEXT NOT NULL,
            FOREIGN KEY(ticker) REFERENCES markets(ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_signals_ticker_time
            ON signal_snapshots(ticker, observed_at);

        CREATE TABLE IF NOT EXISTS settlements (
            ticker TEXT PRIMARY KEY,
            settled_at TEXT NOT NULL,
            result INTEGER NOT NULL,
            settlement_value REAL,
            raw_json TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            FOREIGN KEY(ticker) REFERENCES markets(ticker)
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            entry_price REAL NOT NULL,
            contracts INTEGER NOT NULL,
            entry_cost REAL NOT NULL,
            fees REAL NOT NULL,
            model_probability REAL NOT NULL,
            market_probability REAL NOT NULL,
            edge REAL NOT NULL,
            expected_value REAL NOT NULL,
            confidence TEXT NOT NULL,
            model_version TEXT NOT NULL,
            status TEXT NOT NULL,
            settled_at TEXT,
            outcome INTEGER,
            payout REAL,
            realized_pnl REAL,
            UNIQUE(ticker, side)
        );

        CREATE TABLE IF NOT EXISTS model_versions (
            version TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            model_type TEXT NOT NULL,
            status TEXT NOT NULL,
            training_samples INTEGER NOT NULL,
            validation_json TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            promoted_at TEXT,
            parent_version TEXT
        );

        CREATE TABLE IF NOT EXISTS calibration_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            trigger TEXT NOT NULL,
            tldr TEXT NOT NULL,
            settled_contracts INTEGER NOT NULL,
            active_model_version TEXT NOT NULL,
            candidate_model_version TEXT,
            promoted INTEGER NOT NULL,
            brier_before REAL,
            brier_after REAL,
            calibration_error REAL,
            report_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            results_json TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        ALTER TABLE btc_ticks ADD COLUMN high_low_5m_pct REAL;
        """,
    ),
    (
        3,
        """
        ALTER TABLE paper_trades RENAME TO paper_trades_legacy;
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            entry_price REAL NOT NULL,
            contracts INTEGER NOT NULL,
            entry_cost REAL NOT NULL,
            fees REAL NOT NULL,
            model_probability REAL NOT NULL,
            market_probability REAL NOT NULL,
            edge REAL NOT NULL,
            expected_value REAL NOT NULL,
            confidence TEXT NOT NULL,
            model_version TEXT NOT NULL,
            status TEXT NOT NULL,
            settled_at TEXT,
            outcome INTEGER,
            payout REAL,
            realized_pnl REAL,
            source TEXT NOT NULL DEFAULT 'automatic'
        );
        INSERT INTO paper_trades(
            id,ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
            model_probability,market_probability,edge,expected_value,confidence,
            model_version,status,settled_at,outcome,payout,realized_pnl,source
        )
        SELECT
            id,ticker,side,opened_at,entry_price,contracts,entry_cost,fees,
            model_probability,market_probability,edge,expected_value,confidence,
            model_version,status,settled_at,outcome,payout,realized_pnl,'automatic'
        FROM paper_trades_legacy;
        DROP TABLE paper_trades_legacy;
        CREATE INDEX idx_paper_trades_ticker_status
            ON paper_trades(ticker, status);

        CREATE TABLE paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            order_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            requested_dollars REAL,
            requested_contracts INTEGER,
            limit_price REAL,
            filled_price REAL,
            filled_contracts INTEGER,
            fees REAL NOT NULL DEFAULT 0,
            realized_pnl REAL,
            filled_at TEXT,
            canceled_at TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            error TEXT,
            FOREIGN KEY(ticker) REFERENCES markets(ticker)
        );
        CREATE INDEX idx_paper_orders_status_ticker
            ON paper_orders(status, ticker);
        """,
    ),
    (
        4,
        """
        UPDATE model_versions SET status='retired' WHERE status='active';
        INSERT OR IGNORE INTO model_versions(
            version, created_at, model_type, status, training_samples,
            validation_json, parameters_json, promoted_at, parent_version
        ) VALUES (
            'baseline-1.1', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            'settlement-average', 'active', 0,
            '{"note":"Settlement-aware analytical baseline"}',
            '{"volatility_floor":0.15,"volatility_cap":2.5,"settlement_window_seconds":60}',
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'baseline-1.0'
        );
        """,
    ),
    (
        5,
        """
        ALTER TABLE paper_orders ADD COLUMN stop_loss_price REAL;
        ALTER TABLE paper_orders ADD COLUMN entry_id INTEGER;

        CREATE TABLE paper_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            order_id INTEGER,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            entry_price REAL NOT NULL,
            initial_contracts INTEGER NOT NULL,
            remaining_contracts INTEGER NOT NULL,
            entry_cost REAL NOT NULL,
            entry_fees REAL NOT NULL,
            stop_loss_price REAL,
            stop_status TEXT,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            closed_at TEXT,
            FOREIGN KEY(trade_id) REFERENCES paper_trades(id),
            FOREIGN KEY(order_id) REFERENCES paper_orders(id),
            FOREIGN KEY(ticker) REFERENCES markets(ticker)
        );
        CREATE INDEX idx_paper_entries_active_stop
            ON paper_entries(ticker, status, stop_status);
        CREATE UNIQUE INDEX idx_one_automatic_entry_per_outcome
            ON paper_entries(ticker, side) WHERE source='automatic';

        INSERT INTO paper_entries(
            trade_id,ticker,side,opened_at,entry_price,initial_contracts,
            remaining_contracts,entry_cost,entry_fees,source,status,closed_at
        )
        SELECT
            id,ticker,side,opened_at,entry_price,contracts,
            CASE WHEN status='open' THEN contracts ELSE 0 END,
            entry_cost,fees,source,status,settled_at
        FROM paper_trades;

        CREATE TABLE configuration_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            changed_json TEXT NOT NULL,
            restored_from_id INTEGER,
            FOREIGN KEY(restored_from_id) REFERENCES configuration_snapshots(id)
        );
        """,
    ),
    (
        6,
        """
        ALTER TABLE signal_snapshots ADD COLUMN forecast_signal TEXT;
        ALTER TABLE signal_snapshots ADD COLUMN forecast_explanation TEXT;
        """,
    ),
    (
        7,
        """
        ALTER TABLE paper_orders ADD COLUMN strategy TEXT;
        ALTER TABLE paper_entries ADD COLUMN strategy TEXT;
        ALTER TABLE paper_entries ADD COLUMN model_probability REAL;
        ALTER TABLE paper_entries ADD COLUMN expected_value REAL;
        ALTER TABLE paper_entries ADD COLUMN entry_reason TEXT;
        ALTER TABLE paper_trades ADD COLUMN strategy TEXT;

        CREATE TABLE threshold_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            threshold REAL NOT NULL,
            observed_at TEXT NOT NULL,
            market_status TEXT,
            open_time TEXT,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            revision INTEGER NOT NULL,
            changed INTEGER NOT NULL
        );
        CREATE INDEX idx_threshold_observations_ticker_time
            ON threshold_observations(ticker, observed_at);

        UPDATE settings SET value_json='0.03', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE key='max_risk_per_trade_pct' AND value_json='0.02'
          AND NOT EXISTS (
              SELECT 1 FROM configuration_snapshots
              WHERE changed_json LIKE '%"max_risk_per_trade_pct"%'
          );
        """,
    ),
]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, sql in MIGRATIONS:
                if version not in current:
                    connection.executescript(sql)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, iso_now()),
                    )
            for key, value in DEFAULT_SETTINGS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                    (key, json.dumps(value), iso_now()),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO model_versions(
                    version, created_at, model_type, status, training_samples,
                    validation_json, parameters_json, promoted_at, parent_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "baseline-1.1",
                    iso_now(),
                    "settlement-average",
                    "active",
                    0,
                    json.dumps({"note": "Settlement-aware analytical baseline"}),
                    json.dumps(
                        {
                            "volatility_floor": 0.15,
                            "volatility_cap": 2.5,
                            "settlement_window_seconds": 60,
                        }
                    ),
                    iso_now(),
                    "baseline-1.0",
                ),
            )

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self.transaction() as connection:
            connection.executemany(sql, rows)

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def settings(self) -> dict[str, Any]:
        rows = self.fetch_all("SELECT key, value_json FROM settings")
        stored = {row["key"]: json.loads(row["value_json"]) for row in rows}
        return {**DEFAULT_SETTINGS, **stored}

    def update_settings(
        self,
        updates: dict[str, Any],
        *,
        record_snapshot: bool = True,
        restored_from_id: int | None = None,
    ) -> dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS)
        before = self.settings()
        cleaned = {key: value for key, value in updates.items() if key in allowed}
        changed = {
            key: {"before": before.get(key), "after": value}
            for key, value in cleaned.items()
            if before.get(key) != value
        }
        with self.transaction() as connection:
            for key, value in cleaned.items():
                connection.execute(
                    """
                    INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (key, json.dumps(value), iso_now()),
                )
            if changed and record_snapshot:
                snapshot = {**before, **cleaned}
                connection.execute(
                    """
                    INSERT INTO configuration_snapshots(
                        created_at,settings_json,changed_json,restored_from_id
                    ) VALUES (?,?,?,?)
                    """,
                    (
                        iso_now(), json.dumps(snapshot), json.dumps(changed),
                        restored_from_id,
                    ),
                )
        return self.settings()

    def configuration_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.fetch_all(
            "SELECT * FROM configuration_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["settings"] = json.loads(row.pop("settings_json"))
            row["changed"] = json.loads(row.pop("changed_json"))
        return rows

    def restore_configuration(self, snapshot_id: int) -> dict[str, Any]:
        row = self.fetch_one(
            "SELECT settings_json FROM configuration_snapshots WHERE id=?",
            (snapshot_id,),
        )
        if not row:
            raise ValueError("Configuration snapshot not found.")
        values = json.loads(row["settings_json"])
        return self.update_settings(values, restored_from_id=snapshot_id)
