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
    RESEARCH_REQUEST_ADAPTER,
    RESEARCH_RESPONSE_ADAPTER,
    RESPONSE_ADAPTER,
    REVIEW_ADAPTER,
    D06Evidence,
    ExtractedSignal,
    ResearchCandidate,
    validate_agent_pair,
    validate_conflict_reviews,
    validate_research_handoff_pair,
    validate_review_references,
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
    assert set(artifacts) == {
        "specialist_agent_input.schema.json", "specialist_agent_output.schema.json",
        "specialist_conflict_review.schema.json", "specialist_research_request.schema.json",
        "specialist_research_response.schema.json",
    }
    assert artifacts == generate_artifacts()
    assert check_artifacts(ROOT / "docs/contracts", artifacts) == []
    for content in artifacts.values():
        schema = json.loads(content)
        assert schema["$schema"].endswith("2020-12/schema")
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
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


@pytest.mark.parametrize("filename", [
    "specialist_agent_output.schema.json", "specialist_research_response.schema.json",
])
def test_export_check_detects_drift_without_writes(tmp_path, monkeypatch, filename):
    import scripts.export_specialist_agents as exporter

    directory = tmp_path / "docs/contracts"
    directory.mkdir(parents=True)
    artifacts = generate_artifacts()
    for name, data in artifacts.items():
        (directory / name).write_bytes(data)
    bad = directory / filename
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


def test_research_accepts_incomplete_candidates_without_strict_routing():
    request = fixture("research_request")
    request["candidates"][0].update(
        source_url=None, published_at=None, available_at=None, fetched_at=None,
        text_excerpt=None, source_kind="unknown", topic_hint="maintenance",
    )
    request = RESEARCH_REQUEST_ADAPTER.validate_python(request)
    candidate = request.candidates[0]
    assert isinstance(candidate, ResearchCandidate)
    assert candidate.available_at is None and candidate.text_excerpt is None
    response = validate_research_handoff_pair(request, fixture("research_response"))
    assert response.status == "complete"
    assert RESEARCH_REQUEST_ADAPTER.validate_json(request.model_dump_json()) == request
    assert RESEARCH_RESPONSE_ADAPTER.validate_json(response.model_dump_json()) == response


def test_research_keeps_candidates_available_only_after_cutoff():
    request = fixture("research_request")
    request["candidates"][0]["available_at"] = "2026-11-01T00:00:00.000Z"
    request = RESEARCH_REQUEST_ADAPTER.validate_python(request)
    assert request.candidates[0].available_at > request.cutoff_at
    validate_research_handoff_pair(request, fixture("research_response"))


@pytest.mark.parametrize("planning_field", ["information_gaps", "enrichment_suggestions"])
def test_zero_evidence_research_requires_planning_without_fake_insights(planning_field):
    request = fixture("research_request")
    request.update(candidates=[], package_description=None, description_source_refs=[])
    RESEARCH_REQUEST_ADAPTER.validate_python(request)
    response = fixture("research_response")
    planning = response[planning_field][0]
    planning["related_source_refs"] = []
    response.update(insights=[], information_gaps=[], enrichment_suggestions=[])
    response[planning_field] = [planning]
    assert validate_research_handoff_pair(request, response).insights == []
    response[planning_field] = []
    with pytest.raises(ValueError, match="requires a gap or suggestion"):
        validate_research_handoff_pair(request, response)


def test_bare_description_reference_cannot_support_zero_input_insights():
    request = fixture("research_request")
    request.update(candidates=[], package_description=None)
    response = fixture("research_response")
    response["insights"][0]["source_refs"] = request["description_source_refs"]
    response.update(information_gaps=[], enrichment_suggestions=[])
    with pytest.raises(ValueError, match="cannot produce insights"):
        validate_research_handoff_pair(request, response)


@pytest.mark.parametrize("field,reference_field", [
    ("insights", "source_refs"),
    ("information_gaps", "related_source_refs"),
    ("enrichment_suggestions", "related_source_refs"),
])
def test_all_research_outputs_require_request_source_references(field, reference_field):
    response = fixture("research_response")
    # A request-local candidate ID is not its persisted source reference.
    response[field][0][reference_field] = ["candidate:functional:1"]
    with pytest.raises(ValueError, match="source reference is absent"):
        validate_research_handoff_pair(fixture("research_request"), response)


