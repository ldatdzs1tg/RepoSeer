"""Offline D03: source/version preflight and strict extractive evidence, never stack guesses."""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from types import SimpleNamespace

import pytest

from reposeer.contracts.identity import text_sha256
from reposeer.contracts.models import RepoIdentity
from reposeer.technology.functional_profile import (
    EvidenceRef,
    EvidenceSpan,
    FunctionalDraft,
    FunctionalSource,
    SourceVerificationError,
    ground_claims,
    prepare_passages,
    profile_cache_key,
)
from reposeer.technology.functional_profiler import (
    FunctionalLLMError,
    FunctionalProfiler,
    OpenAIResponsesFunctionalLLM,
)

PACKAGE = "pypi:demo"
REPO = "github:repository:900000001"
TEXT = "# Demo\n\n![badge](https://shields.io/demo)\n\n## Overview\n\nDemo validates user-submitted documents.\n\n## Features\n\nExports validation errors as structured reports.\n\n## Dependencies\n\nPython requests click rich\n"


def source(**changes):
    values = dict(source_id="readme", source_path="README.md", source_revision="abcdef1234",
                  source_available_at=None, availability_basis="current_observation",
                  availability_evidence_ref=None, retrieved_at="2024-02-29T12:00:00.000Z",
                  text=TEXT, scope="repo_wide",
                  source_url="https://github.com/owner/mono/blob/abcdef1234/README.md",
                  repository=RepoIdentity(repo_id=REPO, github_repo_id="900000001", github_node_id=None,
                                          repo_full_name="Owner/Mono", repo_url="https://github.com/owner/mono", known_urls=[]))
    values.update(changes)
    return FunctionalSource(**values)


class ManifestHistoricalVerifier:
    """A fixture-owned capture manifest, fixed before the request under test is altered."""

    def __init__(self, documents: Sequence[FunctionalSource], manifest_version: str = "capture-v1"):
        self.manifest_version = manifest_version
        self.calls: list[str] = []
        self.manifest = {document.source_id: {
            "source_id": document.source_id, "source_url": document.source_url,
            "source_path": document.source_path, "source_revision": document.source_revision,
            "repo_id": document.repository.repo_id if document.repository else None,
            "content_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
            "available_at": document.source_available_at,
        } for document in documents}

    def verify(self, document: FunctionalSource, cutoff_at: str) -> str | None:
        self.calls.append(document.source_id)
        entry = self.manifest.get(document.source_id)
        if entry is None or entry["available_at"] is None:
            return None
        if datetime.fromisoformat(entry["available_at"]) > datetime.fromisoformat(cutoff_at):
            return None
        if (document.source_available_at is None or
                datetime.fromisoformat(document.source_available_at) != datetime.fromisoformat(entry["available_at"])):
            return None
        observed = {
            "source_id": document.source_id, "source_url": document.source_url,
            "source_path": document.source_path, "source_revision": document.source_revision,
            "repo_id": document.repository.repo_id if document.repository else None,
            "content_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
        }
        if any(entry[field] != value for field, value in observed.items()):
            return None
        return "historical:" + hashlib.sha256(json.dumps(
            {"manifest_version": self.manifest_version, "entry": entry}, sort_keys=True,
        ).encode()).hexdigest()


class ManifestScopeVerifier:
    """Fixture-owned independent package/repository/document scope bindings."""

    def __init__(self, documents: Sequence[FunctionalSource], package_id: str = PACKAGE,
                 repo_id: str = REPO, package_scope: str = "docs/demo", manifest_version: str = "mapping-v1"):
        self.manifest_version = manifest_version
        self.calls: list[str] = []
        self.manifest = {document.source_id: {
            "package_id": package_id, "repo_id": repo_id, "package_scope": package_scope,
            "source_url": document.source_url, "source_path": document.source_path,
            "source_revision": document.source_revision,
            "content_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
        } for document in documents}

    def verify(self, package_id: str, repo_id: str, package_scope: str,
               document: FunctionalSource) -> str | None:
        self.calls.append(document.source_id)
        observed = {
            "package_id": package_id, "repo_id": repo_id, "package_scope": package_scope,
            "source_url": document.source_url, "source_path": document.source_path,
            "source_revision": document.source_revision,
            "content_sha256": hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
        }
        if self.manifest.get(document.source_id) != observed:
            return None
        return "scope:" + hashlib.sha256(json.dumps(
            {"manifest_version": self.manifest_version, "entry": observed}, sort_keys=True,
        ).encode()).hexdigest()


