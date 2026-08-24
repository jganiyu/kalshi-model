from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from app.db import Database
from app.domain import (
    benchmark_error_summary,
    calibration_metrics,
    clamp,
    iso_now,
    parse_time,
)


FEATURE_NAMES = [
    "z_distance",
    "time_fraction",
    "volatility_5m",
    "volatility_15m",
    "momentum_1m",
    "momentum_5m",
    "high_low_5m_pct",
    "volume_acceleration",
    "dispersion_pct",
    "orderbook_imbalance",
    "market_probability",
    "settlement_window_fraction",
    "benchmark_uncertainty_pct",
]

MIN_CANDIDATE_OBSERVATIONS = 12
MIN_PROMOTION_OBSERVATIONS = 120
MIN_PROMOTION_DAYS = 7
MAX_TRAINING_OBSERVATIONS = 1_000
MIN_BRIER_IMPROVEMENT = 0.005
MAX_CALIBRATION_ERROR_REGRESSION = 0.01


def feature_vector(features: dict[str, Any]) -> list[float]:
    values = []
    for name in FEATURE_NAMES:
        value = features.get(name, 0.0)
        values.append(float(value) if value is not None else 0.0)
    return values


def sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -30, 30)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_logistic(
    x: np.ndarray, y: np.ndarray, regularization: float = 0.2, iterations: int = 1200
) -> dict[str, Any]:
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-8] = 1.0
    standardized = (x - mean) / scale
    design = np.column_stack([np.ones(len(x)), standardized])
    weights = np.zeros(design.shape[1])
    learning_rate = 0.08
    for step in range(iterations):
        predictions = sigmoid(design @ weights)
        gradient = design.T @ (predictions - y) / len(y)
        gradient[1:] += regularization * weights[1:] / len(y)
        weights -= (learning_rate / (1.0 + step / 800.0)) * gradient
    return {
        "feature_names": FEATURE_NAMES,
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "intercept": float(weights[0]),
        "coefficients": weights[1:].tolist(),
        "regularization": regularization,
    }


def predict_logistic(parameters: dict[str, Any], features: dict[str, Any]) -> float:
    vector = np.asarray(feature_vector(features), dtype=float)
    mean = np.asarray(parameters["mean"], dtype=float)
    scale = np.asarray(parameters["scale"], dtype=float)
    coefficients = np.asarray(parameters["coefficients"], dtype=float)
    score = float(parameters["intercept"]) + float(((vector - mean) / scale) @ coefficients)
    return clamp(float(sigmoid(np.asarray([score]))[0]), 0.01, 0.99)