@pytest.mark.parametrize("field,value", [
    ("probability", 0.9), ("ground_truth_label", 1), ("risk_level", "high"),
    ("official_status", "verified"), ("feature_value", 1), ("confidence", 0.9),
])
def test_research_cannot_return_prediction_status_or_feature_fields(field, value):
    for target in ("response", "insight"):
        response = fixture("research_response")
        container = response if target == "response" else response["insights"][0]
        container[field] = value
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            validate_research_handoff_pair(fixture("research_request"), response)


@pytest.mark.parametrize("model", [D06Evidence, ExtractedSignal])
def test_research_candidate_is_not_strict_evidence_or_signal(model):
    candidate = RESEARCH_REQUEST_ADAPTER.validate_python(fixture("research_request")).candidates[0]
    with pytest.raises(ValidationError):
        model.model_validate(candidate)
    with pytest.raises(ValidationError):
        model.model_validate(candidate.model_dump())
    request = fixture("functional_request")
    request["evidence"] = [candidate.model_dump()]
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(request)


def test_research_requires_description_provenance_and_unique_candidate_identity():
    request = fixture("research_request")
    request["description_source_refs"] = []
    with pytest.raises(ValidationError, match="description requires source references"):
        RESEARCH_REQUEST_ADAPTER.validate_python(request)
    request = fixture("research_request")
    request["candidates"].append(copy.deepcopy(request["candidates"][0]))
    with pytest.raises(ValidationError, match="research candidate IDs"):
        RESEARCH_REQUEST_ADAPTER.validate_python(request)


@pytest.mark.parametrize("field,limit", [("candidates", 50), ("research_questions", 10)])
def test_research_intake_is_bounded(field, limit):
    request = fixture("research_request")
    if field == "candidates":
        candidate = request[field][0]
        request[field] = [dict(candidate, candidate_id=f"candidate:{i}") for i in range(limit)]
    else:
        request[field] *= limit
    RESEARCH_REQUEST_ADAPTER.validate_python(request)
    extra = dict(candidate, candidate_id="candidate:extra") if field == "candidates" else "Another question?"
    request[field].append(extra)
    with pytest.raises(ValidationError, match="at most"):
        RESEARCH_REQUEST_ADAPTER.validate_python(request)


@pytest.mark.parametrize("field,limit", [("package_description", 5000), ("text_excerpt", 10000)])
def test_research_text_is_bounded(field, limit):
    request = fixture("research_request")
    target = request if field == "package_description" else request["candidates"][0]
    target[field] = "x" * limit
    RESEARCH_REQUEST_ADAPTER.validate_python(request)
    target[field] += "x"
    with pytest.raises(ValidationError, match="at most"):
        RESEARCH_REQUEST_ADAPTER.validate_python(request)


@pytest.mark.parametrize("field,value", [
    ("analysis_run_id", "00000000-0000-4000-8000-000000000011"),
    ("request_id", "00000000-0000-4000-8000-000000000005"),
    ("agent_kind", "maintenance_lifecycle"),
    ("package_id", "pypi:another-package"),
    ("cutoff_at", "2026-09-30T00:00:00.000Z"),
])
def test_research_pair_requires_matching_identity(field, value):
    response = fixture("research_response")
    response[field] = value
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_research_handoff_pair(fixture("research_request"), response)


@pytest.mark.parametrize("field", ["insights", "information_gaps", "enrichment_suggestions"])
def test_research_rejects_duplicate_output_ids(field):
    response = fixture("research_response")
    response[field].append(copy.deepcopy(response[field][0]))
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        validate_research_handoff_pair(fixture("research_request"), response)


@pytest.mark.parametrize("status,error_code", [
    ("complete", None), ("partial", "research_incomplete"), ("failed", "source_unavailable"),
])
def test_research_status_and_error_rules(status, error_code):
    response = fixture("research_response")
    response.update(status=status, error_code=error_code)
    if status == "failed":
        response["insights"] = []
    validate_research_handoff_pair(fixture("research_request"), response)
    response["error_code"] = "unexpected_error" if status == "complete" else None
    with pytest.raises(ValidationError, match="require an error code"):
        validate_research_handoff_pair(fixture("research_request"), response)