def historical_source(**changes):
    values = dict(source_available_at="2023-01-01T00:00:00.000Z", availability_basis="versioned_archive",
                  availability_evidence_ref="archive:abcdef1234")
    values.update(changes)
    return source(**values)


def package_document(**changes):
    values = dict(source_path="docs/demo/README.md", scope="target_package",
                  source_url="https://github.com/owner/mono/blob/abcdef1234/docs/demo/README.md",
                  scope_evidence_ref="provider:demo-package-doc-scope")
    values.update(changes)
    return source(**values)


def draft(spans):
    return dict(primary_function={"text": "Demo validates user-submitted documents.",
                                 "evidence_span_ids": [spans[0].span_id]},
                solved_problems=[], capabilities=[{"text": spans[1].text,
                                                   "evidence_span_ids": [spans[1].span_id]}],
                use_cases=[], functional_category=None, synonyms=[], alternative_candidates=[])


class FakeLLM:
    model_id = "configured-test-model"

    def __init__(self, transform=None):
        self.calls = 0
        self.transform = transform or (lambda output, spans, call: output)

    def generate(self, **kwargs):
        self.calls += 1
        spans = kwargs["spans"]
        return self.transform(draft(spans), spans, self.calls)


def build(sources=None, llm=None, **kwargs):
    return FunctionalProfiler().build_profile(package_id=PACKAGE, repo_id=REPO,
                                             package_scope=None, sources=sources or [source()], llm=llm, **kwargs)


def test_grounded_profile_preserves_exact_original_offsets_and_repo_only_scope():
    llm = FakeLLM()
    result = build(llm=llm)
    assert result.status == "verified" and result.scope_quality == "repo_only" and llm.calls == 1
    assert result.primary_function.text == "Demo validates user-submitted documents."
    assert not result.alternative_candidates_verified
    for evidence in result.evidence_refs:
        assert TEXT[evidence.start:evidence.end] == evidence.text
        assert TEXT[evidence.quote_start:evidence.quote_end] == evidence.quote
        assert evidence.content_sha256 == text_sha256(evidence.text)
    assert all("shields.io" not in span.text and "requests" not in span.text for span in prepare_passages([source()]))
    assert sum(len(span.text) for span in prepare_passages([source()], max_characters=20)) == 20


def test_ground_claims_reconstructs_existing_evidence_refs_from_base_span_fields():
    spans = prepare_passages([source()])
    original = FunctionalDraft.model_validate(draft(spans))
    _, existing_refs, _ = ground_claims(original, spans)
    # Reuse the same source span for a different supported quote, rather than copying old offsets.
    quote = "user-submitted documents"
    updated = original.model_copy(update={"primary_function": original.primary_function.model_copy(update={"text": quote})})
    fields, refs, diagnostics = ground_claims(updated, existing_refs)
    assert fields["primary_function"].text == quote and diagnostics == []
    assert len(refs) == 2 and all(isinstance(ref, EvidenceRef) for ref in refs)
    by_id = {ref.span_id: ref for ref in existing_refs}
    for ref in refs:
        assert {name: getattr(ref, name) for name in EvidenceSpan.model_fields} == {
            name: getattr(by_id[ref.span_id], name) for name in EvidenceSpan.model_fields
        }
        assert TEXT[ref.quote_start:ref.quote_end] == ref.quote
    assert next(ref for ref in refs if ref.quote == quote).quote_start == TEXT.index(quote)


