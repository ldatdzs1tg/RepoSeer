"""Offline T-026 flow through all three specialists and the CLI."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from reposeer.agents import MultiAgentCoordinator, MultiAgentRunRequest, MultiAgentRunResult
from reposeer.agents.cli import main
from reposeer.agents.intake import SPECIALISTS
from reposeer.agents.provider import ReplayBundle, ReplaySpecialistLLM
from reposeer.contracts.specialist_agents import validate_agent_pair

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples/agents'


def strict_demo():
    request = MultiAgentRunRequest.model_validate_json(
        (EXAMPLES / 'multi_agent_strict_demo.json').read_bytes())
    bundle = ReplayBundle.model_validate_json(
        (EXAMPLES / 'multi_agent_strict_replay.json').read_bytes())
    return request, ReplaySpecialistLLM(bundle)


class SyntheticReceiptVerifier:
    """Test-only fixture backend; never a production D06 verifier."""

    def verify(self, request, evidence):
        return evidence.gate.policy_version == 'synthetic-d06-policy-1'


@pytest.mark.parametrize('trusted', [False, True])
def test_three_specialists_strict_flow_preserves_grounding_and_promotion_boundary(trusted):
    request, provider = strict_demo()
    original = request.model_dump_json()
    coordinator = MultiAgentCoordinator(
        provider, execution_mode='replay',
        d06_verifier=SyntheticReceiptVerifier() if trusted else None,
        clock=lambda: datetime(2026, 10, 1, 0, 10, tzinfo=UTC),
    )
    result = coordinator.analyze(request)
    assert result == coordinator.analyze(request)
    assert request.model_dump_json() == original
    assert result.provider_calls == 6
    assert result.overall_status == ('complete' if trusted else 'partial')
    assert [response.agent_kind for response in result.strict_responses] == list(SPECIALISTS)
    assert len(result.validated_signal_candidates) == 3
    assert result.conflict_reviews == []
    assert result.coverage_summary.assignments_received == 6
    assert result.coverage_summary.assignments_processed == 6
    assert result.coverage_summary.assignments_unprocessed == 0
    for child, response in zip(request.strict_requests, result.strict_responses, strict=True):
        assert validate_agent_pair(child, response) == response
        assert len(response.signals) == 1
        signal = response.signals[0]
        citation = signal.citations[0]
        assert citation.source_revision == child.evidence[0].source_revision
        assert citation.available_at <= request.cutoff_at
        assert citation.quote in child.evidence[0].passage_text
    for candidate in result.validated_signal_candidates:
        assert candidate.trusted_d06_verified is trusted
        assert candidate.disposition == ('provisional' if trusted else 'report_only')
        assert candidate.e2_eligible is False
    # Official plans remain leads for D09; the coordinator has no score/status output.
    maintenance = result.strict_responses[0].signals[0]
    assert maintenance.proposed_use == 'status_candidate'
    assert maintenance.effective_at > result.cutoff_at
    assert 'prediction' not in result.model_dump()
    assert 'result_status' not in result.model_dump()


def test_strict_budget_failure_retains_successful_specialists_and_counts_missing_work():
    request, provider = strict_demo()
    result = MultiAgentCoordinator(
        provider, execution_mode='replay', max_llm_calls=5,
        d06_verifier=SyntheticReceiptVerifier(),
    ).analyze(request)
    assert result.provider_calls == 5
    assert [response.status for response in result.strict_responses] == [
        'complete', 'complete', 'failed']
    assert len(result.validated_signal_candidates) == 2
    strict_statuses = [status for status in result.agent_execution_statuses if status.lane == 'strict']
    assert [status.received_candidates for status in strict_statuses] == [1, 1, 1]
    assert [status.processed_candidates for status in strict_statuses] == [1, 1, 0]
    assert [status.unprocessed_candidates for status in strict_statuses] == [0, 0, 1]
    assert strict_statuses[-1].error_code == 'llm_call_budget_exhausted'
    assert result.coverage_summary.assignments_received == 6
    assert result.coverage_summary.assignments_processed == 5
    assert result.coverage_summary.assignments_unprocessed == 1
    assert not result.coverage_summary.processing_complete
    assert result.overall_status == 'partial'


def test_live_unverified_strict_inputs_are_counted_without_calling_extraction():
    request, provider = strict_demo()
    result = MultiAgentCoordinator(provider, execution_mode='live').analyze(request)
    assert result.provider_calls == 3  # Research only; strict extraction is blocked.
    assert result.validated_signal_candidates == []
    assert all(response.error_code == 'd06_receipt_unverified' for response in result.strict_responses)
    assert result.coverage_summary.assignments_received == 6
    assert result.coverage_summary.assignments_processed == 3
    assert result.coverage_summary.assignments_unprocessed == 3


def test_strict_demo_cli_serializes_all_three_grounded_analyses(tmp_path):
    output = tmp_path / 'strict-run.json'
    assert main([
        '--mode', 'replay', '--input', str(EXAMPLES / 'multi_agent_strict_demo.json'),
        '--responses', str(EXAMPLES / 'multi_agent_strict_replay.json'),
        '--output', str(output),
    ]) == 0
    result = MultiAgentRunResult.model_validate_json(output.read_bytes())
    assert result.execution_mode == 'replay'
    assert result.overall_status == 'partial'
    assert len(result.strict_responses) == len(result.validated_signal_candidates) == 3
    assert result.provider_calls == 6
    assert all(not candidate.e2_eligible for candidate in result.validated_signal_candidates)


def test_strict_demo_rejects_postcutoff_evidence_before_execution():
    document = json.loads((EXAMPLES / 'multi_agent_strict_demo.json').read_bytes())
    evidence = document['strict_requests'][0]['evidence'][0]
    evidence['available_at'] = evidence['fetched_at'] = '2026-10-02T00:00:00.000Z'
    evidence['gate']['checked_at'] = '2026-10-02T00:01:00.000Z'
    with pytest.raises(ValidationError, match='source revision available at cutoff'):
        MultiAgentRunRequest.model_validate(document)
