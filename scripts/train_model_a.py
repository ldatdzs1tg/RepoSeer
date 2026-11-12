"""Fit a retrospective CORE Model A after source, label and chronological gates pass."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

import polars as pl
from scripts.audit_model_a_splits import SPLITS, materialize_splits
from scripts.run_d09_drive import digest, file_digest

from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.features.builder import FEATURE_COLUMNS, FeatureBuilder
from reposeer.modeling.model_a import ModelA

MIN_ROWS_PER_CLASS = 20
MIN_REPO_GROUPS = 20
DEFAULT_WINDOWS = {
    "validation_start_at": "2022-01-01T00:00:00.000Z",
    "test_start_at": "2024-01-01T00:00:00.000Z",
    "test_end_at": "2025-01-01T00:00:00.000Z",
}


def minimum_sample_deficits(audit: dict) -> dict[str, dict[str, int]]:
    deficits = {}
    for split in SPLITS:
        held_out = audit["splits"][split]["repo_group_disjoint"]
        deficits[split] = {
            "negative": max(0, MIN_ROWS_PER_CLASS - held_out["labels"]["0"]),
            "positive": max(0, MIN_ROWS_PER_CLASS - held_out["labels"]["1"]),
            "repo_groups": max(0, MIN_REPO_GROUPS - held_out["repo_groups"]),
        }
    return deficits


def metrics(y, probabilities):
    import numpy as np
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        log_loss,
        precision_recall_fscore_support,
        roc_auc_score,
    )

    values = np.asarray(probabilities)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y, values >= 0.5, average="binary", zero_division=0
    )
    bins, ece = [], 0.0
    for lower, upper in zip(np.linspace(0, 1, 11)[:-1], np.linspace(0, 1, 11)[1:], strict=True):
        selected = (values >= lower) & ((values < upper) if upper < 1 else values <= upper)
        if not selected.any():
            continue
        actual, predicted = float(np.asarray(y)[selected].mean()), float(values[selected].mean())
        count = int(selected.sum())
        ece += count / len(y) * abs(actual - predicted)
        bins.append(
            {
                "lower": float(lower),
                "upper": float(upper),
                "rows": count,
                "predicted": predicted,
                "observed": actual,
            }
        )
    return {
        "rows": len(y),
        "positive_rows": int(sum(y)),
        "pr_auc_average_precision": float(average_precision_score(y, values)),
        "roc_auc": float(roc_auc_score(y, values)),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "brier": float(brier_score_loss(y, values)),
        "log_loss": float(log_loss(y, values, labels=[0, 1])),
        "ece_10_bins": ece,
        "calibration_bins": bins,
    }


def load_prepared(data_dir: Path) -> tuple[dict, list[dict], list[dict], dict[str, str]]:
    summary_path = data_dir / "summary.json"
    summary_body = summary_path.read_bytes()
    summary = json.loads(summary_body)
    if not summary.get("reviewed_directories"):
        raise ValueError("no prepared D09 directories were supplied")
    hashes = summary["artifact_hashes"]
    for path, expected in hashes.items():
        if file_digest(Path(path)) != expected:
            raise ValueError("a prepared feature or label artifact changed")
    reviews_path = data_dir / "reviews.jsonl"
    if file_digest(reviews_path) != summary["reviews_sha256"]:
        raise ValueError("prepared review records differ from the build summary")
    manifest_path = data_dir / "source-manifest.json"
    manifest = manifest_path.read_bytes()
    if digest(manifest) != summary.get("source_manifest_sha256"):
        raise ValueError("prepared source manifest is missing or differs from the build summary")
    sources = json.loads(manifest)
    for path, expected in sources.items():
        if file_digest(Path(path)) != expected:
            raise ValueError("a prepared source changed")
    universe, rows = [], []
    for value in summary["reviewed_directories"]:
        directory = Path(value)
        eligibility = directory / "eligibility.json"
        if str(eligibility.resolve()) not in hashes:
            raise ValueError("eligibility is missing from the checked artifact manifest")
        universe.extend(json.loads(eligibility.read_bytes()))
        strict = directory / "strict_e0_rows.parquet"
        if strict.is_file():
            if str(strict.resolve()) not in hashes:
                raise ValueError("strict rows are missing from the checked artifact manifest")
            rows.extend(pl.read_parquet(strict).iter_rows(named=True))
    if len(rows) != summary["strict_rows"]:
        raise ValueError("strict row count differs from the build summary")
    checked_files = {
        **sources,
        **hashes,
        str(summary_path.resolve()): digest(summary_body),
        str(reviews_path.resolve()): summary["reviews_sha256"],
        str(manifest_path.resolve()): summary["source_manifest_sha256"],
    }
    return summary, universe, rows, checked_files


def run(args, *, model_version: str = "0.1.0"):
    summary, universe, rows, checked_files = load_prepared(args.data_dir)
    summary_path = args.data_dir / "summary.json"
    source_manifest_sha = summary["source_manifest_sha256"]
    now = format_utc_timestamp(datetime.now(UTC))
    plan = {
        "purpose": "retrospective_analysis",
        "fold_fit_at": now,
        "selection_freeze_at": now,
        "evaluation_as_of": now,
        "validation_start_at": args.validation_start_at,
        "test_start_at": args.test_start_at,
        "test_end_at": args.test_end_at,
    }
    sets, audit = materialize_splits(universe, rows, **plan)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    split_path = args.output_dir / "split-audit.json"
    write_json_atomic(split_path, audit)
    failures = [
        split for split in SPLITS if not audit["splits"][split]["repo_group_disjoint"]["both_classes"]
    ]
    deficits = minimum_sample_deficits(audit)
    sample_minimum_met = not any(value for split in deficits.values() for value in split.values())
    minimum_required = getattr(args, "require_minimum_sample", False)
    if failures or (minimum_required and not sample_minimum_met):
        result = {
            "status": "blocked_missing_classes" if failures else "blocked_minimum_sample",
            "purpose": "retrospective_analysis",
            "splits_missing_a_class": failures,
            "counts": {
                split: audit["splits"][split]["repo_group_disjoint"]["labels"] for split in SPLITS
            },
            "minimum_sample_deficits": deficits,
            "reason": (
                "Every repository-disjoint split requires both verified classes after temporal guards."
                if failures
                else f"Every split requires {MIN_ROWS_PER_CLASS} rows per class and {MIN_REPO_GROUPS} repositories."
            ),
        }
        write_json_atomic(args.output_dir / "training-readiness.json", result)
        return result
    import numpy as np
    import sklearn
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    def matrix(data):
        return np.log1p(
            np.array(
                [
                    [row[name] if row[name] is not None else np.nan for name in FEATURE_COLUMNS]
                    for row in data
                ],
                dtype=float,
            )
        )

    pipeline = make_pipeline(
        SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
        StandardScaler(),
        LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000, random_state=42),
    )
    train, validation = sets["train"], sets["validation"]
    group_sizes = Counter(row["repo_group_id"] for row in train)
    weights = np.array([1 / group_sizes[row["repo_group_id"]] for row in train])
    weights *= len(train) / weights.sum()
    pipeline.fit(
        matrix(train), [row["label"] for row in train], logisticregression__sample_weight=weights
    )
    calibration = {"status": "insufficient_validation_sample", "slope": 1.0, "intercept": 0.0}
    validation_counts = Counter(row["label"] for row in validation)
    if (
        min(validation_counts.values()) >= 10
        and len({row["repo_group_id"] for row in validation}) >= 10
    ):
        calibrator = LogisticRegression(C=1.0, max_iter=1000)
        calibrator.fit(
            pipeline.decision_function(matrix(validation)).reshape(-1, 1),
            [row["label"] for row in validation],
        )
        calibration = {
            "status": "fitted",
            "slope": float(calibrator.coef_[0, 0]),
            "intercept": float(calibrator.intercept_[0]),
        }
    imputer, scaler, classifier = pipeline.steps[0][1], pipeline.steps[1][1], pipeline.steps[2][1]
    repo_groups = {split: {row["repo_group_id"] for row in data} for split, data in sets.items()}
    group_counts = {split: len(groups) for split, groups in repo_groups.items()}
    shared_groups = {
        f"{first}/{second}": len(repo_groups[first] & repo_groups[second])
        for first, second in combinations(SPLITS, 2)
    }
    # These predeclared sample gates block release of a tiny proof-of-concept model.
    sample_release_gates = calibration["status"] == "fitted" and sample_minimum_met
    release_eligible = False
    artifact = {
        "format": "reposeer.model-a.logistic.v1",
        "model_version": model_version,
        "feature_set": "E0",
        "feature_schema_version": "0.1.0",
        "features": list(FEATURE_COLUMNS),
        "impute": imputer.statistics_.tolist(),
        "missing_indicator_columns": imputer.indicator_.features_.tolist(),
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "weights": classifier.coef_[0].tolist(),
        "intercept": float(classifier.intercept_[0]),
        "calibration": calibration,
        "release_eligible": release_eligible,
        "release_gates": {
            "sample_and_group_checks": sample_release_gates,
            "prospective_evaluation_completed": False,
            "population_calibration_established": False,
        },
        "purpose": "retrospective_analysis",
        "fitted_at": now,
        "frozen_at": format_utc_timestamp(datetime.now(UTC)),
        "sklearn_version": sklearn.__version__,
        "prepared_summary_sha256": checked_files[str(summary_path.resolve())],
        "source_manifest_sha256": source_manifest_sha,
        "split_audit_sha256": file_digest(split_path),
        "training_population": "strict independently reviewed subset of the frozen Drive cohort; conditional retrospective evaluation",
    }
    artifact_path = args.output_dir / "model-a.json"
    write_json_atomic(artifact_path, artifact)
    frozen_model = ModelA.load(artifact_path)
    reports = {}
    for split, data in sets.items():
        feature_rows = [{name: row[name] for name in FEATURE_COLUMNS} for row in data]
        if not np.allclose(
            [frozen_model.margin(row) for row in feature_rows],
            pipeline.decision_function(matrix(data)),
            rtol=1e-9,
            atol=1e-9,
        ):
            raise ValueError("frozen model differs from the fitted estimator")
        values = [
            frozen_model.predict(row, experimental=True)["probability"] for row in feature_rows
        ]
        reports[split] = metrics([row["label"] for row in data], values)
        reports[split]["observed_rows_by_feature"] = {
            name: sum(row[name] is not None for row in data) for name in FEATURE_COLUMNS
        }
    sample = FeatureBuilder().polars_to_snapshots(pl.DataFrame(sets["test"][:1]))[0]
    write_json_atomic(args.output_dir / "sample-snapshot.json", sample.model_dump(mode="json"))
    write_json_atomic(
        args.output_dir / "sample-inference.json",
        {
            "default": frozen_model.predict_snapshot(sample),
            "experimental": frozen_model.predict_snapshot(sample, experimental=True),
        },
    )
    result = {
        "status": "trained",
        "model_version": model_version,
        "release_eligible": release_eligible,
        "purpose": "retrospective_analysis",
        "prospective_accuracy_claim_supported": False,
        "source_manifest_sha256": source_manifest_sha,
        "model_sha256": file_digest(artifact_path),
        "metrics": reports,
        "repo_groups": group_counts,
        "shared_repo_groups": shared_groups,
        "unseen_repo_audit_status": audit["status"],
        "coverage": {
            "cohort_package_cutoffs": len(universe),
            "complete_release_snapshots": summary["complete_release_censuses"],
            "strict_rows": len(rows),
        },
        "calibration": calibration,
        "limitation": "Metrics apply to the reviewed cohort. Ecosystem-wide calibration and prospective accuracy are not established.",
    }
    write_json_atomic(args.output_dir / "metrics.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/d09/model-a-retrospective-v1"))
    parser.add_argument(
        "--output-dir", type=Path, help="New output folder (defaults to a timestamped run)"
    )
    parser.add_argument("--model-version", default="0.1.0", help="Version recorded in the frozen model")
    parser.add_argument(
        "--require-minimum-sample",
        action="store_true",
        help=f"Require {MIN_ROWS_PER_CLASS} verified rows per class and {MIN_REPO_GROUPS} repos per split",
    )
    for key, value in DEFAULT_WINDOWS.items():
        parser.add_argument("--" + key.replace("_", "-"), default=value)
    args = parser.parse_args()
    model_version = args.model_version.strip()
    if not model_version:
        parser.error("expected a non-empty model version")
    if args.output_dir is None:
        args.output_dir = Path("data/d09/model-a-runs") / datetime.now(UTC).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
    try:
        result = run(args, model_version=model_version)
    except (OSError, ValueError, KeyError, ImportError, pl.exceptions.PolarsError) as error:
        parser.exit(1, f"Model A training stopped: {error}\n")
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), **result}))
    return 0 if result["status"] == "trained" else 2


if __name__ == "__main__":
    raise SystemExit(main())