@pytest.mark.parametrize("changes", [
    {},
    {"source_available_at": "2023-01-01T00:00:00.000Z"},
    {"availability_basis": "versioned_archive", "source_available_at": "2023-01-01T00:00:00.000Z", "availability_evidence_ref": "archive:abcdef1234"},
    {"availability_basis": "versioned_archive", "source_available_at": "2023-01-01T00:00:00.000Z", "availability_evidence_ref": "sha256:" + text_sha256(TEXT)},
    {"availability_basis": "versioned_archive", "source_available_at": "2023-01-01T00:00:00.000Z", "availability_evidence_ref": "https://web.archive.org/web/20230101000000/https://github.com/owner/mono"},
    {"availability_basis": "provider_version_proof", "source_available_at": "2023-01-01T00:00:00.000Z"},
    {"availability_basis": "versioned_archive", "source_available_at": "2024-01-02T00:00:00.000Z", "availability_evidence_ref": "archive:abcdef1234"},
    {"availability_basis": "provider_version_proof", "source_available_at": "2023-01-01T00:00:00.000Z", "availability_evidence_ref": "https://github.com/owner/mono/commit/abcdef1234"},
])
def test_historical_preflight_blocks_unproved_versions_without_calling_llm(changes):
    llm = FakeLLM()
    result = build([source(**changes)], llm, cutoff_at="2024-01-01T00:00:00.000Z")
    assert result.status == "unavailable" and result.missing_reason == "historical_revision_unverified"
    assert llm.calls == 0


def test_historical_archive_proof_and_scoped_pypi_description_are_explicit():
    historical = source(source_available_at="2023-01-01T00:00:00.000Z",
                        availability_basis="versioned_archive", availability_evidence_ref="archive:abcdef1234")
    verifier = ManifestHistoricalVerifier([historical])
    llm = FakeLLM()
    result = build([historical], llm, cutoff_at="2024-01-01T00:00:00.000Z", historical_verifier=verifier)
    assert result.status == "verified" and llm.calls == 1 and verifier.calls == [historical.source_id]
    assert result.historical_evidence_fingerprints[historical.source_id] == verifier.verify(historical, "2024-01-01T00:00:00.000Z")
    pypi = source(source_path="info.description", source_revision="1.0.0", repository=None,
                  source_url="https://pypi.org/pypi/Demo/1.0.0/json", scope="target_package")
    assert build([pypi], FakeLLM()).scope_quality == "target_package"


@pytest.mark.parametrize("url", ["https://example.org/README.md", "https://github.com/unrelated/repo/blob/abcdef1234/README.md", "https://github.com/owner/mono/blob/OTHER/README.md"])
def test_unofficial_or_mismatched_documents_are_not_used(url):
    llm = FakeLLM()
    result = build([source(source_url=url)], llm)
    assert result.status == "unavailable" and llm.calls == 0


def test_missing_revision_or_moving_head_cannot_construct_source():
    for revision in ["", "HEAD", "main"]:
        with pytest.raises(ValueError):
            source(source_revision=revision)


def test_ungrounded_or_unknown_span_claims_are_removed_even_when_other_claims_are_valid():
    def partial(output, spans, call):
        output["capabilities"] = [{"text": "Invented risk prediction", "evidence_span_ids": [spans[0].span_id]},
                                  {"text": spans[1].text, "evidence_span_ids": ["span:invented"]}]
        return output

    result = build(llm=FakeLLM(partial))
    assert result.status == "verified" and result.capabilities == []
    assert len(result.diagnostics) == 2

    def unsupported(output, spans, call):
        output["primary_function"]["text"] = "Predicts abandonment probability"
        return output

    llm = FakeLLM(unsupported)
    result = build(llm=llm)
    assert result.status == "unavailable" and result.missing_reason == "unsupported_profile" and llm.calls == 1


