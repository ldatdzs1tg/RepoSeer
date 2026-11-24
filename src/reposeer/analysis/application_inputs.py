"""Explicit upstream inputs for T-027; files are not historical verification receipts."""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from reposeer.agents import MultiAgentRunRequest
from reposeer.analysis.core import CoreFeaturePackage
from reposeer.analysis.external import ExternalContextRequest
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.contracts.analysis import DependencyImpact, SourceProvenance
from reposeer.contracts.identity import canonical_github_url, package_id
from reposeer.contracts.models import (
    ContractModel,
    NonEmpty,
    PackageId,
    RawRecord,
    UtcTimestamp,
    UuidText,
    unique,
)
from reposeer.services.repository_service import parse_repository_target


class AnalysisInputError(ValueError):
    """Safe machine-readable input/selection error for an interface without a package yet."""


def parse_identifier(identifier: str) -> tuple[str | None, str | None]:
    value = identifier.strip()
    if not value:
        raise AnalysisInputError('invalid_identifier')
    try:
        if value.startswith('pypi:'):
            return package_id(value[5:]), None
        if '/' in value or ':' in value:
            return None, canonical_github_url(parse_repository_target(value))
        return package_id(value), None
    except (TypeError, ValueError):
        raise AnalysisInputError('invalid_identifier') from None


class AnalyzeRequest(ContractModel):
    identifier: NonEmpty
    package_name: NonEmpty | None = None
    cutoff_at: UtcTimestamp | None = None
    mode_requested: Literal['CORE', 'ENHANCED'] = 'CORE'
    purpose: Literal['current_inference', 'retrospective_analysis'] = 'retrospective_analysis'
    include_external_insights: bool = False
    include_agents: bool = False
    include_dependency_impact: bool = False

    @model_validator(mode='after')
    def check(self):
        subject, _ = parse_identifier(self.identifier)
        if self.package_name is not None:
            selected, repository = parse_identifier(self.package_name)
            if repository is not None or (subject is not None and subject != selected):
                raise AnalysisInputError('package_selection_mismatch')
        return self


class AnalysisBundle(ContractModel):
    """Reviewed CORE input plus optional T-025/T-026 inputs, never an executable plan."""

    core: CoreFeaturePackage
    mapping_evidence: list[RawRecord] = Field(default_factory=list)
    external: ExternalContextRequest | None = None
    agents: MultiAgentRunRequest | None = None
    agent_sources: list[SourceProvenance] = Field(default_factory=list)
    dependency_impact: DependencyImpact | None = None
    impact_sources: list[SourceProvenance] = Field(default_factory=list)

    @model_validator(mode='after')
    def check(self):
        subject, cutoff = self.core.snapshot.package_id, self.core.snapshot.cutoff_at
        unique([row.source_id for row in self.agent_sources], 'agent source IDs')
        unique([row.source_id for row in self.impact_sources], 'impact source IDs')
        for context in (self.external, self.agents):
            if context and (context.package_id != subject or context.cutoff_at != cutoff):
                raise ValueError('optional input package/cutoff differs from CORE')
        if self.agents:
            for child in [*self.agents.research_requests, *self.agents.strict_requests]:
                if child.repo_id is not None and child.repo_id != self.core.snapshot.repo_id:
                    raise ValueError('agent repository differs from CORE')
            for child in self.agents.strict_requests:
                if self.core.mapping is None or child.mapping_revision != self.core.mapping.mapping_revision:
                    raise ValueError('strict agent mapping revision differs from CORE')
        return self

    def repository_url(self) -> str | None:
        """Reverse resolution requires actual PyPI/GitHub mapping observations."""
        if not self.mapping_evidence or self.core.mapping is None:
            return None
        packages = [row for row in self.mapping_evidence if row.source == 'pypi_json']
        repositories = [row for row in self.mapping_evidence if row.source == 'github_rest'
                        and row.source_record_type == 'github_repository']
        if len(packages) != 1 or len(repositories) != 1:
            raise ValueError('mapping_evidence_requires_one_pypi_and_github_observation')
        scope = RepositoryScopeEvidence(pypi=packages[0], github=repositories[0]).resolve()
        if scope['package_id'] != self.core.snapshot.package_id or scope['repo_id'] != self.core.mapping.repo_id:
            raise ValueError('mapping_evidence_identity_mismatch')
        sources = {row.source_id: row for row in self.core.sources}
        for raw in (packages[0], repositories[0]):
            digest = hashlib.sha256(json.dumps(raw.payload, sort_keys=True, ensure_ascii=False,
                                              separators=(',', ':')).encode()).hexdigest()
            if not any(raw.raw_record_id in sources[key].raw_record_ids
                       and raw.source_url == sources[key].source_url
                       and raw.source == sources[key].source
                       and raw.source_record_id == sources[key].source_record_id
                       and raw.retrieved_at == sources[key].retrieved_at
                       and (sources[key].revision == digest or sources[key].content_sha256 == digest
                            or (raw.raw_body_sha256 is not None
                                and sources[key].content_sha256 == raw.raw_body_sha256))
                       for key in self.core.mapping.source_ids):
                raise ValueError('mapping_observation_missing_from_mapping_lineage')
        return scope['repository_url']


