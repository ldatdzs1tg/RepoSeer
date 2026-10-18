"""T-018 deterministic contract, observation history and manually labeled relevance cases."""

import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import polars as pl
import pytest
from pydantic import ValidationError

from reposeer.contracts import (
    NormalizationRequest,
    RawRecord,
    validate_normalization_pair,
    validate_record,
)
from reposeer.contracts.adapters import GitHubRepositoryContractNormalizer
from reposeer.contracts.identity import article_id, normalize_article_text, text_sha256
from reposeer.pipelines.normalization import NormalizationPipeline, NormalizationPipelineResult
from reposeer.processing.normalize.external import ExternalDocumentNormalizer, ExternalNormalizer
from reposeer.processing.normalize.github import GitHubActivityNormalizer, GitHubNormalizer
from reposeer.processing.relevance import (
    ExternalRelevanceEvaluator,
    RelevanceAssessment,
    VerifiedTargetContext,
    validate_assessment_evidence,
)
from reposeer.technology.functional_profile import (
    EvidenceSpan,
    FunctionalClaim,
    FunctionalDraft,
    FunctionalProfileResult,
    ground_claims,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-10-18T10:00:00.000Z"
REPO = "github:repository:900000001"


def request_for(records):
    return NormalizationRequest(schema_version="0.1.0", record_type="normalization_request",
                                request_id=str(uuid4()), run_id=records[0].run_id, records=records)


def repository_raw(**changes):
    value = json.loads((ROOT / "examples/contracts/github_raw_record.json").read_text(encoding="utf-8"))
    value.update(changes)
    return validate_record(value)


def document_raw(text="Flask is a Python framework for web applications.", **changes):
    text = normalize_article_text(text)
    html = changes.pop("html", "<html><body><article>" + text + "</article></body></html>")
    encoded = html.encode("utf-8")
    payload = dict(
        record_stage="external_document", crawler_version="0.3.0", source="web",
        source_url="http://blog.example/Original?utm_source=hn", url="https://blog.example/Final/",
        fetched_url="https://blog.example/Final/", fetched_at=NOW, source_item_id=None,
        candidate_raw_record_id=None, discovery=None, response_body_base64=base64.b64encode(encoded).decode(),
        content_type="text/html; charset=utf-8", etag=None, last_modified=None, native_payload=None,
        html=html, title="Synthetic article", author=None, published_at=None,
        published_date="2024-03-01", published_precision="date", updated_at=None,
        content=text, content_status="full", content_sha256=text_sha256(text), word_count=len(text.split()),
        content_available_at=NOW, historical_version_status="observation_only", relevance_status="unverified",
        availability_basis="observed_response", discovery_references=[], discovery_lineage_complete=False,
    )
    payload.update(changes)
    return RawRecord(schema_version="0.1.0", record_type="raw_record", raw_record_id="raw:"+str(uuid4()),
                     run_id=str(uuid4()), source="web_article", source_record_type="article", source_record_id=None,
                     repo_id=None, package_id=None, source_url=payload["source_url"], retrieved_at=payload["fetched_at"],
                     event_at=payload["published_at"], available_at=payload["fetched_at"], http_status=200,
                     raw_body_sha256=hashlib.sha256(encoded).hexdigest(), payload=payload)


def activity_raw(group, **changes):
    user = {"id": 42, "login": "fixture"}
    payloads = {
        "commits": dict(sha="a"*40, commit={"committer": {"date": "2024-01-01T00:00:00Z"}}, author=user),
        "issues": dict(id=1000, number=1, title="Issue", state="closed", created_at="2024-01-01T00:00:00Z", user=user),
        "pull_requests": dict(id=2000, number=1, title="PR", state="closed", created_at="2024-01-01T00:00:00Z", user=user, merged=False),
        "contributors": dict(id=42, login="fixture", type="User", contributions=5),
        "releases": dict(id=3000, tag_name="v1", draft=False, prerelease=False),
    }
    payload = payloads[group]
    payload.update(changes)
    endpoint = "pulls" if group == "pull_requests" else group
    source_id = {"commits": "commit:"+"a"*40, "issues": "issue:1000", "pull_requests": "pull_request:2000",
                 "contributors": "contributor:42", "releases": "release:3000"}[group]
    return repository_raw(raw_record_id="raw:"+str(uuid4()), source_record_type="repository_activity", source_record_id=source_id,
                          source_url=f"https://api.github.com/repos/Synthetic/Demo/{endpoint}",
                          event_at="2024-01-01T00:00:00.000Z" if group in {"commits", "issues", "pull_requests"} else None,
                          payload=payload)


def target(**changes):
    values = dict(package_id="pypi:flask", repo_id=REPO, verified_repository_url="https://github.com/pallets/flask",
                  verified_package_url="https://pypi.org/project/flask/", verified_aliases=["Flask"],
                  mapping_evidence_refs=["fixture:pypi-source-and-numeric-github-id"])
    values.update(changes)
    return VerifiedTargetContext(**values)


def profile():
    text = "Flask helps users build web applications"
    span = EvidenceSpan(span_id="span:official", source_id="pypi-description", source_path="info.description",
                        source_url="https://pypi.org/pypi/flask/1.0/json", source_revision="1.0",
                        start=0, end=len(text), text=text, content_sha256=text_sha256(text))
    claim = FunctionalClaim(text="build web applications", evidence_span_ids=[span.span_id])
    draft = FunctionalDraft(primary_function=claim, solved_problems=[], capabilities=[], use_cases=[claim],
                            functional_category=None, synonyms=[], alternative_candidates=[])
    grounded, refs, _ = ground_claims(draft, [span])
    return FunctionalProfileResult(status="verified", package_id="pypi:flask", repo_id=REPO,
                                   scope_quality="target_package", evidence_refs=refs,
                                   source_revision_keys=["pypi-description:1.0"], **grounded)


def test_repository_facade_delegates_and_rejects_wrong_numeric_identity(monkeypatch):
    raw = repository_raw()
    request = request_for([raw])
    existing = GitHubRepositoryContractNormalizer.normalize
    calls = []

    def delegated(self, value):
        calls.append(value)
        return existing(self, value)

    monkeypatch.setattr(GitHubRepositoryContractNormalizer, "normalize", delegated)
    result = GitHubNormalizer().normalize(request)
    assert calls == [request]
    assert result.records[0].identity.repo_id == REPO
    assert result.records[0].provenance.temporal_basis == "observation"
    invalid = raw.model_copy(update={"repo_id": "github:repository:2"})
    assert GitHubNormalizer().normalize(request_for([invalid])).rejected_record_count == 1


@pytest.mark.parametrize("group,kinds,outcome", [
    ("commits", ["commit"], "canonical_events"),
    ("issues", ["issue_opened"], "canonical_events"),
    ("pull_requests", ["pr_opened"], "canonical_events"),
    ("contributors", [], "contributor_aggregate"),
    ("releases", [], "github_release"),
])
def test_activity_groups_keep_internal_and_canonical_meanings(group, kinds, outcome):
    raw = activity_raw(group)
    before = raw.model_dump_json()
    request = request_for([raw])
    output = GitHubActivityNormalizer().normalize_with_entities(request)
    validate_normalization_pair(request, output.canonical_result)
    assert [record.activity_kind for record in output.canonical_result.records] == kinds
    assert output.internal_entities[0].outcome_code == outcome
    assert output.internal_entities[0].raw_record_id == raw.raw_record_id
    assert all(record.provenance.available_at is None and record.actor_is_bot is None
               for record in output.canonical_result.records)
    assert raw.model_dump_json() == before


@pytest.mark.parametrize("group,changes,expected", [
    ("issues", {"closed_at": "2024-01-02T00:00:00Z"}, ["issue_opened", "issue_closed"]),
    ("issues", {"state": "open"}, ["issue_opened"]),
    ("pull_requests", {"merged": True, "merged_at": "2024-01-03T00:00:00Z",
                       "closed_at": "2024-01-02T00:00:00Z"}, ["pr_opened", "pr_merged"]),
    ("pull_requests", {"closed_at": "2024-01-02T00:00:00Z"}, ["pr_opened", "pr_closed"]),
    ("pull_requests", {"merged": None, "closed_at": "2024-01-02T00:00:00Z"}, ["pr_opened"]),
])
def test_activity_events_require_explicit_timestamps_and_merge_evidence(group, changes, expected):
    output = GitHubActivityNormalizer().normalize(request_for([activity_raw(group, **changes)]))
    assert [record.activity_kind for record in output.records] == expected
    assert output.processed_record_count == 1
    assert all(record.source_object_id in {"1000", "2000"} for record in output.records)


@pytest.mark.parametrize("mutation", ["null_pr", "source_id", "endpoint", "repository_id", "merge_contradiction"])
def test_activity_rejects_misclassified_or_inconsistent_native_identity(mutation):
    raw = activity_raw("issues")
    if mutation == "null_pr":
        raw.payload["pull_request"] = None
    elif mutation == "source_id":
        raw.source_record_id = "issue:999"
    elif mutation == "endpoint":
        raw.source_url = raw.source_url.replace("/issues", "/pulls")
    elif mutation == "repository_id":
        raw.payload["repository"] = {"id": 999}
    else:
        raw = activity_raw("pull_requests", merged=True)
    output = GitHubActivityNormalizer().normalize_with_entities(request_for([raw]))
    assert output.canonical_result.status == "failed"
    assert not output.internal_entities
    assert output.canonical_result.errors[0].raw_record_id == raw.raw_record_id


def test_commit_without_timestamp_is_internal_only_and_not_a_fabricated_event():
    raw = activity_raw("commits", commit={})
    raw.event_at = None
    result = NormalizationPipeline().process([raw])
    assert not result.canonical_activities
    assert result.internal_activity_entities[0].outcome_code == "event_time_unknown"
    assert result.processing_summary.internal_only_observation_count == 1
    assert result.processing_summary.rejected_observation_count == 0


def test_external_contract_mapping_url_unicode_date_and_no_entity_assignment():
    raw = document_raw("  Cafe\u0301\r\nSecond paragraph!  ")
    request = request_for([raw])
    result = ExternalDocumentNormalizer().normalize(request)
    validate_normalization_pair(request, result)
    article = result.records[0]
    assert article.canonical_url == "https://blog.example/Final/"
    assert article.original_url == "http://blog.example/Original?utm_source=hn"
    assert article.article_id == article_id(article.canonical_url)
    assert article.content_text == "Caf\u00e9\nSecond paragraph!"
    assert article.content_sha256 == text_sha256(article.content_text)
    assert article.published_at is None and article.published_date == "2024-03-01"
    assert article.authors is article.language_code is None
    assert article.package_ids == article.repo_ids == article.technology_mentions == []
    assert article.content_available_at == article.provenance.available_at == NOW
    assert article.provenance.raw_record_ids == [raw.raw_record_id]


@pytest.mark.parametrize("url,expected", [
    ("HTTP://Example.COM/Case/?id=2&utm_source=hn", "http://example.com/Case/?id=2"),
    ("https://example.com/Case", "https://example.com/Case"),
    ("https://example.com/case/", "https://example.com/case/"),
    ("https://example.com/?a=1&b=2&fbclid=x", "https://example.com/?a=1&b=2"),
])
def test_legacy_url_dataframe_uses_canonical_policy(url, expected):
    frame = ExternalNormalizer().normalize_urls(pl.DataFrame({"url": [url, None]}))
    assert frame["url"].to_list() == [expected, None]


@pytest.mark.parametrize("mutation,reason", [("title", "article_title_missing"), ("hash", "invalid_external_document"),
                                               ("discovery", "invalid_external_document"), ("future", "invalid_external_document")])
def test_external_malformed_capture_is_rejected_without_fabrication(mutation, reason):
    raw = document_raw()
    if mutation == "title":
        raw.payload["title"] = None
    elif mutation == "hash":
        raw.payload["content_sha256"] = "f"*64
    elif mutation == "discovery":
        raw.payload["record_stage"] = "discovery_candidate"
    else:
        raw.payload["content_available_at"] = "2027-01-01T00:00:00.000Z"
    result = ExternalDocumentNormalizer().normalize(request_for([raw]))
    assert result.status == "failed" and result.rejected_record_count == 1
    assert result.errors[0].message == reason


def test_unavailable_content_and_historical_diagnostics_remain_unknown():
    raw = document_raw(content=None, content_status="unavailable", content_sha256=None,
                       word_count=None, content_available_at=None, historical_version_status="unverified")
    result = NormalizationPipeline().process([raw], targets=[target()], cutoff_at="2024-12-31T00:00:00.000Z")
    article = result.canonical_articles[0]
    assert article.content_text is article.content_sha256 is article.content_available_at is None
    assert result.temporal_diagnostics[raw.raw_record_id] == "availability_unknown"
    assert result.relevance_assessments[0].decision == "unresolved"
    current = document_raw()
    assert NormalizationPipeline().process([current], cutoff_at="2024-12-31T00:00:00.000Z").temporal_diagnostics[current.raw_record_id] == "observed_after_cutoff"


# Expected labels are manual, based on the stated substantive evidence, not evaluator output.
RELEVANCE_CASES = [
    ("direct", "Flask is a Python framework with installation instructions.", False, "relevant", "package_specific", "exact alias and technical prose"),
    ("query_noise", "The hiking trail passes a lake.", False, "irrelevant", None, "no package or technical content"),
    ("collision", "Carry water in a flask during the hike.", False, "unresolved", None, "ambiguous ordinary noun"),
    ("technology", "Python is a widely used programming language.", False, "irrelevant", None, "generic technology is not target evidence"),
    ("framework", "A framework is a reusable software library.", False, "irrelevant", None, "generic framework is not target evidence"),
    ("function", "This tool helps users build web applications.", True, "relevant", "function_level", "exact official use case"),
    ("no_profile", "This tool helps users build web applications.", False, "unresolved", None, "use case needs profile proof"),
    ("migration", "Migrate from the Flask framework to another tool to build web applications.", True, "relevant", "alternative_level", "explicit migration in verified scope"),
    ("names_only", "Flask and Django appear in the index.", False, "unresolved", None, "names alone do not prove alternatives"),
]


@pytest.mark.parametrize("case,text,with_profile,decision,level,justification", RELEVANCE_CASES)
def test_manually_labeled_relevance_cases(case, text, with_profile, decision, level, justification):
    assert justification
    raw = document_raw(text)
    # Discovery intent must not change the label, even when it suggests an exact package query.
    raw.payload["discovery"] = {"query_intent": "alternatives", "query": "Flask"}
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    context = target(optional_functional_profile=profile() if with_profile else None)
    result = ExternalRelevanceEvaluator().evaluate(article, context, raw=raw)
    assert (result.decision, result.context_level) == (decision, level), case
    for span in result.evidence_spans:
        assert article.content_text[span.start_offset:span.end_offset] == span.text
    assert article.package_ids == article.repo_ids == []


def test_footer_link_and_unverified_repository_mapping_are_not_positive_evidence():
    raw = document_raw("The hiking trail passes a lake.", html='<article>The hiking trail passes a lake.</article><footer><a href="https://github.com/pallets/flask">Flask</a></footer>')
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    result = ExternalRelevanceEvaluator().evaluate(article, target(), raw=raw)
    assert result.decision == "irrelevant" and result.context_level is None
    assert result.link_evidence[0].location == "reference"
    with pytest.raises(ValidationError, match="repository_matching_requires"):
        target(mapping_evidence_refs=[])


def test_same_article_separate_targets_and_strict_evidence_validation():
    raw = document_raw()
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    first = ExternalRelevanceEvaluator().evaluate(article, target())
    other = target(package_id="pypi:other", repo_id=None, verified_repository_url=None,
                   verified_package_url="https://pypi.org/project/other/", verified_aliases=["Other"])
    second = ExternalRelevanceEvaluator().evaluate(article, other)
    assert first.context_level == "package_specific" and second.context_level != "package_specific"
    value = first.model_dump()
    value["evidence_spans"][0]["text"] = "x" * len(value["evidence_spans"][0]["text"])
    restored = RelevanceAssessment.model_validate(value)
    with pytest.raises(ValueError, match="assessment_evidence_offset_mismatch"):
        validate_assessment_evidence(restored, article)
    value = first.model_dump()
    value["raw_record_ids"] *= 2
    with pytest.raises(ValidationError, match="duplicate_raw"):
        RelevanceAssessment.model_validate(value)


@pytest.mark.parametrize("mutation", ["repo_only", "foreign", "ungrounded"])
def test_functional_matching_rejects_unusable_profile(mutation):
    verified = profile()
    if mutation == "repo_only":
        verified = verified.model_copy(update={"scope_quality": "repo_only"})
    elif mutation == "foreign":
        verified = verified.model_copy(update={"package_id": "pypi:other"})
    else:
        verified = verified.model_copy(update={"primary_function": FunctionalClaim(text="Invented claim", evidence_span_ids=["missing"])})
    raw = document_raw("This tool helps users build web applications.")
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    assert ExternalRelevanceEvaluator().evaluate(article, target(optional_functional_profile=verified)).decision == "unresolved"


def test_observation_history_revisions_cross_url_duplicates_and_determinism():
    first = document_raw()
    second = document_raw()
    third = document_raw("Flask is a Python framework with a changed API.")
    fourth = document_raw(fetched_url="https://mirror.example/Copy", url="https://mirror.example/Copy")
    second.raw_record_id = "raw:00000000-0000-4000-8000-000000000001"
    inputs = [first, second, third, fourth]
    before = [raw.model_dump_json() for raw in inputs]
    pipeline = NormalizationPipeline()
    result = pipeline.process(inputs, targets=[target()])
    group = next(group for group in result.deduplicated_document_view if group.canonical_url == "https://blog.example/Final/")
    assert len(group.raw_record_ids) == 3 and len(group.distinct_content_hashes) == 2
    assert group.primary_observation_raw_id == second.raw_record_id
    assert result.processing_summary.content_revisions == 3
    assert len(result.content_duplicates) == 1 and len(result.content_duplicates[0].article_ids) == 2
    assert all(len(article.provenance.raw_record_ids) == 1 for article in result.canonical_articles)
    assert result.model_dump() == pipeline.process(reversed(inputs), targets=[target()]).model_dump()
    assert [raw.model_dump_json() for raw in inputs] == before
    repeated = pipeline.process([first, first])
    assert repeated.processing_summary.duplicate_input_count == 1
    changed = first.model_copy(deep=True)
    changed.payload["title"] = "Conflicting observation"
    with pytest.raises(ValueError, match="conflicting_raw_record_id"):
        pipeline.process([first, changed])


@pytest.mark.parametrize("alias,content", [
    ("Flask", "The drinking flask has a Python logo."),
    ("Requests", "HTTP requests are sent using Python."),
])
def test_technical_words_do_not_disambiguate_an_ordinary_name(alias, content):
    raw = document_raw(content)
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    context = target(package_id="pypi:"+alias.lower(), verified_aliases=[alias], repo_id=None,
                     verified_repository_url=None, verified_package_url=None)
    result = ExternalRelevanceEvaluator().evaluate(article, context)
    assert (result.decision, result.context_level) == ("unresolved", None)


@pytest.mark.parametrize("as_html_link", [False, True])
def test_exact_verified_url_needs_substantive_body_context(as_html_link):
    text = "This Python library validates user input through the documented API."
    if as_html_link:
        html = '<article>This Python <a href="https://pypi.org/project/flask/">library</a> validates user input through the documented API.</article>'
        raw = document_raw(text, html=html)
    else:
        raw = document_raw(text + " See https://pypi.org/project/flask/ for installation.")
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    result = ExternalRelevanceEvaluator().evaluate(article, target(), raw=raw)
    assert result.context_level == "package_specific" and result.evidence_spans


def test_github_dedup_keeps_numeric_identity_renames_state_history_and_observation_times():
    first = repository_raw()
    renamed = first.model_copy(deep=True)
    renamed.raw_record_id = "raw:" + str(uuid4())
    renamed.run_id = str(uuid4())
    renamed.payload.update(full_name="NewOwner/Renamed", stargazers_count=99)
    renamed.retrieved_at = "2024-03-01T12:00:00.000Z"
    another = first.model_copy(deep=True)
    another.raw_record_id = "raw:" + str(uuid4())
    another.repo_id = "github:repository:999"
    another.source_record_id = "999"
    another.payload["id"] = 999
    contributor = activity_raw("contributors")
    later = contributor.model_copy(deep=True)
    later.raw_record_id = "raw:" + str(uuid4())
    later.retrieved_at = "2024-03-01T12:00:00.000Z"
    result = NormalizationPipeline().process([first, renamed, another, contributor, later])
    assert len(result.deduplicated_repository_view) == 2
    group = next(group for group in result.deduplicated_repository_view if group.logical_id == REPO)
    assert len(group.raw_record_ids) == len(group.distinct_state_hashes) == 2
    assert group.primary_observation_raw_id == renamed.raw_record_id
    assert len(result.deduplicated_activity_view) == 2


def test_full_content_primary_precedes_newer_unavailable_and_old_content_is_not_backdated():
    full = document_raw()
    unavailable = document_raw(content=None, content_status="unavailable", content_sha256=None,
                               word_count=None, content_available_at=None, historical_version_status="unverified",
                               fetched_at="2026-10-19T10:00:00.000Z")
    result = NormalizationPipeline().process([unavailable, full])
    assert result.deduplicated_document_view[0].primary_observation_raw_id == full.raw_record_id
    assert result.processing_summary.content_revisions == 1


def test_strict_relevance_rejects_unsupported_decision_and_no_positive_evidence():
    raw = document_raw()
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    value = ExternalRelevanceEvaluator().evaluate(article, target()).model_dump()
    value["decision"] = "probably"
    with pytest.raises(ValidationError):
        RelevanceAssessment.model_validate(value)
    value["decision"] = "relevant"
    value["evidence_spans"] = []
    with pytest.raises(ValidationError, match="substantive_content_evidence"):
        RelevanceAssessment.model_validate(value)


@pytest.mark.parametrize("text,decision", [
    ("Flask is a Python framework with documented usage.", "relevant"),
    ("Python is a widely used programming language.", "irrelevant"),
    ("Carry water in a flask on the hike.", "unresolved"),
])
def test_assessment_and_pipeline_json_round_trip_revalidate_evidence(text, decision):
    result = NormalizationPipeline().process([document_raw(text)], targets=[target()])
    article = result.canonical_articles[0]
    assessment = result.relevance_assessments[0]
    assert assessment.decision == decision
    serialized = json.loads(assessment.model_dump_json())
    restored = RelevanceAssessment.model_validate(serialized)
    validate_assessment_evidence(restored, article)
    assert restored.model_dump(mode="json") == serialized
    payload = json.loads(result.model_dump_json())
    assert NormalizationPipelineResult.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize("mutation,reason", [
    ("article_id", "assessment_article_identity_mismatch"),
    ("revision", "assessment_content_revision_mismatch"),
    ("raw_id", "assessment_raw_reference_mismatch"),
    ("text", "assessment_evidence_offset_mismatch"),
    ("bounds", "assessment_evidence_offset_mismatch"),
])
def test_explicit_evidence_verifier_rejects_structurally_valid_tampering(mutation, reason):
    result = NormalizationPipeline().process([document_raw()], targets=[target()])
    article, assessment = result.canonical_articles[0], result.relevance_assessments[0]
    before_article, before_assessment = article.model_dump_json(), assessment.model_dump_json()
    payload = assessment.model_dump(mode="json")
    if mutation == "article_id":
        payload["article_id"] = article_id("https://unrelated.example/")
    elif mutation == "revision":
        changed = document_raw("Flask is a Python framework with a different API.")
        payload["content_sha256"] = changed.payload["content_sha256"]
    elif mutation == "raw_id":
        payload["raw_record_ids"] = ["raw:" + str(uuid4())]
    elif mutation == "text":
        payload["evidence_spans"][0]["text"] = "x" * len(payload["evidence_spans"][0]["text"])
    else:
        payload["evidence_spans"][0]["start_offset"] += len(article.content_text)
        payload["evidence_spans"][0]["end_offset"] += len(article.content_text)
    restored = RelevanceAssessment.model_validate(payload)
    with pytest.raises(ValueError, match=reason):
        validate_assessment_evidence(restored, article)
    assert article.model_dump_json() == before_article
    assert assessment.model_dump_json() == before_assessment


@pytest.mark.parametrize("mutation,reason", [
    ("text", "assessment_evidence_offset_mismatch"),
    ("offset", "assessment_evidence_offset_mismatch"),
    ("revision", "assessment_content_revision_mismatch"),
    ("raw_id", "assessment_raw_reference_mismatch"),
    ("orphan", "assessment_matching_article_missing"),
    ("conflicting_article", "assessment_article_observation_conflict"),
])
def test_pipeline_reload_matches_exact_observation_and_revision(mutation, reason):
    first, second = document_raw(), document_raw("Flask is a Python framework with a revised API.")
    result = NormalizationPipeline().process([first, second], targets=[target()])
    assert len({a.article_id for a in result.canonical_articles}) == 1
    assert len({a.content_sha256 for a in result.canonical_articles}) == 2
    payload = result.model_dump(mode="json")
    assessment = payload["relevance_assessments"][0]
    if mutation == "text":
        assessment["evidence_spans"][0]["text"] = "x" * len(assessment["evidence_spans"][0]["text"])
    elif mutation == "offset":
        assessment["evidence_spans"][0]["start_offset"] += 1
        assessment["evidence_spans"][0]["end_offset"] += 1
    elif mutation == "revision":
        assessment["content_sha256"] = payload["relevance_assessments"][1]["content_sha256"]
    elif mutation == "raw_id":
        assessment["raw_record_ids"] = ["raw:" + str(uuid4())]
    elif mutation == "orphan":
        raw_id = assessment["raw_record_ids"][0]
        payload["canonical_normalization_results"] = [batch for batch in payload["canonical_normalization_results"]
            if not any(raw_id in article["provenance"]["raw_record_ids"] for article in batch["records"])]
    else:
        batch = payload["canonical_normalization_results"][0]
        extra = json.loads(json.dumps(batch["records"][0]))
        extra["title"] = "Conflicting data for one observation key"
        batch["records"].append(extra)
        batch["output_record_count"] += 1
    with pytest.raises(ValidationError, match=reason):
        NormalizationPipelineResult.model_validate(payload)


@pytest.mark.parametrize("group", ["contributors", "releases", "commits"])
def test_internal_only_is_accepted_without_processing_failure(group):
    raw = activity_raw(group)
    if group == "commits":
        raw.payload["commit"] = {}
        raw.event_at = None
    result = NormalizationPipeline().process([raw])
    assert result.processing_summary.internal_only_observation_count == 1
    assert result.processing_summary.accepted_observation_count == 1
    assert result.processing_summary.rejected_observation_count == 0
    assert result.processing_summary.canonical_rejected_observation_count == 1
    assert result.processing_errors == []
    canonical = result.canonical_normalization_results[0]
    assert canonical.errors[0].code == "unsupported_record_type"
    request = NormalizationRequest(schema_version="0.1.0", record_type="normalization_request",
                                   request_id=canonical.request_id, run_id=raw.run_id, records=[raw])
    validate_normalization_pair(request, canonical)


def test_mixed_internal_only_and_real_failures_are_accounted_separately():
    commit = activity_raw("commits")
    contributor, release = activity_raw("contributors"), activity_raw("releases")
    invalid = activity_raw("issues", pull_request=None)
    unsupported = repository_raw(raw_record_id="raw:" + str(uuid4()), source="github_graphql")
    result = NormalizationPipeline().process([commit, contributor, release, invalid, unsupported])
    summary = result.processing_summary
    assert summary.input_record_count == 5
    assert summary.accepted_observation_count == 3 and summary.rejected_observation_count == 2
    assert summary.internal_only_observation_count == 2
    assert summary.canonical_rejected_observation_count == 4
    assert len(result.canonical_activities) == 1
    assert {(error.raw_record_id, error.code) for error in result.processing_errors} == {
        (invalid.raw_record_id, "validation_error"), (unsupported.raw_record_id, "unsupported_record_type")}
    assert sum(len(batch.errors) for batch in result.canonical_normalization_results) == 4


@pytest.mark.parametrize("case,text,html,with_profile,decision,level,reason", [
    ("inline_reference_url",
     "This article discusses hiking safety.\n\nReferences: Learn about this Python framework at https://github.com/pallets/flask for more information.",
     None, False, "irrelevant", None, "reference_only_link"),
    ("multiline_references",
     "This article discusses hiking safety.\n\n## References\n\nFlask is a Python web framework.\nhttps://github.com/pallets/flask",
     None, False, "irrelevant", None, "reference_only_link"),
    ("retained_footer",
     "This article discusses hiking safety.\n\nFooter:\nFlask is a Python framework.\nhttps://github.com/pallets/flask",
     '<article>This article discusses hiking safety.</article><footer><p>Flask is a Python framework.</p><a href="https://github.com/pallets/flask">https://github.com/pallets/flask</a></footer>',
     False, "irrelevant", None, "reference_only_link"),
    ("html_bibliography",
     "This article discusses hiking safety.\n\nFlask is a Python framework.",
     '<article>This article discusses hiking safety.</article><section class="references"><p><a href="https://github.com/pallets/flask">Flask</a> is a Python framework.</p></section>',
     False, "irrelevant", None, "reference_only_link"),
    ("html_footer_alias_without_link",
     "This article discusses hiking safety.\n\nFlask is a Python framework.",
     '<article>This article discusses hiking safety.</article><footer><p>Flask is a Python framework.</p></footer>',
     False, "irrelevant", None, "reference_only_mention"),
    ("body_url",
     "This tutorial explains the Flask Python web framework.\n\nTo use Flask, see https://github.com/pallets/flask and follow the installation instructions.",
     None, False, "relevant", "package_specific", "verified_entity_in_substantive_content"),
    ("bare_body_link",
     "Python.",
     '<article><p><a href="https://github.com/pallets/flask">Python</a>.</p></article>',
     False, "unresolved", None, "reference_context_ambiguous"),
    ("body_and_reference",
     "Flask is a Python web framework for creating web APIs.\n\n## References\nhttps://github.com/pallets/flask",
     None, False, "relevant", "package_specific", "verified_entity_in_substantive_content"),
    ("duplicate_url_occurrences",
     "Use the Flask framework to build a Python application.\nOfficial source: https://github.com/pallets/flask\n\nReferences:\nhttps://github.com/pallets/flask",
     '<article><p>Use the <a href="https://github.com/pallets/flask">Flask framework</a> to build a Python application.</p><p>Official source: https://github.com/pallets/flask</p></article><footer><a href="https://github.com/pallets/flask">https://github.com/pallets/flask</a></footer>',
     False, "relevant", "package_specific", "verified_entity_in_substantive_content"),
    ("alias_only_bibliography",
     "This document compares hiking equipment.\n\nBibliography:\nFlask is a Python framework.",
     None, False, "irrelevant", None, "reference_only_mention"),
    ("heading_boundary",
     "## References\nFlask documentation: https://github.com/pallets/flask\n\n## Implementation\nWe use the Flask framework to build a Python API.",
     None, False, "relevant", "package_specific", "verified_entity_in_substantive_content"),
    ("ordinary_prose_references",
     "This tutorial references Flask while explaining the routing API of the Flask Python framework.",
     None, False, "relevant", "package_specific", "verified_entity_in_substantive_content"),
    ("reference_only_function",
     "This article discusses hiking safety.\n\n## Further Reading\nThis tool helps users build web applications.",
     None, True, "irrelevant", None, "reference_only_mention"),
    ("reference_only_migration",
     "This article discusses hiking safety.\n\nSources:\nMigrate from the Flask framework to another tool to build web applications.",
     None, True, "irrelevant", None, "reference_only_mention"),
    ("body_function_with_reference",
     "This tool helps users build web applications.\n\n## References\nFlask is a Python framework.",
     None, True, "relevant", "function_level", "verified_use_case_match"),
    ("body_migration_with_reference",
     "Migrate from the Flask framework to another tool to build web applications.\n\n## References\nFlask is a Python framework.",
     None, True, "relevant", "alternative_level", "explicit_comparison_with_verified_function"),
    ("ambiguous_html_occurrence",
     "Flask is a Python framework.",
     '<article><p>Flask is a Python framework.</p></article><footer><p>Flask is a Python framework.</p></footer>',
     False, "unresolved", None, "reference_context_ambiguous"),
    ("unmapped_footer_alias",
     "Flask is a Python framework.",
     '<article>This article discusses hiking safety.</article><footer><p><a href="https://github.com/pallets/flask">Flask</a>is a Python framework.</p></footer>',
     False, "unresolved", None, "reference_context_ambiguous"),
    ("unmapped_footer_function",
     "This tool helps users build web applications.",
     '<article>This article discusses hiking safety.</article><footer><p>This tool helps users <b>build</b>web applications.</p></footer>',
     True, "unresolved", None, "reference_context_ambiguous"),
    ("identical_body_and_footer_occurrences",
     "Flask is a Python framework.\n\nFlask is a Python framework.",
     '<article><p>Flask is a Python framework.</p></article><footer><p>Flask is a Python framework.</p></footer>',
     False, "relevant", "package_specific", "verified_entity_in_substantive_content"),
])
def test_reference_occurrences_require_substantive_evidence(case, text, html, with_profile,
                                                          decision, level, reason):
    raw = document_raw(text, **({"html": html} if html is not None else {}))
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    raw_before, article_before = raw.model_dump_json(), article.model_dump_json()
    context = target(optional_functional_profile=profile() if with_profile else None)
    result = ExternalRelevanceEvaluator().evaluate(article, context, raw=raw if html is not None else None)
    assert (result.decision, result.context_level, result.reason_code) == (decision, level, reason), case
    validate_assessment_evidence(result, article)
    if decision != "relevant":
        assert result.evidence_spans == []
    if case in {"body_and_reference", "body_function_with_reference", "body_migration_with_reference", "duplicate_url_occurrences"}:
        boundary = text.index("References")
        assert all(span.end_offset <= boundary for span in result.evidence_spans)
    if case == "heading_boundary":
        assert all(span.start_offset > text.index("## Implementation") for span in result.evidence_spans)
    if case == "identical_body_and_footer_occurrences":
        assert all(span.end_offset <= text.index("\n\n") for span in result.evidence_spans)
    if case in {"retained_footer", "html_bibliography"}:
        assert result.link_evidence and all(link.location == "reference" for link in result.link_evidence)
    if case == "duplicate_url_occurrences":
        assert [link.location for link in result.link_evidence] == ["content", "reference"]
    pipeline_result = NormalizationPipeline().process([raw], targets=[context])
    assert pipeline_result.relevance_assessments[0].decision == decision
    payload = json.loads(pipeline_result.model_dump_json())
    assert NormalizationPipelineResult.model_validate(payload).model_dump(mode="json") == payload
    assert raw.model_dump_json() == raw_before and article.model_dump_json() == article_before


@pytest.mark.parametrize("heading", ["References", "References:", "## References", "Bibliography",
                                     "Bibliography:", "Further Reading", "Related Links", "Sources", "### Further Reading"])
def test_dedicated_reference_heading_variants(heading):
    raw = document_raw("This article discusses hiking safety.\n\n"+heading+"\nFlask is a Python framework.")
    article = ExternalDocumentNormalizer().normalize(request_for([raw])).records[0]
    result = ExternalRelevanceEvaluator().evaluate(article, target())
    assert (result.decision, result.context_level, result.reason_code) == ("irrelevant", None, "reference_only_mention")