def test_no_source_no_credentials_and_no_model_do_not_guess_or_call_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REPOSEER_FUNCTIONAL_MODEL", raising=False)
    assert build().missing_reason == "missing_llm_configuration"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert build().missing_reason == "missing_llm_configuration"
    llm = FakeLLM()
    result = FunctionalProfiler().build_profile(package_id=PACKAGE, repo_id=REPO, package_scope=None, sources=[], llm=llm)
    assert result.missing_reason == "no_source" and llm.calls == 0


def test_validation_retry_is_bounded_and_cache_key_tracks_evidence_policy_and_model():
    llm = FakeLLM(lambda output, spans, call: {} if call == 1 else output)
    first = build(llm=llm)
    assert first.status == "verified" and llm.calls == 2
    assert first.cache_key == build(llm=FakeLLM()).cache_key
    bad = FakeLLM(lambda output, spans, call: {})
    assert build(llm=bad).missing_reason == "invalid_llm_output" and bad.calls == 2
    changed = build([source(text=TEXT + "\nAdditional documentation.")], FakeLLM())
    assert first.cache_key != changed.cache_key
    # Normalized text hashes alone would collide, invalidating original citation offsets.
    assert first.cache_key != build([source(text="\n" + TEXT)], FakeLLM()).cache_key
    different_model = FakeLLM()
    different_model.model_id = "other-configured-model"
    assert first.cache_key != build(llm=different_model).cache_key
    a, b = source(), source(source_id="quickstart")
    assert build([a, b], FakeLLM()).cache_key == build([b, a], FakeLLM()).cache_key


@pytest.mark.parametrize("mode,reason", [("refusal", "llm_refusal"), ("empty", "llm_empty_output"), ("incomplete", "llm_incomplete_output"), ("valid", None)])
def test_official_responses_adapter_arguments_and_expected_output_states(mode, reason):
    calls = []
    spans = prepare_passages([source()])

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="incomplete" if mode == "incomplete" else "completed",
                               output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])] if mode == "refusal" else [],
                               output_parsed=draft(spans) if mode == "valid" else None)

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    adapter = OpenAIResponsesFunctionalLLM(model="explicit-model", client=client)
    if reason:
        with pytest.raises(FunctionalLLMError, match=reason):
            adapter.generate(package_id=PACKAGE, repo_id=REPO, spans=spans)
    else:
        assert adapter.generate(package_id=PACKAGE, repo_id=REPO, spans=spans)["primary_function"]
    assert len(calls) == 1 and calls[0]["store"] is False and calls[0]["model"] == "explicit-model"
    assert calls[0]["max_output_tokens"] == 2500
    adapter.close()  # Injected client ownership stays with the caller.



def test_scoped_monorepo_docs_need_explicit_scope_evidence_and_cache_tracks_policy():
    doc = source(source_path="docs/demo/README.md", scope="target_package",
                 source_url="https://github.com/owner/mono/blob/abcdef1234/docs/demo/README.md")
    profiler = FunctionalProfiler()
    unproved = profiler.build_profile(package_id=PACKAGE, repo_id=REPO, package_scope="docs/demo", sources=[doc], llm=FakeLLM())
    assert unproved.scope_quality == "repo_only"
    self_declared = doc.model_copy(update={"scope_evidence_ref": "provider:demo-package-doc-scope"})
    claimed = profiler.build_profile(package_id=PACKAGE, repo_id=REPO, package_scope="docs/demo",
                                      sources=[self_declared], llm=FakeLLM())
    assert claimed.scope_quality == "repo_only" and claimed.cache_key == unproved.cache_key
    proved = profiler.build_profile(package_id=PACKAGE, repo_id=REPO, package_scope="docs/demo",
                                    sources=[doc], llm=FakeLLM(), scope_verifier=ManifestScopeVerifier([doc]))
    assert proved.scope_quality == "target_package" and proved.cache_key != unproved.cache_key
    historical = historical_source()
    assert build([historical], FakeLLM()).cache_key != build(
        [historical], FakeLLM(), cutoff_at="2024-01-01T00:00:00.000Z",
        historical_verifier=ManifestHistoricalVerifier([historical]),
    ).cache_key
    new_revision = source(source_revision="abcdef5678", source_url="https://github.com/owner/mono/blob/abcdef5678/README.md")
    assert build(llm=FakeLLM()).cache_key != build([new_revision], FakeLLM()).cache_key


