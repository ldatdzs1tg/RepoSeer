"""Exercise the installed Analyze route and its score eligibility gates."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from reposeer.analysis.core import RELEASE_GATES, CoreFeaturePackage, analyze_core
from reposeer.cli.app import app
from reposeer.contracts.analysis import validate_final_report
from reposeer.features.builder import FEATURE_COLUMNS

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/analysis"
CUTOFF = "2024-03-31T23:59:59.999Z"
HORIZON = "2025-03-31T23:59:59.999Z"


@pytest.fixture
def feature_package():
    example = json.loads((EXAMPLES / "final_report_core.json").read_bytes())
    source = example["sources"][0]
    source.update(retrieved_at=CUTOFF, valid_at=CUTOFF, available_at=CUTOFF)
    mapping = example["mapping"]
    mapping.update(valid_from=CUTOFF, available_at=CUTOFF)
    features = dict.fromkeys(FEATURE_COLUMNS)
    features["releases_total"] = 1
    return CoreFeaturePackage.model_validate(
        {
            "snapshot": {
                "schema_version": "0.1.0",
                "record_type": "package_snapshot",
                "package_id": example["package_id"],
                "repo_id": mapping["repo_id"],
                "repo_group_id": mapping["repo_group_id"],
                "cutoff_at": CUTOFF,
                "horizon_months": 12,
                "horizon_end_at": HORIZON,
                "features": features,
                "coverage": [
                    {
                        "data_group": "releases",
                        "status": "complete",
                        "records_observed": 1,
                        "time_range": {"start_at": "2023-01-01T00:00:00.000Z", "end_at": CUTOFF},
                        "missing_reason": None,
                    }
                ],
                "missing_fields": [
                    {"field_path": f"features.{name}", "reason": "not_collected", "detail": None}
                    for name, value in features.items()
                    if value is None
                ],
                "raw_record_ids": source["raw_record_ids"],
            },
            "mapping": mapping,
            "sources": [source],
            "core_coverage_sources": {"releases": [source["source_id"]]},
        }
    )


@pytest.fixture
def artifact(tmp_path):
    def write(*, released=True, **overrides):
        zeros = [0.0] * len(FEATURE_COLUMNS)
        path = tmp_path / "model-a.json"
        path.write_text(
            json.dumps(
                {
                    "format": "reposeer.model-a.logistic.v1",
                    "model_version": "synthetic-a",
                    "feature_schema_version": "0.1.0",
                    "feature_set": "E0",
                    "purpose": "retrospective_analysis",
                    "features": list(FEATURE_COLUMNS),
                    "impute": zeros,
                    "mean": zeros,
                    "scale": [1.0] * len(FEATURE_COLUMNS),
                    "weights": zeros,
                    "intercept": 0.0,
                    "missing_indicator_columns": [],
                    "calibration": {
                        "status": "fitted" if released else "insufficient_validation_sample",
                        "slope": 1.0,
                        "intercept": 0.0,
                    },
                    "release_eligible": released,
                    "release_gates": dict.fromkeys(RELEASE_GATES, released),
                    "frozen_at": HORIZON,
                    "training_population": "Synthetic test input.",
                    "analysis_policy": {
                        "preprocessing_version": "test-1",
                        "calibration_version": "test-1",
                        "calibration_population": "Synthetic test input.",
                        "trained_through_at": CUTOFF,
                        "risk_threshold_version": "test-1",
                        "moderate_at": 0.3,
                        "high_at": 0.7,
                        "applicability_version": "test-1",
                        "feature_ranges": {"releases_total": {"minimum": 1, "maximum": 1}},
                    },
                    **overrides,
                }
            ),
            encoding="utf-8",
        )
        return path

    return write


def test_analyze_cli_reports_no_score_without_a_released_model(feature_package, artifact, tmp_path):
    model = artifact(released=False, analysis_policy=None)
    original = model.read_bytes()
    path = tmp_path / "package.json"
    path.write_text(feature_package.model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["analyze", "--feature-package", str(path), "--model", str(model)]
    )
    assert result.exit_code == 0, result.output
    report = validate_final_report(result.stdout)
    assert report.result_status == "insufficient_evidence"
    assert report.reason_code == "model_release_gates_not_met"
    assert report.mapping == feature_package.mapping
    assert report.coverage[0].historical_validity == "verified"
    assert report.coverage[1].records_observed is None
    assert {"prediction", "core_prediction", "probability"}.isdisjoint(report.model_dump())
    assert model.read_bytes() == original


def test_frozen_prediction_uses_core_fallback_and_binds_model_and_sources(
    feature_package, artifact
):
    model = artifact()
    report = analyze_core(feature_package, model, mode_requested="ENHANCED")
    assert report.result_status == "predicted"
    assert (report.mode_used, report.enhanced_status, report.fallback_reason) == (
        "CORE",
        "fallback",
        "model_b_unavailable",
    )
    assert report.core_prediction is None
    assert report.prediction.probability == 0.5
    assert report.prediction.risk_level == "moderate"
    assert report.prediction.model.artifact_sha256 == sha256(model.read_bytes()).hexdigest()
    assert report.prediction.feature_source_ids == ["source:core"]


def test_late_coverage_cannot_score(feature_package, artifact):
    source = feature_package.sources[0].model_copy(
        update={
            "source_id": "source:late",
            "available_at": HORIZON,
            "valid_at": HORIZON,
            "retrieved_at": HORIZON,
        }
    )
    feature_package.sources.append(source)
    feature_package.core_coverage_sources["releases"] = [source.source_id]
    report = analyze_core(feature_package, artifact())
    assert report.result_status == "insufficient_evidence"
    assert report.reason_code == "core_history_unverified"
    assert report.coverage[0].historical_validity == "unverified"


def test_frozen_policy_rejects_out_of_domain_features(feature_package, artifact):
    feature_package.snapshot.features.releases_total = 2
    report = analyze_core(feature_package, artifact())
    assert report.result_status == "insufficient_evidence"
    assert report.reason_code == "outside_calibrated_feature_profile"
    assert report.applicability.status == "out_of_distribution"


def test_official_status_takes_precedence_before_loading_a_model(feature_package, tmp_path):
    example = json.loads((EXAMPLES / "final_report_already_abandoned.json").read_bytes())
    data = feature_package.model_dump()
    data["official_status"] = {
        **example["official_status"],
        "announced_at": CUTOFF,
        "effective_at": CUTOFF,
        "verified_at": HORIZON,
        "available_at": CUTOFF,
    }
    evidence = example["evidence"][0]
    evidence.update(event_at=CUTOFF)
    evidence["citations"][0]["source_id"] = "source:core"
    data["official_evidence"] = [evidence]
    report = analyze_core(CoreFeaturePackage.model_validate(data), tmp_path / "missing-model.json")
    assert report.result_status == "already_abandoned"
    assert "prediction" not in report.model_dump()
