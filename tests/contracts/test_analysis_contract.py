"""Analysis boundary regressions: score exclusivity, temporal safety and lineage."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.export_analysis_contract import check_artifacts, generate_artifacts
from scripts.validate_analysis_examples import validate_examples

from reposeer.contracts.analysis import validate_final_report

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples/analysis"


def example(name="final_report_core"):
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def set_value(data, path, value):
    parts = path.split(".")
    target = data
    for key in parts[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if isinstance(target, list):
        target[int(parts[-1])] = value
    else:
        target[parts[-1]] = value


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.stem)
def test_examples_round_trip(path):
    raw = path.read_bytes()
    report = validate_final_report(raw)
    assert validate_final_report(raw.decode()) == report
    assert validate_final_report(json.loads(raw)) == report
    assert validate_final_report(report) == report
    assert validate_final_report(report.model_dump_json()) == report


def test_example_validation_and_generated_artifacts():
    assert validate_examples(EXAMPLES) == 6
    artifacts = generate_artifacts()
    assert artifacts == generate_artifacts()
    assert check_artifacts(ROOT / "docs/contracts", artifacts) == []
    schema = json.loads(artifacts["analysis_contract.schema.json"])
    assert schema["discriminator"]["propertyName"] == "result_status"
    assert schema["$schema"].endswith("2020-12/schema")
    definitions = schema["$defs"]
    for name in ("AlreadyAbandonedReport", "ScheduledCessationReport", "NoScoreReport"):
        assert definitions[name]["additionalProperties"] is False
        for field in ("prediction", "core_prediction", "probability"):
            assert field not in definitions[name]["properties"]
    assert "prediction" in definitions["PredictedReport"]["required"]
    for definition in definitions.values():
        if "properties" in definition:
            assert set(definition["required"]) == set(definition["properties"])
    ts = artifacts["analysis_types.ts"].decode()
    assert "export type FinalReport = PredictedReport |" in ts
    assert 'result_status: "predicted";' in ts
    assert "probability: number;" in ts
    assert "any" not in ts.split()


def test_exporter_check_detects_drift_without_writing(tmp_path, monkeypatch):
    import scripts.export_analysis_contract as exporter

    directory = tmp_path / "docs/contracts"
    directory.mkdir(parents=True)
    artifacts = generate_artifacts()
    for name, data in artifacts.items():
        (directory / name).write_bytes(data)
    broken = directory / "analysis_types.ts"
    broken.write_bytes(b"drift\n")
    monkeypatch.setattr(exporter, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["export_analysis_contract.py", "--check"])
    assert exporter.main() == 1
    assert broken.read_bytes() == b"drift\n"
    assert (directory / "analysis_contract.schema.json").read_bytes() == artifacts["analysis_contract.schema.json"]


def test_generated_artifacts_accept_windows_checkout_line_endings(tmp_path):
    artifacts = generate_artifacts()
    for name, data in artifacts.items():
        (tmp_path / name).write_bytes(data.replace(b"\n", b"\r\n"))
    assert check_artifacts(tmp_path, artifacts) == []


@pytest.mark.parametrize("module", ["export_analysis_contract", "validate_analysis_examples"])
def test_analysis_cli(module):
    command = [sys.executable, "-m", f"scripts.{module}"]
    if module.startswith("export"):
        command.append("--check")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("fixture", [
    "final_report_already_abandoned", "final_report_scheduled_cessation", "final_report_no_score"
])
@pytest.mark.parametrize("field,value", [
    ("probability", 0), ("probability", None), ("prediction", None),
    ("prediction", {"probability": 0.8}), ("core_prediction", None),
])
def test_unscored_wire_branches_prohibit_all_prediction_fields(fixture, field, value):
    data = example(fixture)
    assert '"probability"' not in json.dumps(validate_final_report(data).model_dump())
    data[field] = value
    with pytest.raises(ValidationError):
        validate_final_report(data)


@pytest.mark.parametrize("value", [-0.01, 1.01, "0.5", True, float("nan"), float("inf")])
def test_probability_is_finite_strict_and_bounded(value):
    data = example()
    data["prediction"]["probability"] = value
    with pytest.raises(ValidationError):
        validate_final_report(data)


@pytest.mark.parametrize("value", [0, 1, 0.5])
def test_real_prediction_can_be_zero_or_one(value):
    data = example()
    data["prediction"]["probability"] = value
    assert validate_final_report(data).prediction.probability == value


@pytest.mark.parametrize("path,value", [
    ("schema_version", "99.0.0"), ("package_id", "pypi:Synthetic_Demo"),
    ("horizon_months", 12.0), ("horizon_months", True), ("horizon_months", 13),
    ("cutoff_at", "2026-10-01T00:00:00Z"),
    ("horizon_end_at", "2027-09-30T00:00:00.000Z"),
    ("generated_at", "2026-09-01T00:00:00.000Z"),
    ("mapping", None), ("mapping.repo_group_id", "github:repository:2"),
    ("mapping.available_at", "2026-10-02T00:00:00.000Z"),
    ("mapping.valid_to", "2026-10-01T00:00:00.000Z"),
    ("mapping.source_ids", ["orphan"]), ("sources.0.available_at", None),
    ("sources.0.available_at", "2026-10-02T00:00:00.000Z"),
    ("sources.0.revision", None),
    ("prediction.feature_source_ids", ["orphan"]),
    ("prediction.feature_evidence_ids", ["orphan"]),
    ("prediction.model.feature_set", "E2"),
    ("prediction.model.frozen_at", "2026-10-02T00:00:00.000Z"),
    ("applicability.status", "out_of_distribution"),
    ("mode_used", "ENHANCED"), ("enhanced_status", "used"),
    ("fallback_reason", "unrequested"), ("external_insights_reason", "unrequested"),
    ("coverage.0.historical_validity", "unverified"),
    ("coverage.0.records_observed", False),
])
def test_rejects_inconsistent_prediction_context(path, value):
    data = example()
    set_value(data, path, value)
    with pytest.raises(ValidationError):
        validate_final_report(data)


@pytest.mark.parametrize("path,value", [
    ("core_prediction", None), ("core_prediction.model.feature_set", "E1"),
    ("coverage.1.source_coverage", "partial"),
    ("coverage.2.processed_documents", 0),
    ("coverage.2.processed_documents", 2),
    ("evidence.0.citations.0.source_id", "orphan"),
    ("evidence.0.citations.0.end_offset", 1),
    ("evidence.0.package_id", "pypi:another-package"),
    ("evidence.1.agent", None),
    ("prediction.model.feature_set", "E1"),
    ("dependency_impact.direct_dependents", 6),
    ("dependency_impact.graph_kind", "snapshot_resolved"),
    ("dependency_impact.graph_as_of", "2026-10-02T00:00:00.000Z"),
    ("dependency_impact.graph_as_of", "2020-01-01T00:00:00.000Z"),
])
def test_enhanced_evidence_coverage_and_impact_invariants(path, value):
    data = example("final_report_enhanced")
    set_value(data, path, value)
    with pytest.raises(ValidationError):
        validate_final_report(data)


def test_report_only_evidence_can_be_late_but_never_become_a_feature():
    data = example("final_report")
    late = "2026-10-01T00:05:00.000Z"
    data["sources"][1].update(valid_at=late, retrieved_at=late, available_at=late)
    for coverage in data["coverage"][1:]:
        coverage["historical_validity"] = "unverified"
    assert validate_final_report(data).prediction.probability == 0.24
    data["evidence"][0].update(use="feature", exclusion_reason=None)
    with pytest.raises(ValidationError, match="availability"):
        validate_final_report(data)


def test_official_status_overrides_existing_score_and_cannot_come_from_an_agent():
    known = example("final_report_already_abandoned")
    predicted = example()
    predicted["evidence"] = copy.deepcopy(known["evidence"])
    predicted["sources"].append(copy.deepcopy(known["sources"][-1]))
    with pytest.raises(ValidationError, match="precedence"):
        validate_final_report(predicted)
    known["evidence"][0]["origin"] = "external"
    with pytest.raises(ValidationError, match="official"):
        validate_final_report(known)


@pytest.mark.parametrize("fixture,path,value", [
    ("final_report_already_abandoned", "official_status.cessation_confirmed", False),
    ("final_report_already_abandoned", "official_status.effective_at", "2027-01-01T00:00:00.000Z"),
    ("final_report_already_abandoned", "official_status.announced_at", "2026-10-01T00:01:00.000Z"),
    ("final_report_already_abandoned", "official_status.evidence_ids", ["orphan"]),
    ("final_report_already_abandoned", "official_status.revoked_at", "2026-09-15T00:00:00.000Z"),
    ("final_report_scheduled_cessation", "within_horizon", False),
    ("final_report_scheduled_cessation", "official_status.effective_at", "2026-09-01T00:00:00.000Z"),
    ("final_report_scheduled_cessation", "official_status.cessation_confirmed", True),
])
def test_official_status_dates_revocation_and_horizon(fixture, path, value):
    data = example(fixture)
    set_value(data, path, value)
    with pytest.raises(ValidationError):
        validate_final_report(data)


def test_future_plan_outside_horizon_still_has_no_score_and_later_revocation_does_not_rewrite_cutoff():
    data = example("final_report_scheduled_cessation")
    data["official_status"].update(effective_at="2028-01-01T00:00:00.000Z", revoked_at="2027-01-01T00:00:00.000Z")
    data["generated_at"] = "2027-02-01T00:00:00.000Z"
    data["purpose"] = "retrospective_analysis"
    data["within_horizon"] = False
    assert validate_final_report(data).result_status == "scheduled_cessation"


def test_unknown_counts_are_null_and_impact_failure_does_not_clear_prediction():
    data = example()
    data["dependency_impact"] = {"impact_status": "unavailable", "missing_reason": "timeout"}
    assert validate_final_report(data).prediction == validate_final_report(example()).prediction
    data["dependency_impact"]["direct_dependents"] = 0
    with pytest.raises(ValidationError):
        validate_final_report(data)
    data = example()
    data["coverage"].append({**data["coverage"][0], "coverage_id": "external", "stage": "external",
                             "status": "unavailable", "source_coverage": "unknown", "source_ids": [],
                             "historical_validity": "unverified", "missing_reason": "timeout",
                             "records_observed": None})
    validate_final_report(data)
    data["coverage"][-1]["records_observed"] = 0
    with pytest.raises(ValidationError):
        validate_final_report(data)


@pytest.mark.parametrize("status", ["mapping_unresolved", "mapping_history_unverified", "unsupported", "insufficient_evidence", "failed"])
def test_all_explicit_no_score_statuses(status):
    data = example("final_report_no_score")
    data["result_status"] = status
    assert validate_final_report(data).mode_used is None


def test_backtest_requires_d10_provenance_and_a_model_frozen_before_test():
    data = example()
    data["purpose"] = "retrospective_analysis"
    validate_final_report(data)
    data["purpose"] = "prospective_backtest"
    with pytest.raises(ValidationError):
        validate_final_report(data)
    data["backtest"] = {"evaluation_id": "d10-demo", "fold_id": "fold-1", "manifest_sha256": "d" * 64,
                        "test_window": {"start_at": "2026-09-15T00:00:00.000Z", "end_at": "2026-12-31T00:00:00.000Z"}}
    validate_final_report(data)
    data["prediction"]["model"]["frozen_at"] = data["backtest"]["test_window"]["start_at"]
    with pytest.raises(ValidationError, match="before the test"):
        validate_final_report(data)


def test_mutated_instances_revalidated_and_unknown_fields_forbidden():
    report = validate_final_report(example())
    report.prediction.probability = 2.0
    with pytest.raises(ValidationError):
        validate_final_report(report)
    data = example("final_report")
    data["evidence"][1]["probability"] = 0.9
    with pytest.raises(ValidationError):
        validate_final_report(data)


def test_duplicates_rejected():
    for field in ("sources", "evidence", "coverage"):
        data = example("final_report")
        data[field].append(copy.deepcopy(data[field][0]))
        with pytest.raises(ValidationError):
            validate_final_report(data)