def test_owned_official_sdk_uses_finite_timeout_and_no_hidden_retries(monkeypatch):
    import sys

    captured = {}
    client = SimpleNamespace(close=lambda: captured.update(closed=True))

    def create(**kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=create, APIError=FunctionalLLMError))
    adapter = OpenAIResponsesFunctionalLLM(model="explicit-model", api_key="synthetic", timeout=15)
    assert captured["timeout"] == 15 and captured["max_retries"] == 0
    adapter.close()
    assert captured["closed"]
    with pytest.raises(ValueError):
        OpenAIResponsesFunctionalLLM(model="", client=client)


def test_expected_provider_failure_returns_unavailable_without_retry():
    class Failed(FakeLLM):
        def generate(self, **kwargs):
            self.calls += 1
            raise FunctionalLLMError("llm_request_failed")

    llm = Failed()
    result = build(llm=llm)
    assert result.missing_reason == "llm_request_failed" and llm.calls == 1



def test_passages_keep_prose_immediately_after_headings_and_exclude_code():
    text = ("# Demo\nDemo validates user-submitted documents.\n"
            "## Usage\nExports validation errors as structured reports.\n"
            "```python\nimport requests\nprint('not functional evidence')\n```\n"
            "## Dependencies\nPython requests click rich\n")
    document = source(text=text)
    spans = prepare_passages([document])
    assert [span.text for span in spans] == ["Demo validates user-submitted documents.",
                                             "Exports validation errors as structured reports."]
    assert all(text[span.start:span.end] == span.text for span in spans)
    assert build([document], FakeLLM()).status == "verified"



def test_verified_package_docs_take_precedence_over_repo_wide_monorepo_text():
    scoped = source(source_id="target-docs", source_path="docs/demo/README.md", scope="target_package",
                    source_url="https://github.com/owner/mono/blob/abcdef1234/docs/demo/README.md",
                    scope_evidence_ref="provider:demo-package-doc-scope",
                    source_available_at="2023-01-01T00:00:00.000Z", availability_basis="versioned_archive",
                    availability_evidence_ref="archive:abcdef1234")
    unrelated = historical_source(source_id="aaa-repo-wide", text="# Mono\nOther packages build unrelated web apps.")

    class ScopedLLM(FakeLLM):
        def generate(self, **kwargs):
            assert {span.source_id for span in kwargs["spans"]} == {"target-docs"}
            return super().generate(**kwargs)

    llm = ScopedLLM()
    result = FunctionalProfiler().build_profile(package_id=PACKAGE, repo_id=REPO, package_scope="docs/demo",
                                                sources=[unrelated, scoped], cutoff_at="2024-01-01T00:00:00.000Z", llm=llm,
                                                historical_verifier=ManifestHistoricalVerifier([unrelated, scoped]),
                                                scope_verifier=ManifestScopeVerifier([scoped]))
    assert result.status == "verified" and result.scope_quality == "target_package" and llm.calls == 1
    assert len(result.source_revision_keys) == 1 and result.source_revision_keys[0].startswith("target-docs@")



@pytest.mark.parametrize("changes", [
    {"text": TEXT + "\nChanged bytes."},
    {"text": "\n" + TEXT},  # Shared normalized text hash alone would hide this change.
    {"source_id": "different-source"},
    {"source_revision": "abcdef5678", "source_url": "https://github.com/owner/mono/blob/abcdef5678/README.md"},
    {"source_path": "docs/README.md", "source_url": "https://github.com/owner/mono/blob/abcdef1234/docs/README.md"},
])
def test_independent_historical_manifest_rejects_mismatched_identity_revision_or_bytes(changes):
    document = historical_source()
    verifier = ManifestHistoricalVerifier([document])
    altered = document.model_copy(update=changes)
    llm = FakeLLM()
    result = build([altered], llm, cutoff_at="2024-01-01T00:00:00.000Z", historical_verifier=verifier)
    assert result.missing_reason == "historical_revision_unverified" and llm.calls == 0
    assert verifier.calls == [altered.source_id]