def test_research_failed_insights_and_pre_cutoff_generation_are_rejected():
    response = fixture("research_response")
    response.update(status="failed", error_code="source_unavailable")
    with pytest.raises(ValidationError, match="cannot contain insights"):
        RESEARCH_RESPONSE_ADAPTER.validate_python(response)
    response = fixture("research_response")
    response["generated_at"] = "2026-09-30T00:00:00.000Z"
    with pytest.raises(ValidationError, match="cannot precede cutoff"):
        RESEARCH_RESPONSE_ADAPTER.validate_python(response)


def test_research_pair_revalidates_mutated_models():
    request = RESEARCH_REQUEST_ADAPTER.validate_python(fixture("research_request"))
    response = RESEARCH_RESPONSE_ADAPTER.validate_python(fixture("research_response"))
    response.insights[0].source_refs = []
    with pytest.raises(ValidationError):
        validate_research_handoff_pair(request, response)
    response = RESEARCH_RESPONSE_ADAPTER.validate_python(fixture("research_response"))
    request.candidates[0].available_at = "not-a-timestamp"
    with pytest.raises(ValidationError):
        validate_research_handoff_pair(request, response)


def test_status_announcement_must_precede_cutoff_but_effective_date_may_follow():
    request = fixture("maintenance_request")
    response = fixture("maintenance_response")
    valid = validate_agent_pair(request, response)
    assert valid.signals[0].announced_at <= valid.cutoff_at < valid.signals[0].effective_at
    response["signals"][0]["announced_at"] = "2026-10-01T00:00:00.001Z"
    with pytest.raises(ValidationError, match="announcement must be known by cutoff"):
        validate_agent_pair(request, response)


def review_with_validated_responses():
    review = fixture("conflict_review")
    request = fixture("maintenance_request")
    response = fixture("maintenance_response")
    first = validate_agent_pair(request, response)
    request["request_id"] = response["request_id"] = "00000000-0000-4000-8000-000000000005"
    request["evidence"][0]["source_id"] = review["source_ids"][1]
    response["signals"][0]["signal_id"] = review["signal_ids"][1]
    response["signals"][0]["citations"][0]["source_id"] = review["source_ids"][1]
    second = validate_agent_pair(request, response)
    return review, [first, second]


def test_review_references_bind_verified_responses_and_keep_cluster_uniqueness():
    review, responses = review_with_validated_responses()
    assert validate_review_references([review], responses) == [REVIEW_ADAPTER.validate_python(review)]
    with pytest.raises(ValueError, match="review clusters"):
        validate_review_references([review, copy.deepcopy(review)], responses)


@pytest.mark.parametrize("field,value", [
    ("signal_ids", ["signal:maintenance:1", "missing-signal"]),
    ("source_ids", ["source:maintenance:1", "missing-source"]),
    ("source_ids", ["source:maintenance:1"]),
    ("source_ids", ["source:maintenance:1", "source:maintenance:contradictory-2", "extra-source"]),
    ("subject_id", "pypi:another-package"),
    ("analysis_run_id", "00000000-0000-4000-8000-000000000011"),
])
def test_review_references_reject_orphan_or_mismatched_lineage(field, value):
    review, responses = review_with_validated_responses()
    review[field] = value
    with pytest.raises(ValueError, match="review"):
        validate_review_references([review], responses)


def test_review_references_reject_duplicate_signal_ids_within_run_only():
    review, responses = review_with_validated_responses()
    with pytest.raises(ValueError, match="duplicate signal IDs across responses"):
        validate_review_references([review], [*responses, responses[0]])
    request = fixture("maintenance_request")
    response = fixture("maintenance_response")
    request["analysis_run_id"] = response["analysis_run_id"] = "00000000-0000-4000-8000-000000000011"
    other_run = validate_agent_pair(request, response)
    assert len(validate_review_references([review], [*responses, other_run])) == 1
