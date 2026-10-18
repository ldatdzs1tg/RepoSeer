"""Internal D03 models and extractive grounding; canonical T-011 is unchanged."""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reposeer.contracts.identity import normalize_pypi_name, text_sha256
from reposeer.contracts.models import (
    HttpUrl,
    NonEmpty,
    PackageId,
    RepoId,
    RepoIdentity,
    UtcTimestamp,
)

PROFILER_VERSION = "0.2.0-extractive"
VERIFICATION_POLICY_VERSION = "1.0.0"


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FunctionalSource(ProfileModel):
    source_id: NonEmpty
    source_path: NonEmpty
    source_revision: NonEmpty
    source_available_at: UtcTimestamp | None
    availability_basis: Literal["current_observation", "versioned_archive", "provider_version_proof", "unknown"]
    availability_evidence_ref: NonEmpty | None
    retrieved_at: UtcTimestamp
    text: str
    scope: Literal["target_package", "repo_wide"]
    source_url: HttpUrl
    repository: RepoIdentity | None = None
    scope_evidence_ref: NonEmpty | None = None

    @model_validator(mode="after")
    def concrete_version(self):
        if self.source_revision.casefold() in {"head", "main", "master", "latest"}:
            raise ValueError("source_revision must identify a concrete version")
        if (self.source_available_at is not None and
                datetime.fromisoformat(self.source_available_at) > datetime.fromisoformat(self.retrieved_at)):
            raise ValueError("source_available_at exceeds retrieved_at")
        if any(part in {"", ".", ".."} for part in self.source_path.split("/")) or "\\" in self.source_path:
            raise ValueError("source_path must be relative")
        return self


class SourceVerificationError(Exception):
    """Expected trusted-backend evidence failure; never a successful verification."""


class HistoricalSourceVerifier(Protocol):
    """Trusted backend dependency, never a caller-supplied request field.

    Independently bind source identity/revision, exact UTF-8 bytes and availability
    to a trusted manifest at/before cutoff. Return its stable evidence fingerprint,
    None for unverified evidence, or raise SourceVerificationError on backend failure.
    Checking a caller's availability_evidence_ref string does not implement this contract.
    """

    def verify(self, source: FunctionalSource, cutoff_at: str) -> str | None: ...


class PackageScopeVerifier(Protocol):
    """Bind package/repository/scope and document revision/path to trusted mapping evidence.

    Return a stable manifest fingerprint or None, without relying on the caller's
    scope_evidence_ref. Operational failures use SourceVerificationError.
    """

    def verify(self, package_id: str, repo_id: str, package_scope: str,
               source: FunctionalSource) -> str | None: ...


class EvidenceSpan(ProfileModel):
    span_id: NonEmpty
    source_id: NonEmpty
    source_path: NonEmpty
    source_url: HttpUrl
    source_revision: NonEmpty
    start: int
    end: int
    text: NonEmpty
    content_sha256: str


class FunctionalClaim(ProfileModel):
    text: str = Field(min_length=1, max_length=800)
    evidence_span_ids: list[str] = Field(min_length=1, max_length=8)


class FunctionalDraft(ProfileModel):
    """Strict LLM response: all fields required, all functional statements cited."""
    primary_function: FunctionalClaim | None
    solved_problems: list[FunctionalClaim]
    capabilities: list[FunctionalClaim]
    use_cases: list[FunctionalClaim]
    functional_category: FunctionalClaim | None
    synonyms: list[FunctionalClaim]
    alternative_candidates: list[FunctionalClaim]


class EvidenceRef(EvidenceSpan):
    quote: NonEmpty
    quote_start: int
    quote_end: int


class FunctionalProfileResult(ProfileModel):
    status: Literal["verified", "unavailable"]
    package_id: PackageId
    repo_id: RepoId
    scope_quality: Literal["target_package", "repo_only", "unknown"] = "unknown"
    cutoff_at: UtcTimestamp | None = None
    primary_function: FunctionalClaim | None = None
    solved_problems: list[FunctionalClaim] = Field(default_factory=list)
    capabilities: list[FunctionalClaim] = Field(default_factory=list)
    use_cases: list[FunctionalClaim] = Field(default_factory=list)
    functional_category: FunctionalClaim | None = None
    synonyms: list[FunctionalClaim] = Field(default_factory=list)
    alternative_candidates: list[FunctionalClaim] = Field(default_factory=list)
    alternative_candidates_verified: Literal[False] = False
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    source_revision_keys: list[str] = Field(default_factory=list)
    profiler_version: str = PROFILER_VERSION
    verification_policy_version: str = VERIFICATION_POLICY_VERSION
    historical_evidence_fingerprints: dict[str, str] = Field(default_factory=dict)
    scope_evidence_fingerprints: dict[str, str] = Field(default_factory=dict)
    llm_model: str | None = None
    missing_reason: str | None = None
    cache_key: str | None = None
    diagnostics: list[str] = Field(default_factory=list)