def test_future_metadata_is_rejected_before_verification_and_future_manifest_is_rejected_before_llm():
    document = historical_source()
    verifier = ManifestHistoricalVerifier([document])
    llm = FakeLLM()
    future = document.model_copy(update={"source_available_at": "2024-01-02T00:00:00.000Z"})
    result = build([future], llm, cutoff_at="2024-01-01T00:00:00.000Z", historical_verifier=verifier)
    assert result.missing_reason == "historical_revision_unverified" and not verifier.calls and llm.calls == 0
    verifier.manifest[document.source_id]["available_at"] = "2024-01-02T00:00:00.000Z"
    result = build([document], llm, cutoff_at="2024-01-01T00:00:00.000Z", historical_verifier=verifier)
    assert result.missing_reason == "historical_revision_unverified" and verifier.calls == [document.source_id] and llm.calls == 0


@pytest.mark.parametrize("change", ["package_id", "repo_id", "source_path", "source_revision", "path_boundary"])
def test_package_scope_manifest_mismatch_does_not_upgrade_scope(change):
    document = package_document()
    verifier = ManifestScopeVerifier([document])
    package_id, repo_id = PACKAGE, REPO
    if change == "package_id":
        package_id = "pypi:other"
    elif change == "repo_id":
        repo_id = "github:repository:900000002"
        document = document.model_copy(update={"repository": document.repository.model_copy(update={
            "repo_id": repo_id, "github_repo_id": "900000002",
        })})
    elif change == "source_path":
        document = document.model_copy(update={"source_path": "docs/demo/quickstart.md",
            "source_url": "https://github.com/owner/mono/blob/abcdef1234/docs/demo/quickstart.md"})
    elif change == "source_revision":
        document = document.model_copy(update={"source_revision": "abcdef5678",
            "source_url": "https://github.com/owner/mono/blob/abcdef5678/docs/demo/README.md"})
    else:
        document = document.model_copy(update={"source_path": "docs/demo-other/README.md",
            "source_url": "https://github.com/owner/mono/blob/abcdef1234/docs/demo-other/README.md"})
    result = FunctionalProfiler().build_profile(package_id=package_id, repo_id=repo_id, package_scope="docs/demo",
                                                sources=[document], llm=FakeLLM(), scope_verifier=verifier)
    assert result.status == "verified" and result.scope_quality == "repo_only"
    assert result.scope_evidence_fingerprints == {}
    assert verifier.calls == ([] if change == "path_boundary" else [document.source_id])


@pytest.mark.parametrize("inject_wrong_verifier", [False, True])
def test_unverified_target_marker_cannot_displace_repo_wide_passages(inject_wrong_verifier):
    target = package_document(source_id="aaa-claimed-package")
    repository = source(source_id="repo-readme")
    verifier = ManifestScopeVerifier([target], package_id="pypi:other") if inject_wrong_verifier else None

    class RepoOnlyLLM(FakeLLM):
        def generate(self, **kwargs):
            assert {span.source_id for span in kwargs["spans"]} == {"repo-readme"}
            return super().generate(**kwargs)

    result = FunctionalProfiler().build_profile(package_id=PACKAGE, repo_id=REPO, package_scope="docs/demo",
                                                sources=[target, repository], llm=RepoOnlyLLM(), scope_verifier=verifier)
    assert result.status == "verified" and result.scope_quality == "repo_only"
    assert result.source_revision_keys[0].startswith("repo-readme@")