class ResolvedTarget(ContractModel):
    package_id: PackageId
    cutoff_at: UtcTimestamp | None
    bundle: AnalysisBundle | None = None


class AnalysisBackend(Protocol):
    def resolve(self, request: AnalyzeRequest) -> ResolvedTarget: ...

    def collect(self, target: ResolvedTarget, request: AnalyzeRequest,
                analysis_run_id: UuidText) -> AnalysisBundle: ...


class StoredAnalysisBackend:
    """Explicit, caller-reviewed upstream bundles; no network or implicit raw discovery."""

    def __init__(self, bundles: Sequence[AnalysisBundle]):
        self.bundles = [AnalysisBundle.model_validate(row.model_dump()) for row in bundles]
        unique([(row.core.snapshot.package_id, row.core.snapshot.cutoff_at) for row in self.bundles],
               'stored package/cutoff keys')

    @classmethod
    def from_files(cls, paths: Sequence[Path]) -> 'StoredAnalysisBackend':
        bundles = []
        for path in paths:
            if Path(path).stat().st_size > 16 * 1024 * 1024:
                raise AnalysisInputError('analysis_bundle_exceeds_16MiB')
            bundles.append(AnalysisBundle.model_validate_json(Path(path).read_bytes()))
        return cls(bundles)

    def resolve(self, request: AnalyzeRequest) -> ResolvedTarget:
        subject, url = parse_identifier(request.identifier)
        if request.package_name:
            subject, _ = parse_identifier(request.package_name)
        matches = [row for row in self.bundles
                   if (subject is None or row.core.snapshot.package_id == subject)
                   and (request.cutoff_at is None or row.core.snapshot.cutoff_at == request.cutoff_at)
                   and (url is None or row.repository_url() == url)]
        if len({row.core.snapshot.package_id for row in matches}) > 1:
            raise AnalysisInputError('github_package_selection_required')
        if not matches:
            if url is not None:
                raise AnalysisInputError('github_mapping_unresolved')
            return ResolvedTarget(package_id=subject, cutoff_at=request.cutoff_at)
        # No cross-cutoff substitution when a specific cutoff was supplied.
        bundle = max(matches, key=lambda row: row.core.snapshot.cutoff_at)
        return ResolvedTarget(package_id=bundle.core.snapshot.package_id,
                              cutoff_at=bundle.core.snapshot.cutoff_at,
                              bundle=bundle.model_copy(deep=True))

    def collect(self, target: ResolvedTarget, request: AnalyzeRequest,
                analysis_run_id: UuidText) -> AnalysisBundle:
        if target.bundle is None:
            raise AnalysisInputError('reviewed_core_input_unavailable')
        return AnalysisBundle.model_validate(target.bundle.model_dump())