def source_eligibility(source: FunctionalSource, package_id: str, repo_id: str,
                       package_scope: str | None, cutoff_at: str | None) -> str | None:
    """Metadata consistency only; None does not establish historical or scope authenticity."""
    parsed = urlsplit(source.source_url)
    if parsed.scheme != "https" or parsed.port is not None or parsed.query or parsed.fragment:
        return "unofficial_source"
    path = parsed.path.strip("/")
    doc_path = source.source_path.casefold()
    if (not (parsed.hostname == "pypi.org" and source.source_path == "info.description") and not (
            doc_path.endswith((".md", ".rst", ".txt")) and (
            doc_path.split("/")[-1].startswith(("readme", "quickstart", "example"))
            or doc_path.startswith(("docs/", "doc/", "examples/"))))):
        return "unsupported_document_path"
    official = False
    if parsed.hostname in {"github.com", "raw.githubusercontent.com"} and source.repository is not None:
        identity = RepoIdentity.model_validate(source.repository.model_dump())
        prefix = identity.repo_full_name.casefold()
        suffix = f"{source.source_revision}/{source.source_path}"
        expected = f"{prefix}/blob/{suffix}" if parsed.hostname == "github.com" else f"{prefix}/{suffix}"
        official = (identity.repo_id == repo_id and path[:len(prefix)].casefold() == prefix
                    and path[len(prefix):] == expected[len(prefix):])
    elif parsed.hostname == "pypi.org":
        parts = path.split("/")
        if len(parts) == 4 and parts[0] == "pypi" and parts[3] == "json":
            official = ("pypi:" + normalize_pypi_name(parts[1]) == package_id
                        and parts[2] == source.source_revision and source.source_path == "info.description"
                        and source.scope == "target_package")
    if not official:
        return "unofficial_or_mismatched_source"
    if cutoff_at is not None:
        if (source.source_available_at is None
                or datetime.fromisoformat(source.source_available_at) > datetime.fromisoformat(cutoff_at)
                or source.availability_basis not in {"versioned_archive", "provider_version_proof"}):
            return "historical_revision_unverified"
    return None


def prepare_passages(sources: Sequence[FunctionalSource], *, max_characters: int = 12000,
                     max_spans: int = 8, span_characters: int = 2000) -> list[EvidenceSpan]:
    """Select prose paragraphs without rewriting text, preserving original character offsets."""
    spans = []
    remaining = max_characters
    sections = {"overview", "purpose", "features", "usage", "use cases", "examples", "quickstart", "quick start"}
    for source in sorted(sources, key=lambda item: (item.source_id, item.source_revision, item.source_path)):
        selected, fenced = True, False
        for match in re.finditer(
                r"(?m)^#{1,6}[ \t]+[^\n]*|^```[^\n]*|[^\n]+(?:\n(?!\n|#|```)[^\n]+)*",
                source.text):
            paragraph = match.group()
            stripped = paragraph.strip()
            if stripped.startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            heading = re.match(r"^#{1,6}\s+(.+)", stripped)
            if heading:
                title = heading[1].strip().casefold()
                selected = title in sections or (stripped.startswith("# ") and match.start() == 0)
                continue
            if not selected or not stripped or any(token in paragraph for token in ("shields.io", "![", "[![", "<nav", "<badge")):
                continue
            if stripped.startswith(("import ", "from ", "pip install", "uv add", "|", "[", "<")):
                continue
            start = match.start() + len(paragraph) - len(paragraph.lstrip())
            text = stripped[:min(span_characters, remaining)]
            if not text or len(spans) >= max_spans:
                return spans
            end = start + len(text)
            identifier = text_sha256(f"{source.source_id}:{source.source_revision}:{start}:{end}:{text}")
            spans.append(EvidenceSpan(span_id="span:" + identifier, source_id=source.source_id,
                                      source_path=source.source_path, source_url=source.source_url,
                                      source_revision=source.source_revision, start=start, end=end,
                                      text=text, content_sha256=text_sha256(text)))
            remaining -= len(text)
            if remaining <= 0:
                return spans
    return spans


def profile_cache_key(package_id: str, repo_id: str, scope: str | None,
                      sources: Sequence[FunctionalSource], cutoff_at: str | None, model: str,
                      limits: tuple[int, int], *,
                      historical_evidence: Mapping[str, str] | None = None,
                      scope_evidence: Mapping[str, str] | None = None,
                      verification_policy_version: str = VERIFICATION_POLICY_VERSION) -> str:
    policy = {"package_id": package_id, "repo_id": repo_id, "scope": scope,
              "sources": [dict(source.model_dump(exclude={"text", "availability_evidence_ref", "scope_evidence_ref"}), text_hash=text_sha256(source.text),
                               original_text_sha256=hashlib.sha256(source.text.encode("utf-8")).hexdigest())
                          for source in sorted(sources, key=lambda item: (item.source_id, item.source_revision, item.source_path))],
              "cutoff_at": cutoff_at, "version": PROFILER_VERSION, "model": model, "limits": limits,
              "verification_policy_version": verification_policy_version,
              "historical_evidence": dict(historical_evidence or {}),
              "scope_evidence": dict(scope_evidence or {})}
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ground_claims(draft: FunctionalDraft, spans: Sequence[EvidenceSpan]) -> tuple[dict, list[EvidenceRef], list[str]]:
    """Conservative extractive validation: IDs alone do not prove a model's paraphrase."""
    by_id = {span.span_id: span for span in spans}
    refs, diagnostics = {}, []

    def validate(claim):
        if claim is None:
            return None
        selected = [by_id.get(identifier) for identifier in claim.evidence_span_ids]
        if not claim.text.strip() or any(span is None or claim.text not in span.text for span in selected):
            diagnostics.append("unsupported_claim_removed")
            return None
        for span in selected:
            position = span.start + span.text.index(claim.text)
            base_fields = {name: getattr(span, name) for name in EvidenceSpan.model_fields}
            refs[(span.span_id, claim.text)] = EvidenceRef(**base_fields, quote=claim.text,
                                                         quote_start=position, quote_end=position + len(claim.text))
        return claim

    fields = {}
    for name in FunctionalDraft.model_fields:
        value = getattr(draft, name)
        fields[name] = [valid for item in value if (valid := validate(item)) is not None] if isinstance(value, list) else validate(value)
    return fields, list(refs.values()), diagnostics
