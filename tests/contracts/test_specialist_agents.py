"""D07 routing, D06 temporal/quote gates and bounded review invariants."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.export_specialist_agents import check_artifacts, generate_artifacts
from scripts.validate_specialist_examples import validate_examples

from reposeer.contracts.specialist_agents import (
    REQUEST_ADAPTER,
    RESPONSE_ADAPTER,
    REVIEW_ADAPTER,
    validate_agent_pair,
    validate_conflict_reviews,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples/agents"


def fixture(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind", ["maintenance", "functional", "alternatives"])
def test_three_specialists_have_valid_request_response_pairs(kind):
    request = REQUEST_ADAPTER.validate_python(fixture(f"{kind}_request"))
    response = RESPONSE_ADAPTER.validate_python(fixture(f"{kind}_response"))
    assert validate_agent_pair(request, response) == response
    assert REQUEST_ADAPTER.validate_json(request.model_dump_json()) == request
    assert RESPONSE_ADAPTER.validate_json(response.model_dump_json()) == response
    assert len(response.signals) == 1


def test_examples_and_export_are_deterministic():
    assert validate_examples(EXAMPLES) == 3
    artifacts = generate_artifacts()
    assert artifacts == generate_artifacts()
    assert check_artifacts(ROOT / "docs/contracts", artifacts) == []
    for content in artifacts.values():
        schema = json.loads(content)
        assert schema["$schema"].endswith("2020-12/schema")
        assert schema["additionalProperties"] is False
        for definition in schema.get("$defs", {}).values():
            if "properties" in definition:
                assert definition["additionalProperties"] is False
                assert set(definition["required"]) == set(definition["properties"])


@pytest.mark.parametrize("module", ["export_specialist_agents", "validate_specialist_examples"])
def test_cli(module):
    command = [sys.executable, "-m", f"scripts.{module}"]
    if module.startswith("export"):
        command.append("--check")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_export_check_detects_drift_without_writes(tmp_path, monkeypatch):
    import scripts.export_specialist_agents as exporter

    directory = tmp_path / "docs/contracts"
    directory.mkdir(parents=True)
    artifacts = generate_artifacts()
    for name, data in artifacts.items():
        (directory / name).write_bytes(data)
    bad = directory / "specialist_agent_output.schema.json"
    bad.write_bytes(b"drift\n")
    monkeypatch.setattr(exporter, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["export_specialist_agents.py", "--check"])
    assert exporter.main() == 1
    assert bad.read_bytes() == b"drift\n"


def test_export_check_accepts_windows_checkout_line_endings(tmp_path):
    artifacts = generate_artifacts()
    for name, data in artifacts.items():
        (tmp_path / name).write_bytes(data.replace(b"\n", b"\r\n"))
    assert check_artifacts(tmp_path, artifacts) == []


@pytest.mark.parametrize("change", [
    {"agent_kind": "functional_trend_demand"},
    {"package_id": "pypi:another-package"},
    {"cutoff_at": "2026-09-01T00:00:00.000Z"},
])
def test_request_route_package_and_cutoff_are_strict(change):
    request = fixture("maintenance_request")
    request.update(change)
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(request)


@pytest.mark.parametrize("path,value", [
    ("gate.decision", "unknown"),
    ("gate.allowed_use", "status_candidate"),
    ("available_at", "2026-10-01T00:01:00.000Z"),
    ("valid_at", "2026-10-01T00:01:00.000Z"),
    ("event_at", "2027-01-01T00:00:00.000Z"),
    ("passage_sha256", "0" * 64),
])
def test_d06_input_receipt_revision_and_asof_gate(path, value):
    request = fixture("maintenance_request")
    current = request["evidence"][0]
    if path.startswith("gate."):
        current["gate"][path.removeprefix("gate.")] = value
        if path == "gate.allowed_use":
            current["source_kind"] = "external"
    else:
        current[path] = value
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(request)


@pytest.mark.parametrize("field,value", [
    ("probability", 0.9), ("risk_level", "high"), ("ground_truth_label", 1),
])
def test_agent_cannot_return_probability_risk_or_label(field, value):
    response = fixture("functional_response")
    response[field] = value
    with pytest.raises(ValidationError):
        RESPONSE_ADAPTER.validate_python(response)
    response = fixture("functional_response")
    response["signals"][0][field] = value
    with pytest.raises(ValidationError):
        RESPONSE_ADAPTER.validate_python(response)


@pytest.mark.parametrize("kind", ["functional", "alternatives"])
def test_nonmaintenance_specialists_cannot_propose_official_status(kind):
    response = fixture(f"{kind}_response")
    response["signals"][0]["proposed_use"] = "status_candidate"
    with pytest.raises(ValidationError):
        RESPONSE_ADAPTER.validate_python(response)


def test_known_outcome_claims_cannot_become_e2_features():
    response = fixture("maintenance_response")
    response["signals"][0]["proposed_use"] = "feature_candidate"
    with pytest.raises(ValidationError, match="known-outcome"):
        RESPONSE_ADAPTER.validate_python(response)
    response = fixture("alternatives_response")
    response["signals"][0]["signal_type"] = "official_replacement_claim"
    with pytest.raises(ValidationError, match="known-outcome"):
        RESPONSE_ADAPTER.validate_python(response)


@pytest.mark.parametrize("field,value", [
    ("quote", "A fabricated quote"),
    ("start_offset", 501),
    ("source_revision", "different-revision"),
    ("source_id", "other-source"),
    ("subject_id", "pypi:another-package"),
])
def test_pair_rechecks_exact_citation_and_subject(field, value):
    request = fixture("alternatives_request")
    response = fixture("alternatives_response")
    target = response["signals"][0]
    if field == "subject_id":
        target[field] = value
    else:
        target["citations"][0][field] = value
    with pytest.raises((ValidationError, ValueError)):
        validate_agent_pair(request, response)


def test_pair_prohibits_report_only_input_promotion():
    request = fixture("functional_request")
    request["evidence"][0]["gate"]["allowed_use"] = "report_only"
    with pytest.raises(ValueError, match="cannot become a predictive feature"):
        validate_agent_pair(request, fixture("functional_response"))


def test_pair_prohibits_external_event_candidate():
    request = fixture("maintenance_request")
    request["evidence"][0]["source_kind"] = "external"
    request["evidence"][0]["gate"]["allowed_use"] = "report_only"
    with pytest.raises(ValueError, match="official"):
        validate_agent_pair(request, fixture("maintenance_response"))


@pytest.mark.parametrize("field,value", [
    ("review_calls", 2),
    ("final_use", "e2_feature"),
    ("status", "resolved"),
    ("deterministic_recheck_passed", True),
])
def test_conflict_review_is_bounded_and_unresolved_stays_report_only(field, value):
    review = fixture("conflict_review")
    review[field] = value
    with pytest.raises(ValidationError):
        REVIEW_ADAPTER.validate_python(review)


def test_zero_call_no_conflict_and_one_call_resolved_require_recheck():
    review = fixture("conflict_review")
    review.update(status="no_conflict", review_calls=0, reviewer_version=None,
                  deterministic_recheck_passed=True, final_use="e2_feature")
    REVIEW_ADAPTER.validate_python(review)
    resolved = copy.deepcopy(review)
    resolved.update(status="resolved", review_calls=1, reviewer_version="synthetic-reviewer-1")
    REVIEW_ADAPTER.validate_python(resolved)
    resolved["deterministic_recheck_passed"] = False
    with pytest.raises(ValidationError):
        REVIEW_ADAPTER.validate_python(resolved)


def test_one_persisted_review_per_cluster():
    review = fixture("conflict_review")
    assert len(validate_conflict_reviews([review])) == 1
    with pytest.raises(ValueError, match="review clusters"):
        validate_conflict_reviews([review, copy.deepcopy(review)])
