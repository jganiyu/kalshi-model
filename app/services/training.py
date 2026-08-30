from __future__ import annotations

import json
import math
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


LEGACY_FEATURE_NAMES = [
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

# Retained as the public legacy schema name. Existing promoted models keep using
# the feature_names stored in their own parameters.
FEATURE_NAMES = LEGACY_FEATURE_NAMES

VOLUME_FEATURE_GROUPS = {
    "btc_relative_volume": ["btc_rvol_1m", "btc_rvol_5m"],
    "signed_order_flow": [
        "btc_flow_imbalance_1m", "btc_flow_imbalance_5m",
        "btc_cvd_slope_1m", "btc_cvd_slope_5m",
    ],
    "volume_confirmed_momentum": [
        "btc_volume_confirmation_1m", "btc_volume_confirmation_5m",
    ],
    "vwap_position": [
        "btc_vwap_distance_1m", "btc_vwap_distance_5m",
        "btc_vwap_z_1m", "btc_vwap_z_5m",
    ],
    "kalshi_flow_turnover": [
        "kalshi_flow_imbalance_1m", "kalshi_turnover_5m",
        "kalshi_turnover_change", "btc_kalshi_flow_agreement",
    ],
    "context_interactions": [
        "volume_time_interaction", "volume_margin_interaction",
        "volume_volatility_interaction", "volume_settlement_interaction",
    ],
    "missingness": ["btc_volume_missing", "kalshi_volume_missing"],
}
VOLUME_FEATURE_NAMES = [
    *LEGACY_FEATURE_NAMES,
    *[name for group in VOLUME_FEATURE_GROUPS.values() for name in group],
]

MIN_CANDIDATE_OBSERVATIONS = 12
MIN_PROMOTION_OBSERVATIONS = 120
MIN_PROMOTION_DAYS = 7
MAX_TRAINING_OBSERVATIONS = 1_000
MIN_BRIER_IMPROVEMENT = 0.005
MAX_CALIBRATION_ERROR_REGRESSION = 0.01


def feature_vector(
    features: dict[str, Any], feature_names: list[str] | None = None
) -> list[float]:
    values = []
    for name in feature_names or FEATURE_NAMES:
        value = features.get(name, 0.0)
        values.append(float(value) if value is not None else 0.0)
    return values


def sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -30, 30)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_logistic(
    x: np.ndarray, y: np.ndarray, regularization: float = 0.2, iterations: int = 1200,
    feature_names: list[str] | None = None,
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
        "feature_names": feature_names or FEATURE_NAMES,
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "intercept": float(weights[0]),
        "coefficients": weights[1:].tolist(),
        "regularization": regularization,
    }


def predict_logistic(parameters: dict[str, Any], features: dict[str, Any]) -> float:
    names = list(parameters.get("feature_names") or FEATURE_NAMES)
    vector = np.asarray(feature_vector(features, names), dtype=float)
    mean = np.asarray(parameters["mean"], dtype=float)
    scale = np.asarray(parameters["scale"], dtype=float)
    coefficients = np.asarray(parameters["coefficients"], dtype=float)
    score = float(parameters["intercept"]) + float(((vector - mean) / scale) @ coefficients)
    return clamp(float(sigmoid(np.asarray([score]))[0]), 0.01, 0.99)


def logarithmic_loss(predictions: list[tuple[float, int]]) -> float | None:
    if not predictions:
        return None
    return -sum(
        result * math.log(clamp(probability, 1e-9, 1 - 1e-9))
        + (1 - result) * math.log(clamp(1 - probability, 1e-9, 1 - 1e-9))
        for probability, result in predictions
    ) / len(predictions)


