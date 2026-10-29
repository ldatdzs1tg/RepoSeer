"""T-025 safety/semantic regressions, using explicit synthetic trusted backends."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from reposeer.analysis.external import (
    ExternalContextRequest,
    ExternalDocument,
    ValidatedAgentEvent,
    WindowCensus,
    analyze_external_context,
)
from reposeer.contracts import Article
from reposeer.contracts.analysis import (
    AgentProvenance,
    AnalysisCoverage,
    AnalysisEvidence,
    EvidenceCitation,
    SourceProvenance,
    validate_final_report,
)
from reposeer.contracts.identity import article_id, text_sha256
from reposeer.contracts.models import TimeRange
from reposeer.processing.relevance import RelevanceAssessment, RelevanceSpan

ROOT = Path(__file__).resolve().parents[3]
CUTOFF = "2026-10-01T00:00:00.000Z"
QUERY = "a" * 64
WINDOW = TimeRange(start_at="2026-09-01T00:00:00.000Z", end_at=CUTOFF)


class SyntheticVerifier:
    """Only test fixtures; not a production claim of corpus/archive verification."""
    def historical_document(self, document, cutoff_at):
        return True

    def census(self, census, documents):
        return True

    def agent_event(self, event):
        return True

    def agent_processing(self, coverage, documents):
        return True


def document(number=1, *, date="2026-09-20T00:00:00.000Z", text=None):
    data = json.loads((ROOT / "examples/contracts/article.json").read_text(encoding="utf-8"))
    data.update(
        canonical_url=f"https://example.test/synthetic/{number}",
        original_url=f"https://example.test/synthetic/{number}",
        title=f"Synthetic context {number}",
        article_id=article_id(f"https://example.test/synthetic/{number}"),
        package_ids=["pypi:synthetic-demo"], content_text=text or f"Synthetic demo use case {number}.",
        published_at=date, published_date=date[:10], published_precision="timestamp",
        content_available_at=date,
    )
    data["content_sha256"] = text_sha256(data["content_text"])
    data["provenance"].update(
        source_url=data["canonical_url"], retrieved_at=date, valid_at=date, available_at=date,
        raw_record_ids=[f"raw:00000000-0000-4000-8000-{number:012d}"],
    )
    article = Article.model_validate(data)
    assessment = RelevanceAssessment(
        article_id=article.article_id, target_package_id="pypi:synthetic-demo",
        target_repo_id="github:repository:900000001", content_sha256=article.content_sha256,
        context_level="package_specific", decision="relevant", match_method="synthetic",
        reason_code="synthetic_verified_link",
        evidence_spans=[RelevanceSpan(text=article.content_text, start_offset=0,
                                     end_offset=len(article.content_text))],
        evidence_urls=[], link_evidence=[], raw_record_ids=article.provenance.raw_record_ids,
    )
    return ExternalDocument(article=article, relevance=assessment, provider="synthetic",
                            query_intent="direct_package", query_set_hash=QUERY)


def census(documents, *, census_id="recent", window=WINDOW):
    source = SourceProvenance(
        **dict(source="source_archive", source_url=f"https://example.test/census/{census_id}",
               retrieved_at=CUTOFF, valid_at=CUTOFF, available_at=CUTOFF,
               temporal_basis="observation",
               raw_record_ids=["raw:00000000-0000-4000-8000-000000000099"], missing_fields=[]),
        source_id="census:" + census_id, source_record_id=census_id, revision="synthetic-v1",
        content_sha256=None, query_set_hash=QUERY,
    )
    return WindowCensus(census_id=census_id, provider="synthetic", context="direct_package",
                        query_intent="direct_package", window=window, source=source,
                        retrieval_policy_version="synthetic-1", filters_sha256="b" * 64,
                        status="complete", source_coverage="complete",
                        document_source_ids=[d.source_id for d in documents], missing_reason=None)


def request(documents, censuses=None, **extra):
    return ExternalContextRequest(
        package_id="pypi:synthetic-demo", cutoff_at=CUTOFF,
        generated_at="2026-10-01T00:10:00.000Z", documents=documents,
        censuses=[census(documents)] if censuses is None else censuses, **extra,
    )


def analyze(req):
    return analyze_external_context(req, verifier=SyntheticVerifier())


def test_verified_e1_grounded_evidence_rates_and_pure_determinism():
    req = request([document(2), document()])
    before = req.model_dump_json()
    result = analyze(req)
    assert result.e1[0].count == 2
    assert result.e1[0].rate_per_30_days == 2
    assert result.coverage[0].historical_validity == "verified"
    assert all(e.use == "feature" for e in result.evidence)
    assert req.model_dump_json() == before
    assert result == analyze(req)
    assert result == analyze(request(list(reversed(req.documents))))


def test_absent_census_verifier_never_promotes_complete_flags_or_zero():
    result = analyze_external_context(request([]))
    assert result.e1[0].count is None
    assert result.e1[0].rate_per_30_days is None
    assert result.e1[0].missing_reason == "source_census_unverified"
    assert result.coverage[0].records_observed is None
    assert result.external_insights_status == "unavailable"
    assert result.external_insights_reason is not None


def test_verified_empty_census_can_mean_zero():
    result = analyze(request([]))
    assert result.e1[0].count == 0
    assert result.e1[0].rate_per_30_days == 0
    assert result.coverage[0].records_observed == 0
    assert result.evidence == []
    assert result.coverage[0].status == result.coverage[0].source_coverage == "complete"
    assert result.coverage[0].historical_validity == "verified"
    assert result.external_insights_status == "complete"
    assert result.external_insights_reason is None


@pytest.mark.parametrize("status,source_coverage", [
    ("partial", "partial"), ("complete", "unknown"), ("unavailable", "unknown"),
])
def test_empty_incomplete_census_preserves_null_and_missing_reason(status, source_coverage):
    item = census([])
    item.status, item.source_coverage = status, source_coverage
    item.missing_reason = None if status == "complete" else "source_incomplete"
    result = analyze(request([], [item]))
    assert result.e1[0].count is result.e1[0].rate_per_30_days is None
    assert result.e1[0].missing_reason is not None
    assert result.coverage[0].source_coverage != "complete"
    assert result.coverage[0].historical_validity == "unverified"
    assert result.external_insights_status in {"partial", "unavailable"}
    assert result.external_insights_reason is not None


def test_current_capture_unknown_availability_remains_report_only_and_null():
    doc = document()
    doc.article.provenance.available_at = None
    doc.article.content_available_at = None
    req = request([doc])
    result = analyze(req)
    assert result.e1[0].count is None
    assert result.evidence[0].use == "report_only"
    assert result.evidence[0].exclusion_reason == "source_availability_unverified"
    assert result.external_insights_status == "partial"
    assert "probability" not in result.model_dump_json()


@pytest.mark.parametrize("case", ["snippet", "unknown_publication", "late_revision",
                                  "known_outcome", "declared_outcome", "historical"])
def test_temporal_and_outcome_gates(case):
    doc = document()
    if case == "snippet":
        doc.article.content_status = "snippet"
    elif case == "unknown_publication":
        doc.article.published_at = doc.article.published_date = None
        doc.article.published_precision = "unknown"
    elif case == "late_revision":
        doc.article.provenance.retrieved_at = "2026-10-01T00:05:00.000Z"
        doc.article.provenance.valid_at = doc.article.provenance.retrieved_at
        doc.article.updated_at = doc.article.provenance.retrieved_at
    elif case == "known_outcome":
        doc = document(text="Synthetic package is no longer maintained.")
    elif case == "declared_outcome":
        doc.outcome_disclosure = True
    else:
        doc.article.provenance.temporal_basis = "historical_reconstruction"
    result = analyze_external_context(request([doc]))
    assert result.evidence[0].use == "report_only"
    assert result.e1[0].count is None


def test_relevance_quote_revision_and_target_are_revalidated():
    doc = document()
    doc.relevance.evidence_spans[0].text = "x" * len(doc.article.content_text)
    with pytest.raises(ValidationError, match="offset_mismatch"):
        request([doc])
    doc = document()
    doc.relevance.target_package_id = "pypi:other"
    with pytest.raises(ValidationError, match="another package"):
        request([doc])


def test_exact_syndication_and_original_ids_do_not_double_count():
    first = document(1, text="Synthetic copied article.")
    second = document(2, text=first.article.content_text)
    third = document(3, text="Synthetic rewritten article.")
    second.origin_id = third.origin_id = "original:1"
    assert analyze(request([first, second, third])).e1[0].count == 1


@pytest.mark.parametrize("case", ["partial", "missing_member", "query_mismatch", "omitted_member"])
def test_incomplete_or_mismatched_census_is_null(case):
    docs = [document()]
    item = census(docs)
    if case == "partial":
        item.status, item.source_coverage, item.missing_reason = "partial", "partial", "budget_exhausted"
    elif case == "missing_member":
        item.document_source_ids.append("external:missing")
    elif case == "query_mismatch":
        item.source.query_set_hash = "c" * 64
    else:
        docs.append(document(2))
    result = analyze(request(docs, [item]))
    assert result.e1[0].count is None
    assert result.coverage[0].source_coverage != "complete"


def test_same_query_policy_and_equal_adjacent_windows_required_for_trend():
    old = document(2, date="2026-08-20T00:00:00.000Z")
    recent = document()
    previous_window = TimeRange(start_at="2026-08-02T00:00:00.000Z",
                                end_at=WINDOW.start_at)
    before = census([old], census_id="previous", window=previous_window)
    after = census([recent])
    result = analyze(request([old, recent], [before, after]))
    assert result.trends[0].log1p_count_delta == 0
    before.source.query_set_hash = "d" * 64
    result = analyze(request([old, recent], [before, after]))
    assert result.trends[0].log1p_count_delta is None
    assert result.trends[0].missing_reason == "windows_not_comparable"


def event(doc, *, event_id="event:1", evidence_id="signal:1"):
    item = AnalysisEvidence(
        evidence_id=evidence_id, package_id="pypi:synthetic-demo", origin="agent",
        context="direct_package", signal_type="functional_demand", claim=doc.article.content_text,
        event_at=doc.article.published_at,
        citations=[EvidenceCitation(source_id=doc.source_id, quote=doc.article.content_text,
                                   start_offset=0, end_offset=len(doc.article.content_text))],
        use="feature", exclusion_reason=None, uncertainty=None,
        agent=AgentProvenance(agent_id="functional-demand", agent_version="synthetic-1",
                              model_version="synthetic", prompt_version="synthetic-1"),
    )
    return ValidatedAgentEvent(event_id=event_id, evidence=item)


def agent_coverage(docs):
    return AnalysisCoverage(
        coverage_id="agent:recent", stage="agent", source_ids=[d.source_id for d in docs],
        status="complete", source_coverage="complete", historical_validity="verified",
        window=WINDOW, records_observed=len(docs), eligible_documents=len(docs),
        processed_documents=len(docs), missing_reason=None,
    )


def test_e2_verified_events_dedup_independent_of_e1_counts():
    doc = document()
    first, duplicate = event(doc), event(doc, evidence_id="signal:2")
    req = request([doc], agent_events=[first, duplicate], agent_coverage=[agent_coverage([doc])])
    result = analyze(req)
    assert result.e1[0].count == 1
    assert {e.signal_type: e.count for e in result.e2} == {
        "maintenance": 0, "functional_demand": 1, "migration": 0,
    }
    assert len([e for e in result.evidence if e.origin == "agent"]) == 1
    req.agent_events = []
    assert analyze(req).e1 == result.e1


def test_top_k_agent_processing_does_not_become_zero_events():
    doc = document()
    result = analyze(request([doc], agent_events=[event(doc)]))
    assert all(e.count is None and e.missing_reason == "agent_processing_incomplete"
               for e in result.e2)


def test_agent_coverage_needs_trusted_processing_verification():
    class NoProcessingProof(SyntheticVerifier):
        def agent_processing(self, coverage, documents):
            return False
    doc = document()
    result = analyze_external_context(
        request([doc], agent_coverage=[agent_coverage([doc])]), verifier=NoProcessingProof())
    assert all(e.count is None and e.missing_reason == "agent_processing_unverified"
               for e in result.e2)


@pytest.mark.parametrize("case", ["quote", "future", "unknown_source", "outcome", "uncertain"])
def test_bad_agent_events_rejected_without_changing_e1(case):
    doc = document()
    item = event(doc)
    if case == "quote":
        item.evidence.citations[0].quote = "x" * len(doc.article.content_text)
    elif case == "future":
        item.evidence.event_at = "2026-10-02T00:00:00.000Z"
    elif case == "unknown_source":
        item.evidence.citations[0].source_id = "source:missing"
    elif case == "outcome":
        item.outcome_disclosure = True
    else:
        item.evidence.uncertainty = "unresolved conflict"
    result = analyze(request([doc], agent_events=[item], agent_coverage=[agent_coverage([doc])]))
    assert result.rejected_agent_events
    assert not [e for e in result.evidence if e.origin == "agent"]
    assert result.e1[0].count == 1
    assert all(e.count is None for e in result.e2)


@pytest.mark.parametrize("scope", ["one", "reviewed_empty", "shared", "unknown"])
def test_rejected_events_only_invalidate_proven_census_scope(scope):
    old = document(2, date="2026-08-20T00:00:00.000Z")
    recent = document()
    previous = TimeRange(start_at="2026-08-02T00:00:00.000Z", end_at=WINDOW.start_at)
    censuses = [census([old], census_id="previous", window=previous), census([recent])]
    old_coverage = agent_coverage([old]).model_copy(update={
        "coverage_id": "agent:previous", "window": previous,
    })
    good = [] if scope == "reviewed_empty" else [event(recent)]
    req = request([old, recent], censuses, agent_events=good,
                  agent_coverage=[old_coverage, agent_coverage([recent])])
    baseline = analyze(req)
    bad = event(old, event_id="event:bad", evidence_id="signal:bad")
    bad.evidence.citations[0].quote = "x" * len(old.article.content_text)
    if scope == "shared":
        bad.evidence.citations.extend(event(recent).evidence.citations)
    elif scope == "unknown":
        bad.evidence.citations[0].source_id = "external:unknown"
    req.agent_events.append(bad)
    result = analyze(req)
    assert result.e1 == baseline.e1 and result.trends == baseline.trends
    assert all(item.count == 1 for item in result.e1)
    assert result.rejected_agent_events == {"signal:bad": (
        "agent_sources_ineligible" if scope == "unknown" else "agent_quote_or_context_mismatch"
    )}
    old_counts = [item for item in result.e2 if item.census_id == "previous"]
    recent_counts = [item for item in result.e2 if item.census_id == "recent"]
    assert all(item.count is None and item.missing_reason == "agent_events_rejected"
               for item in old_counts)
    if scope in {"shared", "unknown"}:
        assert all(item.count is None and item.missing_reason == "agent_events_rejected"
                   for item in recent_counts)
    else:
        assert recent_counts == [item for item in baseline.e2 if item.census_id == "recent"]
        assert {item.signal_type: item.count for item in recent_counts} == {
            "maintenance": 0, "functional_demand": 0 if scope == "reviewed_empty" else 1,
            "migration": 0,
        }
    assert result == analyze(req)


def test_conflicting_event_identity_fails_closed():
    doc = document()
    one, two = event(doc), event(doc, evidence_id="signal:2")
    two.evidence.claim = "Conflicting interpretation"
    with pytest.raises(ValueError, match="requires_review"):
        analyze(request([doc], agent_events=[one, two]))


def test_result_integrates_t021_without_mutating_prediction_or_training_fields():
    doc = document()
    doc.article.provenance.available_at = None
    doc.article.content_available_at = None
    result = analyze(request([doc]))
    report = json.loads((ROOT / "examples/analysis/final_report_core.json").read_text(encoding="utf-8"))
    original = copy.deepcopy(report)
    fields = result.report_fields()
    for key in ("sources", "coverage", "evidence"):
        report[key].extend(fields.pop(key))
    report.update(fields)
    validate_final_report(report)
    assert report["prediction"] == original["prediction"]
    assert report["core_prediction"] == original["core_prediction"]
    assert report["result_status"] == original["result_status"]


@pytest.mark.parametrize("empty", [False, True])
def test_verified_external_context_integrates_t021_enhanced_without_changing_scores(empty):
    docs = [] if empty else [document()]
    processing = agent_coverage(docs)
    if empty:
        processing.source_ids = ["census:recent"]
    req = request(docs, agent_events=[] if empty else [event(docs[0])],
                  agent_coverage=[processing])
    original = json.loads((ROOT / "examples/analysis/final_report_enhanced.json").read_text(encoding="utf-8"))
    core = json.loads((ROOT / "examples/analysis/final_report_core.json").read_text(encoding="utf-8"))
    assert original["core_prediction"] == core["prediction"]
    for verifier in (SyntheticVerifier(), None):
        result = analyze_external_context(req, verifier=verifier)
        report = copy.deepcopy(original)
        fields = result.report_fields()
        assert set(fields) == {
            "sources", "evidence", "coverage", "external_insights_status", "external_insights_reason",
        }
        report["sources"].extend(fields.pop("sources"))
        report["coverage"] = copy.deepcopy(core["coverage"]) + fields.pop("coverage")
        report.update(fields)
        report["prediction"]["feature_source_ids"] = (
            core["prediction"]["feature_source_ids"] + [source.source_id for source in result.sources])
        report["prediction"]["feature_evidence_ids"] = [
            item.evidence_id for item in result.evidence if item.use == "feature"]
        assert report["prediction"]["probability"] == original["prediction"]["probability"]
        assert report["core_prediction"] == original["core_prediction"]
        assert report["coverage"][:len(core["coverage"])] == core["coverage"]
        if verifier is None:
            assert all(item.count is None for item in result.e1)
            with pytest.raises(ValidationError, match="ENHANCED requires"):
                validate_final_report(report)
        else:
            validated = validate_final_report(report)
            assert validated.external_insights_status == "complete"
            assert result.external_insights_reason is None
            assert result.e1[0].count == (0 if empty else 1)
            assert validated.core_prediction.model_dump(mode="json") == original["core_prediction"]


def test_cli_reads_only_explicit_request_and_does_not_overwrite_output(tmp_path):
    input_path, output_path = tmp_path / "input.json", tmp_path / "output.json"
    data = request([document()]).model_dump_json()
    input_path.write_text(data, encoding="utf-8")
    command = [sys.executable, "-m", "scripts.analyze_external_context",
               "--input", str(input_path), "--output", str(output_path)]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output_path.read_text())["e1"][0]["count"] is None
    before = output_path.read_bytes()
    assert subprocess.run(command, cwd=ROOT, capture_output=True).returncode != 0
    assert output_path.read_bytes() == before
    assert input_path.read_text() == data


def test_candidate_or_unbound_json_is_not_document_evidence():
    with pytest.raises(ValidationError):
        ExternalContextRequest.model_validate(dict(
            package_id="pypi:synthetic-demo", cutoff_at=CUTOFF, generated_at=CUTOFF,
            documents=[{"candidate_url": "https://example.test/not-fetched"}],
        ))


def test_no_census_returns_explicit_unavailable_feature_scope():
    result = analyze(request([document()], []))
    assert result.e1 == result.e2 == []
    assert result.coverage[0].missing_reason == "source_census_missing"
    assert result.external_insights_status == "partial"



def test_empty_request_without_census_preserves_unknown_counts():
    result = analyze_external_context(request([], []))
    assert result.external_insights_status == "unavailable"
    assert result.coverage[0].records_observed is None
    assert result.coverage[0].eligible_documents is None
    assert result.coverage[0].processed_documents is None


def test_late_syndication_bridge_merges_previously_separate_groups():
    first = document(1, text="First synthetic version.")
    second = document(2, text="Second synthetic version.")
    bridge = document(3, text=first.article.content_text)
    second.origin_id = bridge.origin_id = "original:bridge"
    assert analyze(request([first, second, bridge])).e1[0].count == 1


def test_complete_empty_agent_processing_requires_census_source_lineage():
    coverage = agent_coverage([])
    coverage.source_ids = ["census:recent"]
    result = analyze(request([], agent_coverage=[coverage]))
    assert all(item.count == 0 for item in result.e2)
    assert result.coverage[-1].source_ids == ["census:recent"]


def test_committed_current_capture_example_is_reproducible():
    directory = ROOT / "examples/external_context"
    req = ExternalContextRequest.model_validate_json((directory / "current_capture_request.json").read_bytes())
    expected = json.loads((directory / "current_capture_result.json").read_bytes())
    assert analyze_external_context(req).model_dump(mode="json") == expected