class ModelManager:
    def __init__(self, db: Database):
        self.db = db

    def active(self) -> dict[str, Any]:
        row = self.db.fetch_one(
            "SELECT * FROM model_versions WHERE status='active' ORDER BY promoted_at DESC LIMIT 1"
        )
        if not row:
            return {"version": "baseline-1.1", "model_type": "settlement-average"}
        row["parameters"] = json.loads(row["parameters_json"])
        row["validation"] = json.loads(row["validation_json"])
        return row

    def predict(self, features: dict[str, Any], baseline_probability: float) -> tuple[float, str]:
        model = self.active()
        if (
            model.get("model_type") == "regularized-logistic"
            and model.get("parameters", {}).get("feature_names") == FEATURE_NAMES
        ):
            return predict_logistic(model["parameters"], features), str(model["version"])
        return baseline_probability, str(model.get("version", "baseline-1.1"))

    def benchmark_calibration(self, limit: int | None = None) -> dict[str, Any]:
        settings = self.db.settings()
        sample_limit = int(
            limit if limit is not None else settings.get("benchmark_history_samples", 120)
        )
        rows = self.db.fetch_all(
            """
            SELECT z.raw_json, m.close_time
            FROM settlements z
            JOIN markets m ON m.ticker = z.ticker
            WHERE m.close_time IS NOT NULL
            ORDER BY m.close_time DESC
            LIMIT ?
            """,
            (sample_limit,),
        )
        observations: list[tuple[float, float]] = []
        coverage: list[float] = []
        for row in rows:
            try:
                payload = json.loads(row["raw_json"])
                official = float(payload.get("expiration_value"))
                close = parse_time(row["close_time"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if official <= 0 or close is None:
                continue
            start = close - timedelta(seconds=60)
            proxy = self.db.fetch_one(
                """
                SELECT AVG(second_price) AS proxy_average,
                       COUNT(*) AS sample_seconds
                FROM (
                    SELECT substr(observed_at, 1, 19) AS observed_second,
                           AVG(composite_price) AS second_price
                    FROM btc_ticks
                    WHERE observed_at >= ? AND observed_at <= ?
                    GROUP BY observed_second
                )
                """,
                (start.isoformat(), close.isoformat()),
            )
            sample_seconds = int((proxy or {}).get("sample_seconds") or 0)
            proxy_average = (proxy or {}).get("proxy_average")
            if sample_seconds < 20 or proxy_average is None:
                continue
            observations.append((float(proxy_average), official))
            coverage.append(min(1.0, sample_seconds / 60.0))
        summary = benchmark_error_summary(
            observations,
            uncertainty_floor_pct=float(
                settings.get("benchmark_uncertainty_floor_pct", 0.00015)
            ),
            minimum_samples=int(
                settings.get("benchmark_calibration_min_samples", 20)
            ),
        )
        summary["average_window_coverage"] = (
            sum(coverage) / len(coverage) if coverage else None
        )
        return summary

    def observations(self) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT s.id, s.ticker, s.observed_at, s.model_probability,
                   s.market_probability, s.input_json, z.result
            FROM signal_snapshots s
            JOIN settlements z ON z.ticker = s.ticker
            JOIN (
                SELECT ticker, MAX(id) AS max_id
                FROM signal_snapshots
                GROUP BY ticker
            ) latest ON latest.max_id = s.id
            WHERE s.model_probability IS NOT NULL AND z.result IN (0, 1)
            ORDER BY s.observed_at ASC
            """
        )
        for row in rows:
            payload = json.loads(row["input_json"])
            row["features"] = payload.get("features", payload)
        return rows

    def evaluate_and_retrain(self, trigger: str) -> dict[str, Any]:
        observations = self.observations()
        settings = self.db.settings()
        current_metrics = calibration_metrics(
            (row["model_probability"], row["result"]) for row in observations
        )
        n = len(observations)
        history_days = int(settings.get("training_history_days", 3650))
        if observations:
            latest_observed = parse_time(str(observations[-1]["observed_at"]))
            if latest_observed:
                cutoff = latest_observed - timedelta(days=history_days)
                observations_in_window = [
                    row for row in observations
                    if (parse_time(str(row["observed_at"])) or latest_observed) >= cutoff
                ]
            else:
                observations_in_window = observations
        else:
            observations_in_window = observations
        maximum_observations = int(
            settings.get("training_max_samples", MAX_TRAINING_OBSERVATIONS)
        )
        training_observations = observations_in_window[-maximum_observations:]
        training_n = len(training_observations)
        observed_days = len(
            {str(row["observed_at"])[:10] for row in training_observations}
        )
        candidate_version = None
        candidate_metrics: dict[str, Any] | None = None
        promoted = False
        promotion_data_eligible = False
        parameters: dict[str, Any] | None = None
        validation_predictions: list[tuple[float, int]] = []

        minimum_candidate = int(settings.get("training_min_samples", MIN_CANDIDATE_OBSERVATIONS))
        minimum_promotion = int(settings.get("promotion_min_samples", MIN_PROMOTION_OBSERVATIONS))
        minimum_days = int(settings.get("promotion_min_days", MIN_PROMOTION_DAYS))
        minimum_brier = float(settings.get("minimum_brier_improvement", MIN_BRIER_IMPROVEMENT))
        calibration_tolerance = float(
            settings.get("calibration_tolerance", MAX_CALIBRATION_ERROR_REGRESSION)
        )
        if training_n >= minimum_candidate:
            x = np.asarray(
                [feature_vector(row["features"]) for row in training_observations],
                dtype=float,
            )
            y = np.asarray([row["result"] for row in training_observations], dtype=float)
            minimum_train = max(8, training_n // 3)
            for index in range(minimum_train, training_n):
                fold_parameters = fit_logistic(x[:index], y[:index], iterations=600)
                prediction = predict_logistic(
                    fold_parameters, training_observations[index]["features"]
                )
                validation_predictions.append((prediction, int(y[index])))
            candidate_metrics = calibration_metrics(validation_predictions)
            parameters = fit_logistic(x, y)
            candidate_version = (
                f"logistic-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"
            )
            incumbent_tail = calibration_metrics(
                (
                    training_observations[index]["model_probability"],
                    training_observations[index]["result"],
                )
                for index in range(minimum_train, training_n)
            )
            candidate_brier = candidate_metrics.get("brier_score")
            incumbent_brier = incumbent_tail.get("brier_score")
            candidate_error = candidate_metrics.get("calibration_error")
            incumbent_error = incumbent_tail.get("calibration_error")
            promotion_data_eligible = bool(
                training_n >= minimum_promotion
                and observed_days >= minimum_days
            )
            promoted = bool(
                promotion_data_eligible
                and candidate_brier is not None
                and incumbent_brier is not None
                and candidate_brier <= incumbent_brier - minimum_brier
                and candidate_error is not None
                and incumbent_error is not None
                and candidate_error
                <= incumbent_error + calibration_tolerance
            )
            self.db.execute(
                """
                INSERT INTO model_versions(
                    version, created_at, model_type, status, training_samples,
                    validation_json, parameters_json, promoted_at, parent_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_version,
                    iso_now(),
                    "regularized-logistic",
                    "active" if promoted else "candidate",
                    training_n,
                    json.dumps(
                        {
                            "candidate": candidate_metrics,
                            "incumbent_same_window": incumbent_tail,
                            "method": "expanding-window one-step-forward validation",
                            "training_window": {
                                "observations": training_n,
                                "distinct_utc_days": observed_days,
                                "maximum_observations": maximum_observations,
                                "maximum_days": history_days,
                            },
                            "promotion_requirements": {
                                "minimum_observations": minimum_promotion,
                                "minimum_distinct_utc_days": minimum_days,
                                "minimum_brier_improvement": minimum_brier,
                                "maximum_calibration_error_regression": (
                                    calibration_tolerance
                                ),
                            },
                        }
                    ),
                    json.dumps(parameters),
                    iso_now() if promoted else None,
                    self.active()["version"],
                ),
            )
            if promoted:
                self.db.execute(
                    "UPDATE model_versions SET status='retired' WHERE status='active' AND version != ?",
                    (candidate_version,),
                )

        active = self.active()
        if n == 0:
            tldr = "No settled live observations yet. The analytical cold-start model remains active."
        elif candidate_metrics is None:
            tldr = (
                f"Calibration updated with {n} settled contracts. More independent outcomes are "
                "needed before a candidate model can be validated."
            )
        elif not promotion_data_eligible:
            tldr = (
                f"Candidate validation updated with {training_n} recent contracts across "
                f"{observed_days} UTC days. Promotion requires at least "
                f"{minimum_promotion} contracts across {minimum_days} days."
            )
        elif promoted:
            tldr = (
                f"Calibration improved and {candidate_version} earned promotion on forward validation."
            )
        else:
            tldr = (
                "Calibration was updated, but the candidate did not clear the out-of-sample "
                "promotion margin. The incumbent remains active."
            )
        report = {
            "trigger": trigger,
            "sample_size": n,
            "training_sample_size": training_n,
            "training_distinct_utc_days": observed_days,
            "training_window_limit": maximum_observations,
            "training_history_days": history_days,
            "current": current_metrics,
            "candidate": candidate_metrics,
            "promoted": promoted,
            "promotion_data_eligible": promotion_data_eligible,
            "active_model": active["version"],
            "candidate_model": candidate_version,
            "feature_names": FEATURE_NAMES,
            "candidate_coefficients": parameters.get("coefficients") if parameters else None,
            "validation": (
                "Rolling-window expanding, one-step-forward; no future rows enter a "
                "training fold."
            ),
            "promotion_requirements": {
                "minimum_observations": minimum_promotion,
                "minimum_distinct_utc_days": minimum_days,
                "minimum_brier_improvement": minimum_brier,
                "maximum_calibration_error_regression": (
                    calibration_tolerance
                ),
            },
            "limitations": [
                "Historical bootstrap uses Coinbase spot as a documented proxy for CF Benchmarks BRTI.",
                "Kalshi historical order-book depth is not available from market candlesticks.",
                "Probability buckets with small samples should be treated as descriptive only.",
            ],
            "signal_snapshot_ids": [row["id"] for row in training_observations],
        }
        self.db.execute(
            """
            INSERT INTO calibration_reports(
                created_at, trigger, tldr, settled_contracts, active_model_version,
                candidate_model_version, promoted, brier_before, brier_after,
                calibration_error, report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iso_now(),
                trigger,
                tldr,
                n,
                active["version"],
                candidate_version,
                int(promoted),
                current_metrics.get("brier_score"),
                candidate_metrics.get("brier_score") if candidate_metrics else None,
                current_metrics.get("calibration_error"),
                json.dumps(report),
            ),
        )
        return {"tldr": tldr, **report}


def report_rows(db: Database) -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT * FROM calibration_reports ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        row["report"] = json.loads(row.pop("report_json"))
    return rows