def walk_forward_evaluation(
    observations: list[dict[str, Any]], feature_names: list[str]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if len(observations) < 12:
        return None, None
    x = np.asarray(
        [feature_vector(row["features"], feature_names) for row in observations],
        dtype=float,
    )
    y = np.asarray([row["result"] for row in observations], dtype=float)
    minimum_train = max(8, len(observations) // 3)
    predictions: list[tuple[float, int]] = []
    for index in range(minimum_train, len(observations)):
        fold = fit_logistic(
            x[:index], y[:index], iterations=600, feature_names=feature_names
        )
        predictions.append(
            (predict_logistic(fold, observations[index]["features"]), int(y[index]))
        )
    metrics = calibration_metrics(predictions)
    metrics["log_loss"] = logarithmic_loss(predictions)
    metrics["validation_samples"] = len(predictions)
    if predictions:
        errors = [(probability - result) ** 2 for probability, result in predictions]
        if len(errors) > 1:
            metrics["brier_standard_error"] = float(np.std(errors, ddof=1) / math.sqrt(len(errors)))
    scored = observations[minimum_train:]

    def sliced(labels: list[tuple[str, Any]]) -> dict[str, Any]:
        return {
            label: calibration_metrics(
                predictions[index]
                for index, row in enumerate(scored)
                if predicate(row)
            )
            for label, predicate in labels
        }

    metrics["by_time_remaining"] = sliced(
        [
            ("10-15m", lambda row: float(row["features"].get("time_fraction", 0)) > 2 / 3),
            ("5-10m", lambda row: 1 / 3 < float(row["features"].get("time_fraction", 0)) <= 2 / 3),
            ("0-5m", lambda row: float(row["features"].get("time_fraction", 0)) <= 1 / 3),
        ]
    )
    metrics["by_threshold_margin"] = sliced(
        [
            ("under-$25", lambda row: abs(float(row["features"].get("threshold_margin_dollars", 0))) < 25),
            ("$25-$50", lambda row: 25 <= abs(float(row["features"].get("threshold_margin_dollars", 0))) < 50),
            ("$50+", lambda row: abs(float(row["features"].get("threshold_margin_dollars", 0))) >= 50),
        ]
    )
    return metrics, fit_logistic(x, y, feature_names=feature_names)


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
        if model.get("model_type") == "regularized-logistic":
            parameters = model.get("parameters", {})
            names = parameters.get("feature_names")
            coefficients = parameters.get("coefficients")
            if (
                isinstance(names, list) and isinstance(coefficients, list)
                and len(names) == len(coefficients)
            ):
                return predict_logistic(parameters, features), str(model["version"])
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
        volume_shadow: dict[str, Any] = {
            "status": "collecting",
            "sample_size": 0,
            "feature_schema": "volume-signals-1",
            "promoted": False,
        }

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

        volume_observations = [
            row for row in training_observations
            if "btc_volume_missing" in row["features"]
            and float(row["features"].get("btc_volume_missing", 1.0)) < 0.5
        ]
        volume_shadow["sample_size"] = len(volume_observations)
        if len(volume_observations) >= minimum_candidate:
            combined_metrics, shadow_parameters = walk_forward_evaluation(
                volume_observations, VOLUME_FEATURE_NAMES
            )
            if combined_metrics and shadow_parameters:
                shadow_parameters["feature_schema_version"] = "volume-signals-1"
                ablations: dict[str, Any] = {}
                base_metrics, _ = walk_forward_evaluation(
                    volume_observations, LEGACY_FEATURE_NAMES
                )
                ablations["existing_model_inputs"] = base_metrics
                for group, names in VOLUME_FEATURE_GROUPS.items():
                    metrics, _ = walk_forward_evaluation(
                        volume_observations, [*LEGACY_FEATURE_NAMES, *names]
                    )
                    ablations[group] = metrics
                feature_completeness = {
                    name: sum(
                        1 for row in volume_observations
                        if row["features"].get(name) is not None
                        and not (
                            (name.startswith("kalshi_") or name.startswith("btc_kalshi_"))
                            and float(row["features"].get("kalshi_volume_missing", 1)) >= .5
                        )
                        and not (
                            name.startswith("btc_")
                            and not name.startswith("btc_kalshi_")
                            and float(row["features"].get("btc_volume_missing", 1)) >= .5
                        )
                    ) / len(volume_observations)
                    for name in VOLUME_FEATURE_NAMES
                    if name not in LEGACY_FEATURE_NAMES
                }
                minimum_train = max(8, len(volume_observations) // 3)
                incumbent_same_window = calibration_metrics(
                    (row["model_probability"], row["result"])
                    for row in volume_observations[minimum_train:]
                )
                shadow_version = (
                    f"volume-shadow-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"
                )
                validation = {
                    "candidate": combined_metrics,
                    "incumbent_same_window": incumbent_same_window,
                    "ablations": ablations,
                    "feature_completeness": feature_completeness,
                    "method": (
                        "Contract-level, time-ordered expanding-window validation; "
                        "normalization and training use earlier contracts only."
                    ),
                    "promotion": (
                        "Shadow only. Review is required before this feature schema can "
                        "become active."
                    ),
                }
                self.db.execute(
                    """
                    INSERT INTO model_versions(
                        version,created_at,model_type,status,training_samples,
                        validation_json,parameters_json,promoted_at,parent_version
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        shadow_version, iso_now(), "regularized-logistic", "shadow",
                        len(volume_observations), json.dumps(validation),
                        json.dumps(shadow_parameters), None, self.active()["version"],
                    ),
                )
                volume_shadow = {
                    "status": "validated-shadow",
                    "version": shadow_version,
                    "sample_size": len(volume_observations),
                    "feature_schema": "volume-signals-1",
                    "candidate": combined_metrics,
                    "incumbent_same_window": incumbent_same_window,
                    "ablations": ablations,
                    "feature_completeness": feature_completeness,
                    "candidate_coefficients": dict(
                        zip(VOLUME_FEATURE_NAMES, shadow_parameters["coefficients"])
                    ),
                    "promoted": False,
                    "review_required": True,
                }

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
            "volume_shadow": volume_shadow,
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
                "Volume features remain shadow-only until an explicit review approves promotion.",
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
