"""Read retained canonical captures into soft research intake; never fetch or promote."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

from pydantic import Field

from reposeer.collectors.external.crawler.validation import (
    ExternalDocumentPayload,
    validate_external_document_record,
)
from reposeer.contracts.models import (
    ContractModel,
    Count,
    HttpUrl,
    NonEmpty,
    PackageId,
    RawRecord,
    RawRecordId,
    RepoId,
    UtcTimestamp,
)
from reposeer.contracts.specialist_agents import ROUTES, ResearchCandidate, ResearchHandoffRequest
from reposeer.storage.raw.jsonl import JsonlStorage

SPECIALISTS = tuple(ROUTES)


def request_id_for(run_id: str, kind: str) -> str:
    """Stable coordinator-owned UUID-shaped identity, not an authentication token."""
    return str(UUID(bytes=hashlib.sha256(f'{run_id}:{kind}:research'.encode()).digest()[:16], version=4))


class IntakeReason(ContractModel):
    reason_code: NonEmpty
    count: Count


class IntakeSummary(ContractModel):
    records_seen: Count = 0
    accepted_candidates: Count = 0
    retained_candidates: Count = 0
    omitted_candidates: Count = 0
    duplicate_records: Count = 0
    truncated_excerpts: Count = 0
    rejected_records: Count = 0
    rejections: list[IntakeReason] = Field(default_factory=list)


class _ScopeReference(ContractModel):
    raw_record_id: RawRecordId
    source_url: HttpUrl
    retrieved_at: UtcTimestamp


class _RetainedScope(ContractModel):
    """Upstream current-discovery receipt; not independent archive/relevance proof."""
    package_id: PackageId
    repo_id: RepoId
    repository_url: HttpUrl
    status: str
    basis: str
    article_relevance: str
    historical_validity: str
    observed_at: UtcTimestamp
    evidence: list[_ScopeReference] = Field(min_length=2)


def build_research_requests_from_raw(
    raw_documents_path: Path, *, analysis_run_id: str, package_id: str, cutoff_at: str,
    repo_id: str | None = None, package_description: str | None = None,
    description_source_refs: list[str] | None = None, max_candidates: int = 12,
    max_excerpt_chars: int = 5000, summary: IntakeSummary | None = None,
) -> list[ResearchHandoffRequest]:
    """Return exactly three requests; optionally fill bounded intake audit counters.

    Trust only retained upstream package-scope receipts, never title/URL matching.
    Receipt consistency is not proof of document relevance or historical validity.
    JSONL is streamed; at most max_candidates excerpts are retained in memory.
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= 50:
        raise ValueError('max_candidates must be between 1 and 50')
    if type(max_excerpt_chars) is not int or not 1 <= max_excerpt_chars <= 10000:
        raise ValueError('max_excerpt_chars must be between 1 and 10000')
    path = Path(raw_documents_path)
    if not path.is_file():
        raise ValueError('raw_documents_path must be an existing explicit JSONL file')
    base = dict(schema_version='0.1.0', record_type='specialist_research_request',
                analysis_run_id=analysis_run_id, package_id=package_id, repo_id=repo_id,
                cutoff_at=cutoff_at, package_description=package_description,
                description_source_refs=description_source_refs or [], research_questions=[
                    'What does the supplied evidence support, what is uncertain, and what should D03-D05 retrieve next?'
                ])
    # Validate identity/configuration even when the file is empty.
    ResearchHandoffRequest(**base, request_id=request_id_for(analysis_run_id, SPECIALISTS[0]),
                           agent_kind=SPECIALISTS[0], candidates=[])
    report = IntakeSummary()
    retained, fingerprints, reasons = {}, {}, {}
    for value in JsonlStorage().iter_records(path):
        report.records_seen += 1
        try:
            raw = validate_external_document_record(RawRecord.model_validate(value))
            payload = ExternalDocumentPayload.model_validate(raw.payload)
        except (ValueError, TypeError):
            reason = 'invalid_external_document'
        else:
            fingerprint = hashlib.sha256(json.dumps(
                raw.model_dump(mode='json'), sort_keys=True, separators=(',', ':')
            ).encode()).hexdigest()
            if raw.raw_record_id in fingerprints:
                if fingerprints[raw.raw_record_id] != fingerprint:
                    raise ValueError('conflicting raw_record_id revisions')
                report.duplicate_records += 1
                continue
            fingerprints[raw.raw_record_id] = fingerprint
            try:
                scope = _RetainedScope.model_validate(payload.repository_scope)
                valid_scope = (
                    raw.package_id == package_id == scope.package_id
                    and raw.repo_id == scope.repo_id
                    and (repo_id is None or repo_id == scope.repo_id)
                    and scope.status == 'verified_current'
                    and scope.basis == 'verified_current_discovery_scope'
                    and scope.observed_at <= payload.fetched_at
                    and len({ref.raw_record_id for ref in scope.evidence}) == len(scope.evidence)
                    and all(ref.retrieved_at <= scope.observed_at for ref in scope.evidence)
                )
            except (ValueError, TypeError):
                valid_scope = False
            if valid_scope:
                report.accepted_candidates += 1
                excerpt = payload.content
                if excerpt is not None and len(excerpt) > max_excerpt_chars:
                    report.truncated_excerpts += 1
                    excerpt = excerpt[:max_excerpt_chars]
                candidate = ResearchCandidate(
                    candidate_id=raw.raw_record_id, source_ref=raw.raw_record_id,
                    source_url=raw.source_url, source_kind='external',
                    relevance_status='not_assessed', topic_hint=None, text_excerpt=excerpt,
                    published_at=payload.published_at, available_at=payload.content_available_at,
                    fetched_at=payload.fetched_at,
                )
                retained[candidate.source_ref] = candidate
                if len(retained) > max_candidates:
                    del retained[max(retained)]
                continue
            reason = 'target_scope_unverified_or_mismatched'
        report.rejected_records += 1
        reasons[reason] = reasons.get(reason, 0) + 1
    report.retained_candidates = len(retained)
    report.omitted_candidates = report.accepted_candidates - len(retained)
    report.rejections = [IntakeReason(reason_code=key, count=reasons[key]) for key in sorted(reasons)]
    if summary is not None:
        for field in IntakeSummary.model_fields:
            setattr(summary, field, getattr(report, field))
    candidates = [retained[key] for key in sorted(retained)]
    return [ResearchHandoffRequest(
        **base, request_id=request_id_for(analysis_run_id, kind), agent_kind=kind,
        candidates=candidates,
    ) for kind in SPECIALISTS]
