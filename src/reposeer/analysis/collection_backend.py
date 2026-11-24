"""T-020/T-022 adapter. Current collection never certifies historical coverage."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from reposeer.analysis.application_inputs import (
    AnalysisBundle,
    AnalysisInputError,
    AnalyzeRequest,
    ResolvedTarget,
    parse_identifier,
)
from reposeer.analysis.core import CoreFeaturePackage
from reposeer.analysis.external import ExternalContextRequest, ExternalDocument
from reposeer.collection.persistence import write_json_atomic
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.contracts.analysis import PackageMapping, SourceProvenance
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.contracts.models import RawRecord
from reposeer.pipelines.collection import CollectionPipeline
from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.services.feature_service import FeatureService
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage


def observation_source(raw: RawRecord) -> SourceProvenance:
    digest = hashlib.sha256(json.dumps(raw.payload, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode()).hexdigest()
    return SourceProvenance(
        source_id='source:' + raw.raw_record_id, source=raw.source, source_url=raw.source_url,
        source_record_id=raw.source_record_id, revision=digest, content_sha256=digest,
        query_set_hash=None, retrieved_at=raw.retrieved_at, valid_at=raw.retrieved_at,
        available_at=raw.retrieved_at, temporal_basis='observation',
        raw_record_ids=[raw.raw_record_id], missing_fields=[],
    )


class CollectionAnalysisBackend:
    """Explicit live acquisition in a separate workspace for each application run."""

    def __init__(self, data_dir: Path, *, collection_factory=CollectionPipeline,
                 feature_factory=FeatureService, clock: Callable[[], datetime] | None = None):
        self.data_dir = Path(data_dir)
        self.collection_factory, self.feature_factory = collection_factory, feature_factory
        self.clock = clock or (lambda: datetime.now(UTC))

    def resolve(self, request: AnalyzeRequest) -> ResolvedTarget:
        subject, repository = parse_identifier(request.identifier)
        if repository is not None:
            if request.package_name is None:
                raise AnalysisInputError('github_package_selection_required')
            subject, _ = parse_identifier(request.package_name)
        return ResolvedTarget(package_id=subject, cutoff_at=request.cutoff_at)

    def collect(self, target: ResolvedTarget, request: AnalyzeRequest,
                analysis_run_id: str) -> AnalysisBundle:
        paths = StoragePaths(self.data_dir / 'analysis_runs' / analysis_run_id)
        wants_external = request.include_external_insights or request.include_agents
        result = self.collection_factory(paths, clock=self.clock).run(
            [target.package_id], mode='live', max_pages=2, max_records=200,
            discovery_max_pages=1, discovery_max_records=30, max_candidates=12,
            available_external_sources=None if wants_external else [],
        )
        manifest = json.loads(result.manifest_path.read_bytes())
        entry = next((row for row in manifest['targets']
                      if row['package_id'] == target.package_id), None)
        if entry is None or entry.get('mapping_status') != 'verified_current':
            if entry and entry.get('github_metadata_status') == 'excluded':
                raise AnalysisInputError('unsupported_repository')
            if entry and entry.get('github_metadata_status') == 'failed':
                raise AnalysisInputError('collection_stage_failed')
            if any(error['stage'] in {'package_resolution', 'pypi_collection', 'github_collection', 'identity_resolution'}
                   for error in manifest.get('errors', [])):
                raise AnalysisInputError('collection_stage_failed')
            raise AnalysisInputError('package_repository_mapping_unresolved')
        store = JsonlStorage()
        packages = store.read_contract_records(paths.raw_dir / 'pypi/packages.jsonl')
        repositories = store.read_contract_records(paths.raw_dir / 'github/repositories.jsonl')
        pypi = next(row for row in packages if row.raw_record_id in entry['mapping_evidence_raw_ids'])
        github = next(row for row in repositories if row.raw_record_id == entry['repository_raw_record_id'])
        scope = RepositoryScopeEvidence(pypi=pypi, github=github)
        resolved = scope.resolve()
        _, requested_url = parse_identifier(request.identifier)
        if requested_url is not None and requested_url != resolved['repository_url']:
            raise AnalysisInputError('requested_repository_does_not_match_package')
        cutoff = target.cutoff_at or format_utc_timestamp(self.clock())
        mapping_sources = [observation_source(row) for row in (pypi, github)]
        mapping = PackageMapping(
            repo_id=resolved['repo_id'], repo_group_id=resolved['repo_id'],
            mapping_revision='observed:' + ':'.join(row.revision for row in mapping_sources),
            scope='repository', scope_quality='repository_only',
            valid_from=resolved['observed_at'], valid_to=None, available_at=resolved['observed_at'],
            source_ids=[row.source_id for row in mapping_sources],
        )
        built = self.feature_factory(paths=paths).build_from_processed(
            targets=[(target.package_id, resolved['repo_id'], cutoff)], persist=True,
        )
        if len(built) != 1 or built[0].snapshot is None:
            raise AnalysisInputError('core_features_unavailable')
        snapshot = built[0].snapshot
        raws = {row.raw_record_id: row for row in (pypi, github)}
        groups = {}
        for group in ('commits', 'contributors', 'issues', 'pull_requests', 'releases'):
            path = paths.raw_dir / 'github' / f'{group}.jsonl'
            rows = store.read_contract_records(path) if path.is_file() else []
            rows = [row for row in rows if row.repo_id == mapping.repo_id]
            raws.update({row.raw_record_id: row for row in rows})
            groups[group] = rows
        if not set(snapshot.raw_record_ids) <= raws.keys():
            raise AnalysisInputError('snapshot_raw_lineage_unavailable')
        sources = {row.raw_record_id: observation_source(row) for row in raws.values()}
        coverage_refs = {
            row.data_group: [sources[raw.raw_record_id].source_id for raw in groups[row.data_group]]
            for row in snapshot.coverage
            if row.status in {'complete', 'partial'} and groups.get(row.data_group)
        }
        core = CoreFeaturePackage(snapshot=snapshot, mapping=mapping,
                                  sources=list(sources.values()), core_coverage_sources=coverage_refs)
        documents_path = paths.raw_dir / 'external/documents.jsonl'
        external = None
        if wants_external and documents_path.is_file():
            normalized = NormalizationPipeline().process(
                store.read_contract_records(documents_path), targets=[VerifiedTargetContext.from_scope(scope)])
            documents = {}
            for article in normalized.canonical_articles:
                assessment = next((row for row in normalized.relevance_assessments
                                   if row.article_id == article.article_id
                                   and row.content_sha256 == article.content_sha256
                                   and row.raw_record_ids == article.provenance.raw_record_ids), None)
                if assessment is not None:
                    document = ExternalDocument(article=article, relevance=assessment,
                                                provider=article.provenance.source,
                                                query_intent='collected_context', query_set_hash=None)
                    documents[document.source_id] = document
            external = ExternalContextRequest(
                package_id=target.package_id, cutoff_at=cutoff,
                generated_at=format_utc_timestamp(self.clock()), documents=list(documents.values()),
                censuses=[], agent_events=[], agent_coverage=[],
            )
        bundle = AnalysisBundle(core=core, mapping_evidence=[pypi, github], external=external)
        write_json_atomic(paths.metadata_dir / 'analysis_bundle.json', bundle.model_dump(mode='json'))
        return bundle
