from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import numpy as np

from app.db import Database
from app.domain import calibration_metrics, clamp, iso_now


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
]


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
            return {"version": "baseline-1.0", "model_type": "distance-volatility"}
        row["parameters"] = json.loads(row["parameters_json"])
        row["validation"] = json.loads(row["validation_json"])
        return row

    def predict(self, features: dict[str, Any], baseline_probability: float) -> tuple[float, str]:
        model = self.active()
        if model.get("model_type") == "regularized-logistic":
            return predict_logistic(model["parameters"], features), str(model["version"])
        return baseline_probability, str(model.get("version", "baseline-1.0"))

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
        current_metrics = calibration_metrics(
            (row["model_probability"], row["result"]) for row in observations
        )
        n = len(observations)
        candidate_version = None
        candidate_metrics: dict[str, Any] | None = None
        promoted = False
        parameters: dict[str, Any] | None = None
        validation_predictions: list[tuple[float, int]] = []

        if n >= 12:
            x = np.asarray([feature_vector(row["features"]) for row in observations], dtype=float)
            y = np.asarray([row["result"] for row in observations], dtype=float)
            minimum_train = max(8, n // 3)
            for index in range(minimum_train, n):
                fold_parameters = fit_logistic(x[:index], y[:index], iterations=600)
                prediction = predict_logistic(fold_parameters, observations[index]["features"])
                validation_predictions.append((prediction, int(y[index])))
            candidate_metrics = calibration_metrics(validation_predictions)
            parameters = fit_logistic(x, y)
            candidate_version = f"logistic-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
            incumbent_tail = calibration_metrics(
                (observations[index]["model_probability"], observations[index]["result"])
                for index in range(minimum_train, n)
            )
            candidate_brier = candidate_metrics.get("brier_score")
            incumbent_brier = incumbent_tail.get("brier_score")
            candidate_error = candidate_metrics.get("calibration_error")
            incumbent_error = incumbent_tail.get("calibration_error")
            promoted = bool(
                n >= 40
                and candidate_brier is not None
                and incumbent_brier is not None
                and candidate_brier <= incumbent_brier - 0.005
                and candidate_error is not None
                and incumbent_error is not None
                and candidate_error <= incumbent_error + 0.01
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
                    n,
                    json.dumps(
                        {
                            "candidate": candidate_metrics,
                            "incumbent_same_window": incumbent_tail,
                            "method": "expanding-window one-step-forward validation",
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
            "current": current_metrics,
            "candidate": candidate_metrics,
            "promoted": promoted,
            "active_model": active["version"],
            "candidate_model": candidate_version,
            "feature_names": FEATURE_NAMES,
            "candidate_coefficients": parameters.get("coefficients") if parameters else None,
            "validation": "Expanding-window, one-step-forward; no future rows enter a training fold.",
            "limitations": [
                "Historical bootstrap uses Coinbase spot as a documented proxy for CF Benchmarks BRTI.",
                "Kalshi historical order-book depth is not available from market candlesticks.",
                "Probability buckets with small samples should be treated as descriptive only.",
            ],
            "signal_snapshot_ids": [row["id"] for row in observations],
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