def test_verified_evidence_and_policy_versions_isolate_cache_keys():
    doc = package_document(source_available_at="2023-01-01T00:00:00.000Z", availability_basis="versioned_archive")
    profiler = FunctionalProfiler()

    def profile(historical_version="capture-v1", scope_version="mapping-v1", scope_verified=True):
        return profiler.build_profile(package_id=PACKAGE, repo_id=REPO, package_scope="docs/demo", sources=[doc],
                                      cutoff_at="2024-01-01T00:00:00.000Z", llm=FakeLLM(),
                                      historical_verifier=ManifestHistoricalVerifier([doc], historical_version),
                                      scope_verifier=ManifestScopeVerifier([doc], manifest_version=scope_version) if scope_verified else None)

    baseline = profile()
    assert baseline.status == "verified" and baseline.cache_key == profile().cache_key
    assert baseline.cache_key != profile(historical_version="capture-v2").cache_key
    assert baseline.cache_key != profile(scope_version="mapping-v2").cache_key
    assert baseline.cache_key != profile(scope_verified=False).cache_key
    args = (PACKAGE, REPO, "docs/demo", [doc], "2024-01-01T00:00:00.000Z", "model", (12000, 8))
    fingerprints = dict(historical_evidence=baseline.historical_evidence_fingerprints,
                        scope_evidence=baseline.scope_evidence_fingerprints)
    assert profile_cache_key(*args, **fingerprints, verification_policy_version="v1") != profile_cache_key(*args, **fingerprints, verification_policy_version="v2")
    forged = doc.model_copy(update={"scope_evidence_ref": "self-declared-other", "availability_evidence_ref": "fake-reference"})
    assert profile_cache_key(*args, **fingerprints) == profile_cache_key(
        PACKAGE, REPO, "docs/demo", [forged], "2024-01-01T00:00:00.000Z", "model", (12000, 8), **fingerprints,
    )
    other = doc.model_copy(update={"source_id": "second-doc"})
    assert profile_cache_key(PACKAGE, REPO, "docs/demo", [doc, other], None, "model", (12000, 8)) == profile_cache_key(
        PACKAGE, REPO, "docs/demo", [other, doc], None, "model", (12000, 8),
    )


@pytest.mark.parametrize("stage", ["historical", "scope"])
def test_verifier_errors_never_become_verified_proof_or_trigger_retries(stage):
    doc = package_document(source_available_at="2023-01-01T00:00:00.000Z", availability_basis="versioned_archive")

    class BrokenVerifier:
        calls = 0

        def verify(self, *args):
            self.calls += 1
            raise SourceVerificationError("Injected evidence backend failure")

    broken = BrokenVerifier()
    llm = FakeLLM()
    args = {"historical_verifier": broken, "cutoff_at": "2024-01-01T00:00:00.000Z"} if stage == "historical" else {"scope_verifier": broken}
    result = FunctionalProfiler().build_profile(package_id=PACKAGE, repo_id=REPO, package_scope="docs/demo", sources=[doc], llm=llm, **args)
    assert broken.calls == 1
    if stage == "historical":
        assert result.missing_reason == "historical_revision_unverified" and llm.calls == 0
    else:
        assert result.scope_quality == "repo_only" and result.scope_evidence_fingerprints == {} and llm.calls == 1
    assert f"{stage}_verification_failed" in result.diagnostics


def test_pypi_metadata_matching_is_not_historical_authentication():
    doc = historical_source(source_path="info.description", source_revision="1.0.0", repository=None,
                            source_url="https://pypi.org/pypi/Demo/1.0.0/json", scope="target_package")
    llm = FakeLLM()
    assert build([doc], llm, cutoff_at="2024-01-01T00:00:00.000Z").missing_reason == "historical_revision_unverified"
    assert llm.calls == 0
    verified = build([doc], llm, cutoff_at="2024-01-01T00:00:00.000Z", historical_verifier=ManifestHistoricalVerifier([doc]))
    assert verified.status == "verified" and verified.scope_quality == "target_package" and llm.calls == 1
    for url in ["https://pypi.org/pypi/other/1.0.0/json", "https://pypi.org/pypi/Demo/2.0.0/json"]:
        assert build([doc.model_copy(update={"source_url": url})], FakeLLM()).status == "unavailable"
