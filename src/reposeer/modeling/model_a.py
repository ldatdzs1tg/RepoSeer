"""Portable Model A inference. Fitting lives in the offline training command."""

import json
import math
from pathlib import Path

from reposeer.contracts.models import PackageSnapshot
from reposeer.contracts.validation import validate_snapshot_coverage
from reposeer.features.builder import FEATURE_COLUMNS


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    value = math.exp(value)
    return value / (1 + value)


class ModelA:
    def __init__(self, artifact: dict):
        if artifact.get("format") != "reposeer.model-a.logistic.v1":
            raise ValueError("unsupported Model A artifact")
        if artifact["features"] != list(FEATURE_COLUMNS):
            raise ValueError("model feature order differs from the CORE schema")
        indicators = artifact["missing_indicator_columns"]
        calibration = artifact["calibration"]
        if (
            any(
                type(index) is not int or not 0 <= index < len(FEATURE_COLUMNS)
                for index in indicators
            )
            or len(set(indicators)) != len(indicators)
            or type(artifact["release_eligible"]) is not bool
            or calibration["status"] not in {"fitted", "insufficient_validation_sample"}
            or not all(math.isfinite(calibration[key]) for key in ("slope", "intercept"))
            or (artifact["release_eligible"] and calibration["status"] != "fitted")
        ):
            raise ValueError("invalid Model A indicators, calibration or release status")
        dimension = len(FEATURE_COLUMNS) + len(indicators)
        if (
            len(artifact["impute"]) != len(FEATURE_COLUMNS)
            or any(len(artifact[key]) != dimension for key in ("mean", "scale", "weights"))
            or any(value <= 0 for value in artifact["scale"])
            or any(
                not math.isfinite(value)
                for key in ("impute", "mean", "scale", "weights")
                for value in artifact[key]
            )
            or not math.isfinite(artifact["intercept"])
        ):
            raise ValueError("invalid model preprocessing dimensions or numeric values")
        self.artifact = artifact

    @classmethod
    def load(cls, path: str | Path):
        return cls(json.loads(Path(path).read_bytes()))

    def margin(self, features: dict) -> float:
        unknown = set(features) - set(FEATURE_COLUMNS)
        if unknown:
            raise ValueError(f"unknown model features: {sorted(unknown)}")
        values, missing = [], []
        for index, name in enumerate(FEATURE_COLUMNS):
            value = features.get(name)
            missing.append(value is None)
            if value is None:
                values.append(self.artifact["impute"][index])
            elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid CORE value for {name}")
            else:
                values.append(math.log1p(value))
        values.extend(float(missing[index]) for index in self.artifact["missing_indicator_columns"])
        return self.artifact["intercept"] + sum(
            weight * ((value - mean) / scale)
            for weight, value, mean, scale in zip(
                self.artifact["weights"],
                values,
                self.artifact["mean"],
                self.artifact["scale"],
                strict=True,
            )
        )

    def predict(self, features: dict, *, experimental: bool = False) -> dict:
        if set(features) - set(FEATURE_COLUMNS):
            raise ValueError("Model A accepts only CORE feature values")
        observed = sum(features.get(name) is not None for name in FEATURE_COLUMNS)
        context = {
            "model_version": self.artifact["model_version"],
            "feature_set": "E0",
            "purpose": self.artifact["purpose"],
            "calibration_status": self.artifact["calibration"]["status"],
            "coverage": {"usable_features": observed, "total_features": len(FEATURE_COLUMNS)},
        }
        if not observed:
            return {
                **context,
                "result_status": "insufficient_evidence",
                "reason": "no_usable_core_features",
            }
        if not self.artifact["release_eligible"] and not experimental:
            return {
                **context,
                "result_status": "insufficient_evidence",
                "reason": "model_release_gates_not_met",
            }
        margin = self.margin(features)
        calibration = self.artifact["calibration"]
        probability = (
            sigmoid(calibration["slope"] * margin + calibration["intercept"])
            if calibration["status"] == "fitted"
            else sigmoid(margin)
        )
        return {
            **context,
            "result_status": "predicted"
            if self.artifact["release_eligible"]
            else "experimental_score",
            "probability": probability,
            "experimental": not self.artifact["release_eligible"],
        }

    def predict_snapshot(self, snapshot: PackageSnapshot, *, experimental: bool = False) -> dict:
        """Run the frozen estimator on a validated feature snapshot from the requested cutoff."""
        snapshot = PackageSnapshot.model_validate(snapshot.model_dump())
        validate_snapshot_coverage(snapshot)
        if snapshot.schema_version != self.artifact.get("feature_schema_version", "0.1.0"):
            raise ValueError("snapshot schema differs from the fitted Model A schema")
        return {
            "package_id": snapshot.package_id,
            "repo_group_id": snapshot.repo_group_id,
            "cutoff_at": snapshot.cutoff_at,
            "horizon_end_at": snapshot.horizon_end_at,
            **self.predict(snapshot.features.model_dump(), experimental=experimental),
        }
