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
    (
        8,
        """
        ALTER TABLE paper_orders ADD COLUMN available_cash_after REAL;
        ALTER TABLE paper_entries ADD COLUMN available_cash_after REAL;
        ALTER TABLE paper_trades ADD COLUMN available_cash_after REAL;
        """,
    ),
    (
        9,
        """
        ALTER TABLE paper_entries ADD COLUMN target_exit_price REAL;
        ALTER TABLE paper_entries ADD COLUMN fallback_exit_mode TEXT;
        ALTER TABLE paper_entries ADD COLUMN fallback_exit_seconds REAL;
        ALTER TABLE paper_entries ADD COLUMN exit_reason TEXT;
        ALTER TABLE paper_entries ADD COLUMN exit_price REAL;
        ALTER TABLE paper_entries ADD COLUMN exit_fees REAL;
        ALTER TABLE paper_entries ADD COLUMN max_favorable_bid REAL;
        ALTER TABLE paper_entries ADD COLUMN min_adverse_bid REAL;
        """,
    ),
    (
        10,
        """
        ALTER TABLE paper_entries ADD COLUMN strategy_metadata_json TEXT;
        """,
    ),
    (
        11,
        """
        CREATE TABLE broker_order_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL CHECK(mode IN ('DEMO','LIVE')),
            client_order_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('YES','NO')),
            action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
            requested_contracts INTEGER NOT NULL,
            limit_price REAL NOT NULL,
            status TEXT NOT NULL,
            strategy TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            exchange_order_id TEXT,
            stop_loss_price REAL,
            target_exit_price REAL,
            fallback_exit_mode TEXT,
            fallback_exit_seconds REAL,
            cancel_deadline_at TEXT,
            decision_snapshot_json TEXT NOT NULL DEFAULT '{}',
            risk_snapshot_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            UNIQUE(mode, client_order_id)
        );
        CREATE INDEX idx_broker_intents_mode_status
            ON broker_order_intents(mode, status, created_at);
        CREATE INDEX idx_broker_intents_market
            ON broker_order_intents(mode, ticker, strategy);

        CREATE TABLE broker_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL CHECK(mode IN ('DEMO','LIVE')),
            exchange_order_id TEXT NOT NULL,
            client_order_id TEXT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('YES','NO')),
            action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
            status TEXT NOT NULL,
            requested_contracts REAL NOT NULL DEFAULT 0,
            filled_contracts REAL NOT NULL DEFAULT 0,
            remaining_contracts REAL NOT NULL DEFAULT 0,
            limit_price REAL,
            average_fill_price REAL,
            fees REAL NOT NULL DEFAULT 0,
            strategy TEXT,
            source TEXT,
            created_at TEXT,
            updated_at TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(mode, exchange_order_id)
        );
        CREATE INDEX idx_broker_orders_mode_status
            ON broker_orders(mode, status, updated_at);

        CREATE TABLE broker_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL CHECK(mode IN ('DEMO','LIVE')),
            fill_id TEXT NOT NULL,
            exchange_order_id TEXT,
            client_order_id TEXT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('YES','NO')),
            action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
            contracts REAL NOT NULL,
            price REAL NOT NULL,
            fee REAL NOT NULL DEFAULT 0,
            strategy TEXT,
            source TEXT,
            filled_at TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(mode, fill_id)
        );
        CREATE INDEX idx_broker_fills_mode_time
            ON broker_fills(mode, filled_at);

        CREATE TABLE broker_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL CHECK(mode IN ('DEMO','LIVE')),
            ticker TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('YES','NO')),
            contracts REAL NOT NULL,
            average_price REAL,
            market_exposure REAL NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0,
            fees REAL NOT NULL DEFAULT 0,
            strategy TEXT,
            source TEXT,
            stop_loss_price REAL,
            target_exit_price REAL,
            fallback_exit_mode TEXT,
            fallback_exit_seconds REAL,
            opened_at TEXT,
            updated_at TEXT NOT NULL,
            market_result TEXT,
            position_won INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            UNIQUE(mode, ticker, side)
        );
        CREATE INDEX idx_broker_positions_mode_status
            ON broker_positions(mode, status, updated_at);

        CREATE TABLE broker_account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL CHECK(mode IN ('DEMO','LIVE')),
            observed_at TEXT NOT NULL,
            available_balance REAL NOT NULL,
            portfolio_value REAL NOT NULL,
            allocated_capital REAL NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_broker_accounts_mode_time
            ON broker_account_snapshots(mode, observed_at);

        CREATE TABLE broker_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL CHECK(mode IN ('DEMO','LIVE')),
            ticker TEXT NOT NULL,
            side TEXT,
            settled_at TEXT NOT NULL,
            market_result TEXT,
            position_won INTEGER,
            realized_pnl REAL,
            fees REAL NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(mode, ticker)
        );
        CREATE INDEX idx_broker_settlements_mode_time
            ON broker_settlements(mode, settled_at);

        CREATE TABLE broker_mode_state (
            mode TEXT PRIMARY KEY CHECK(mode IN ('DEMO','LIVE')),
            connected INTEGER NOT NULL DEFAULT 0,
            authenticated INTEGER NOT NULL DEFAULT 0,
            reconciled INTEGER NOT NULL DEFAULT 0,
            reconciliation_required INTEGER NOT NULL DEFAULT 1,
            demo_verified_at TEXT,
            limits_reviewed_at TEXT,
            kill_switch INTEGER NOT NULL DEFAULT 0,
            last_reconciled_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO broker_mode_state(mode,updated_at) VALUES
            ('DEMO', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            ('LIVE', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

        CREATE TABLE broker_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL CHECK(mode IN ('DEMO','LIVE')),
            created_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            client_order_id TEXT,
            exchange_order_id TEXT,
            ticker TEXT,
            detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_broker_audit_mode_time
            ON broker_audit_events(mode, created_at);
        """,
    ),
    (
        12,
        """
        ALTER TABLE broker_fills ADD COLUMN available_cash_after REAL;
        ALTER TABLE broker_settlements ADD COLUMN available_cash_after REAL;

        UPDATE broker_fills
        SET available_cash_after = (
            SELECT snapshot.available_balance
            FROM broker_account_snapshots AS snapshot
            WHERE snapshot.mode=broker_fills.mode
              AND julianday(snapshot.observed_at) >= julianday(broker_fills.filled_at)
              AND (julianday(snapshot.observed_at)-julianday(broker_fills.filled_at))*86400 <= 30
            ORDER BY julianday(snapshot.observed_at) ASC, snapshot.id ASC
            LIMIT 1
        )
        WHERE available_cash_after IS NULL;

        UPDATE broker_settlements
        SET available_cash_after = (
            SELECT snapshot.available_balance
            FROM broker_account_snapshots AS snapshot
            WHERE snapshot.mode=broker_settlements.mode
              AND julianday(snapshot.observed_at) >= julianday(broker_settlements.settled_at)
              AND (julianday(snapshot.observed_at)-julianday(broker_settlements.settled_at))*86400 <= 30
            ORDER BY julianday(snapshot.observed_at) ASC, snapshot.id ASC
            LIMIT 1
        )
        WHERE available_cash_after IS NULL;
        """,
    ),
    (
        13,
        """
        CREATE TABLE margin_volatility_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            threshold REAL NOT NULL,
            btc_proxy REAL NOT NULL,
            margin REAL NOT NULL,
            raw_realized_volatility REAL,
            movement_intensity REAL,
            reversal_component REAL,
            raw_score REAL,
            mvi REAL,
            expected_remaining_move REAL,
            cushion_ratio REAL,
            seconds_remaining REAL,
            coverage REAL NOT NULL DEFAULT 0,
            reliable INTEGER NOT NULL DEFAULT 0,
            reliability_state TEXT NOT NULL,
            calculation_version TEXT NOT NULL,
            UNIQUE(observed_at, calculation_version)
        );
        CREATE INDEX idx_margin_volatility_time
            ON margin_volatility_observations(observed_at);
        CREATE INDEX idx_margin_volatility_version_score
            ON margin_volatility_observations(calculation_version, reliable, raw_score);

        ALTER TABLE signal_snapshots ADD COLUMN margin_volatility_index REAL;
        ALTER TABLE signal_snapshots ADD COLUMN margin_cushion_ratio REAL;
        ALTER TABLE signal_snapshots ADD COLUMN margin_volatility_max REAL;
        ALTER TABLE paper_entries ADD COLUMN margin_volatility_index REAL;
        ALTER TABLE paper_entries ADD COLUMN margin_cushion_ratio REAL;
        ALTER TABLE broker_order_intents ADD COLUMN margin_volatility_index REAL;
        ALTER TABLE broker_order_intents ADD COLUMN margin_cushion_ratio REAL;
        """,
    ),
    (
        14,
        """
        CREATE TABLE trade_review_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            environment TEXT NOT NULL CHECK(environment IN ('PAPER','DEMO','LIVE')),
            ticker TEXT NOT NULL,
            market_open_time TEXT,
            market_close_time TEXT,
            recording_started_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            finalized_at TEXT,
            status TEXT NOT NULL DEFAULT 'RECORDING',
            settlement_result TEXT,
            settlement_value REAL,
            settlement_margin REAL,
            expected_regular_points INTEGER NOT NULL DEFAULT 180,
            regular_point_count INTEGER NOT NULL DEFAULT 0,
            coverage REAL,
            gap_count INTEGER NOT NULL DEFAULT 0,
            calculation_version TEXT NOT NULL DEFAULT 'trade-review-1',
            UNIQUE(environment,ticker)
        );
        CREATE INDEX idx_trade_review_sessions_environment_status
            ON trade_review_sessions(environment,status,market_close_time);

        CREATE TABLE trade_review_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            sample_kind TEXT NOT NULL DEFAULT 'REGULAR',
            seconds_remaining REAL,
            threshold REAL,
            btc_proxy REAL,
            margin REAL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            spread REAL,
            liquidity REAL,
            open_interest REAL,
            volume REAL,
            up_probability REAL,
            forecast_signal TEXT,
            mvi REAL,
            expected_remaining_move REAL,
            cushion_ratio REAL,
            data_reliable INTEGER,
            readiness_status TEXT,
            readiness_side TEXT,
            readiness_blocker TEXT,
            model_version TEXT,
            configuration_snapshot_id INTEGER,
            state_json TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES trade_review_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY(configuration_snapshot_id) REFERENCES configuration_snapshots(id),
            UNIQUE(session_id,observed_at,sample_kind)
        );
        CREATE INDEX idx_trade_review_points_session_time
            ON trade_review_points(session_id,observed_at);

        CREATE TABLE trade_review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            environment TEXT NOT NULL CHECK(environment IN ('PAPER','DEMO','LIVE')),
            trade_ref TEXT,
            side TEXT,
            action TEXT,
            price REAL,
            contracts REAL,
            fees REAL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            state_hash TEXT,
            FOREIGN KEY(session_id) REFERENCES trade_review_sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX idx_trade_review_events_session_time
            ON trade_review_events(session_id,observed_at,id);
        CREATE INDEX idx_trade_review_events_trade_ref
            ON trade_review_events(environment,trade_ref,observed_at);

        CREATE TABLE trade_review_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            environment TEXT NOT NULL CHECK(environment IN ('PAPER','DEMO','LIVE')),
            trade_ref TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT,
            ticker TEXT NOT NULL,
            side TEXT,
            opened_at TEXT,
            closed_at TEXT,
            status TEXT,
            strategy TEXT,
            FOREIGN KEY(session_id) REFERENCES trade_review_sessions(id) ON DELETE CASCADE,
            UNIQUE(environment,trade_ref)
        );
        CREATE INDEX idx_trade_review_links_environment_ref
            ON trade_review_links(environment,trade_ref);
        CREATE INDEX idx_trade_review_links_ticker
            ON trade_review_links(environment,ticker,opened_at);
        """,
    ),
    (
        15,
        """
        CREATE TABLE btc_volume_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            exchange TEXT NOT NULL,
            cumulative_volume REAL,
            price REAL,
            source_window TEXT NOT NULL DEFAULT 'rolling_24h',
            valid INTEGER NOT NULL DEFAULT 1,
            reason TEXT,
            UNIQUE(exchange, observed_at)
        );
        CREATE INDEX idx_btc_volume_exchange_time
            ON btc_volume_observations(exchange, observed_at);

        CREATE TABLE btc_trade_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            exchange TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            taker_side TEXT NOT NULL CHECK(taker_side IN ('BUY','SELL')),
            signed_size REAL NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(exchange, trade_id)
        );
        CREATE INDEX idx_btc_trade_ticks_time
            ON btc_trade_ticks(observed_at, exchange);

        CREATE TABLE kalshi_trade_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            price REAL,
            contracts REAL NOT NULL,
            taker_side TEXT CHECK(taker_side IN ('YES','NO')),
            is_block_trade INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(ticker, trade_id)
        );
        CREATE INDEX idx_kalshi_trade_ticks_ticker_time
            ON kalshi_trade_ticks(ticker, observed_at);

        CREATE TABLE volume_signal_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            ticker TEXT,
            status TEXT NOT NULL,
            calculation_version TEXT NOT NULL,
            data_completeness REAL NOT NULL DEFAULT 0,
            venue_agreement REAL,
            btc_rvol_1m REAL,
            btc_rvol_5m REAL,
            btc_flow_imbalance_1m REAL,
            btc_flow_imbalance_5m REAL,
            btc_cvd_slope_1m REAL,
            btc_cvd_slope_5m REAL,
            btc_volume_confirmation_1m REAL,
            btc_volume_confirmation_5m REAL,
            btc_vwap_distance_1m REAL,
            btc_vwap_distance_5m REAL,
            btc_vwap_z_1m REAL,
            btc_vwap_z_5m REAL,
            kalshi_flow_imbalance_1m REAL,
            kalshi_turnover_5m REAL,
            kalshi_turnover_change REAL,
            btc_kalshi_flow_agreement REAL,
            values_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_volume_signal_snapshots_time
            ON volume_signal_snapshots(observed_at, ticker);

        ALTER TABLE trade_review_points ADD COLUMN volume_signals_json TEXT;
        """,
    ),
    (
        16,
        """
        ALTER TABLE paper_entries ADD COLUMN threshold_breach_enabled INTEGER;
        ALTER TABLE paper_entries ADD COLUMN threshold_exit_buffer REAL;
        ALTER TABLE paper_entries ADD COLUMN threshold_exit_level REAL;
        ALTER TABLE paper_entries ADD COLUMN threshold_trigger_btc_proxy REAL;
        ALTER TABLE paper_entries ADD COLUMN threshold_trigger_threshold REAL;
        ALTER TABLE paper_entries ADD COLUMN threshold_triggered_at TEXT;
        ALTER TABLE paper_entries ADD COLUMN threshold_exit_status TEXT;
        ALTER TABLE paper_entries ADD COLUMN threshold_exit_block_reason TEXT;

        ALTER TABLE broker_positions ADD COLUMN threshold_breach_enabled INTEGER;
        ALTER TABLE broker_positions ADD COLUMN threshold_exit_buffer REAL;
        ALTER TABLE broker_positions ADD COLUMN threshold_exit_level REAL;
        ALTER TABLE broker_positions ADD COLUMN threshold_trigger_btc_proxy REAL;
        ALTER TABLE broker_positions ADD COLUMN threshold_trigger_threshold REAL;
        ALTER TABLE broker_positions ADD COLUMN threshold_triggered_at TEXT;
        ALTER TABLE broker_positions ADD COLUMN threshold_exit_status TEXT;
        ALTER TABLE broker_positions ADD COLUMN threshold_exit_block_reason TEXT;
        """,
    ),
    (
        17,
        """
        ALTER TABLE broker_order_intents ADD COLUMN error_code TEXT;
        ALTER TABLE broker_order_intents ADD COLUMN error_details_json TEXT;

        ALTER TABLE broker_positions ADD COLUMN threshold_exit_last_attempt_at TEXT;
        ALTER TABLE broker_positions ADD COLUMN threshold_exit_last_attempt_bid REAL;
        ALTER TABLE broker_positions ADD COLUMN threshold_exit_error_code TEXT;
        ALTER TABLE broker_positions ADD COLUMN threshold_exit_error_details_json TEXT;

        -- Settlement is not evidence that a protective exit filled. Repair
        -- earlier history that was incorrectly labeled Exited on settlement.
        UPDATE broker_positions
        SET threshold_exit_status='Blocked',
            threshold_exit_block_reason=COALESCE(
                threshold_exit_block_reason,
                (
                    SELECT i.error FROM broker_order_intents i
                    WHERE i.mode=broker_positions.mode
                      AND i.ticker=broker_positions.ticker
                      AND i.side=broker_positions.side
                      AND i.action='SELL'
                      AND i.source='threshold_breach_exit'
                      AND i.status='REJECTED'
                    ORDER BY i.id DESC LIMIT 1
                ),
                'Threshold exit was not confirmed before settlement.'
            )
        WHERE threshold_exit_status='Exited'
          AND threshold_triggered_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM broker_fills f
              WHERE f.mode=broker_positions.mode
                AND f.ticker=broker_positions.ticker
                AND f.side=broker_positions.side
                AND f.action='SELL'
                AND f.source='threshold_breach_exit'
          );
        """,
    ),
    (
        18,
        """
        -- Prefer the actual rejected protective-order reason over a later
        -- data-quality message that could overwrite it on older rows.
        UPDATE broker_positions
        SET threshold_exit_status='Blocked',
            threshold_exit_block_reason=(
                SELECT i.error FROM broker_order_intents i
                WHERE i.mode=broker_positions.mode
                  AND i.ticker=broker_positions.ticker
                  AND i.side=broker_positions.side
                  AND i.action='SELL'
                  AND i.source='threshold_breach_exit'
                  AND i.status='REJECTED'
                  AND i.error IS NOT NULL
                ORDER BY i.id DESC LIMIT 1
            )
        WHERE threshold_exit_status='Blocked'
          AND EXISTS (
              SELECT 1 FROM broker_order_intents i
              WHERE i.mode=broker_positions.mode
                AND i.ticker=broker_positions.ticker
                  AND i.side=broker_positions.side
                  AND i.action='SELL'
                  AND i.source='threshold_breach_exit'
                  AND i.status='REJECTED'
                  AND i.error IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM broker_fills f
              WHERE f.mode=broker_positions.mode
                AND f.ticker=broker_positions.ticker
                AND f.side=broker_positions.side
                AND f.action='SELL'
                AND f.source='threshold_breach_exit'
          );
        """,
    ),
    (
        19,
        """
        -- Remove audit events generated by the former SETTLED -> FILLED
        -- reconciliation loop. They contain no unique execution evidence.
        DELETE FROM broker_audit_events
        WHERE event_type IN ('ORDER_STATE_CHANGED','INTENT_STATE_CHANGED')
          AND json_extract(detail_json, '$.from')='SETTLED'
          AND json_extract(detail_json, '$.to')='FILLED';

        -- These payloads duplicate normalized columns and are not read by the
        -- model, calibration, ledger, PWA, or historical trade review.
        UPDATE kalshi_snapshots
        SET orderbook_json='{}'
        WHERE orderbook_json IS NOT NULL AND orderbook_json<>'{}';

        UPDATE kalshi_trade_ticks
        SET raw_json='{}'
        WHERE raw_json<>'{}';
        """,
    ),
    (
        20,
        """
        CREATE TABLE texas_holdem_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            environment TEXT NOT NULL CHECK(environment IN ('PAPER','DEMO','LIVE')),
            ticker TEXT NOT NULL,
            market_open_time TEXT,
            threshold REAL,
            opening_btc_proxy REAL,
            side TEXT CHECK(side IN ('YES','NO')),
            status TEXT NOT NULL DEFAULT 'WAITING',
            entry_price_cap REAL NOT NULL,
            target_contracts REAL,
            filled_contracts REAL NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_quote_marker TEXT,
            fold_reason TEXT,
            flop_target REAL NOT NULL,
            turn_target REAL NOT NULL,
            river_target REAL NOT NULL,
            river_stop REAL NOT NULL,
            entry_price REAL,
            entry_fees REAL NOT NULL DEFAULT 0,
            exit_reason TEXT,
            exit_trigger_bid REAL,
            exited_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(environment,ticker)
        );
        CREATE INDEX idx_texas_holdem_rounds_environment_status
            ON texas_holdem_rounds(environment,status,updated_at);

        CREATE TABLE texas_holdem_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            attempt_number INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            quote_marker TEXT,
            side TEXT NOT NULL CHECK(side IN ('YES','NO')),
            executable_price REAL,
            requested_contracts REAL,
            filled_contracts REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            blocker TEXT,
            broker_client_order_id TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(round_id) REFERENCES texas_holdem_rounds(id) ON DELETE CASCADE,
            UNIQUE(round_id,attempt_number)
        );

        ALTER TABLE broker_positions ADD COLUMN strategy_metadata_json TEXT;
        ALTER TABLE broker_positions ADD COLUMN texas_exit_status TEXT;
        ALTER TABLE broker_positions ADD COLUMN texas_exit_reason TEXT;
        ALTER TABLE broker_positions ADD COLUMN texas_exit_last_attempt_at TEXT;
        ALTER TABLE broker_positions ADD COLUMN texas_exit_last_attempt_bid REAL;
        """,
    ),
    (
        21,
        """
        -- A one-round Texas pass is deliberately separate from arming and
        -- settings.  It is consumed only by its scheduled market.
        CREATE TABLE texas_holdem_passes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            environment TEXT NOT NULL CHECK(environment IN ('PAPER','DEMO','LIVE')),
            source_ticker TEXT NOT NULL,
            target_open_epoch REAL NOT NULL,
            target_open_time TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT,
            UNIQUE(environment, target_open_epoch)
        );
        CREATE INDEX idx_texas_holdem_passes_target
            ON texas_holdem_passes(environment, target_open_epoch);
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
