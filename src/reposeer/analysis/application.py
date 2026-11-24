"""T-027 application boundary: identity → status → CORE → optional context → report."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from reposeer.agents import MultiAgentCoordinator, MultiAgentRunResult
from reposeer.analysis.agent_reporting import agent_request, report_agent_output
from reposeer.analysis.application_inputs import (
    AnalysisBackend,
    AnalysisBundle,
    AnalysisInputError,
    AnalyzeRequest,
    ResolvedTarget,
    StoredAnalysisBackend,
    parse_identifier,
)
from reposeer.analysis.core import CoreFeaturePackage, analyze_core
from reposeer.analysis.external import (
    EvidenceVerifier,
    ExternalContextResult,
    analyze_external_context,
)
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.analysis import (
    AnalysisCoverage,
    AnalysisEvidence,
    Applicability,
    DependencyImpact,
    FinalReport,
    ModelManifest,
    ModelResult,
    SourceProvenance,
    UnavailableDependencyImpact,
    validate_final_report,
)
from reposeer.contracts.identity import add_calendar_months, format_utc_timestamp
from reposeer.contracts.models import ContractModel, unique
from reposeer.maintenance.labels import HistoricalOfficialStatus
from reposeer.pipelines.maintenance import MaintenancePipeline

POLICY_VERSION = 'application-0.1.0'


class OfficialContext(ContractModel):
    """An independent D09 ledger handoff; specialist suggestions never enter this type."""

    statuses: list[HistoricalOfficialStatus] = Field(default_factory=list)
    sources: list[SourceProvenance] = Field(default_factory=list)
    evidence: list[AnalysisEvidence] = Field(default_factory=list)

    @model_validator(mode='after')
    def check(self):
        unique([row.ledger_revision for row in self.statuses], 'official ledger revisions')
        unique([row.source_id for row in self.sources], 'official source IDs')
        unique([row.evidence_id for row in self.evidence], 'official evidence IDs')
        return self


class OfficialStatusProvider(Protocol):
    def check(self, package_id: str, cutoff_at: str) -> OfficialContext: ...


class ImpactContext(ContractModel):
    impact: DependencyImpact
    sources: list[SourceProvenance] = Field(default_factory=list)


class EnhancedModel(Protocol):
    """A separately registered/frozen Model B. This interface never trains a model."""

    manifest: ModelManifest
    release_eligible: bool

    def compatible(self, core: CoreFeaturePackage) -> bool: ...

    def predict(self, core: CoreFeaturePackage, external: ExternalContextResult) -> ModelResult: ...


def merge_records(existing: list, additions: list, key: str) -> list:
    def identity(row):
        return row[key] if isinstance(row, dict) else getattr(row, key)

    def value(row):
        return row.model_dump() if isinstance(row, ContractModel) else row

    records = {identity(row): row for row in existing}
    for row in additions:
        row_id = identity(row)
        if row_id in records and value(records[row_id]) != value(row):
            raise ValueError('conflicting_' + key)
        records[row_id] = row
    return list(records.values())


class ApplicationOrchestrator:
    """One UI-callable interface. Optional stage failures preserve a valid CORE score."""

    def __init__(self, backend: AnalysisBackend, *, model_path: Path | None = None,
                 enhanced_model: EnhancedModel | None = None,
                 evidence_verifier: EvidenceVerifier | None = None,
                 agent_coordinator: MultiAgentCoordinator | None = None,
                 agent_factory: Callable[[], MultiAgentCoordinator] | None = None,
                 official_status_provider: OfficialStatusProvider | None = None,
                 impact_provider: Callable[[str, str], ImpactContext] | None = None,
                 reports_dir: Path | None = None, clock: Callable[[], datetime] | None = None):
        self.backend, self.model_path, self.enhanced_model = backend, model_path, enhanced_model
        self.evidence_verifier, self.agent_coordinator = evidence_verifier, agent_coordinator
        self.agent_factory, self.official_status_provider = agent_factory, official_status_provider
        self.impact_provider, self.reports_dir = impact_provider, reports_dir
        self.clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError('application_clock_requires_timezone')
        return format_utc_timestamp(value)

    def _base(self, request, package_id, cutoff, run_id, *, mapping=None, sources=()):
        return dict(
            schema_version='0.1.0', record_type='final_report', analysis_run_id=run_id,
            package_id=package_id, mapping=mapping, cutoff_at=cutoff, generated_at=self.now(),
            purpose=request.purpose, horizon_months=12, horizon_end_at=add_calendar_months(cutoff, 12),
            mode_requested=request.mode_requested, mode_used=None,
            enhanced_status='not_requested' if request.mode_requested == 'CORE' else 'not_run',
            fallback_reason=None, external_insights_status='not_requested', external_insights_reason=None,
            applicability=Applicability(status='unknown', policy_version=POLICY_VERSION,
                                        reasons=['not_scored'], limitations=[]),
            coverage=[], sources=list(sources), evidence=[], dependency_impact=None, backtest=None,
            limitations=[],
        )

    @staticmethod
    def _no_score(context, reason, *, status='insufficient_evidence'):
        if status == 'mapping_unresolved':
            context = {**context, 'mapping': None}
        return validate_final_report({**context, 'result_status': status,
                                      'reason_code': reason, 'reason': reason.replace('_', ' ') + '.'})

    def _official(self, bundle: AnalysisBundle | None, package_id: str, cutoff: str) -> OfficialContext:
        core = bundle.core if bundle else None
        context = OfficialContext(
            statuses=[core.official_status] if core and core.official_status else [],
            sources=core.sources if core else [], evidence=core.official_evidence if core else [],
        )
        if self.official_status_provider is not None:
            supplied = self.official_status_provider.check(package_id, cutoff)
            supplied = OfficialContext.model_validate(supplied.model_dump())
            context = OfficialContext(
                statuses=merge_records(context.statuses, supplied.statuses, 'ledger_revision'),
                sources=merge_records(context.sources, supplied.sources, 'source_id'),
                evidence=merge_records(context.evidence, supplied.evidence, 'evidence_id'),
            )
        return context

    def _known_report(self, context: dict, official: OfficialContext) -> FinalReport | None:
        status, event, within = MaintenancePipeline().evaluate_as_of_status(
            cutoff_at=context['cutoff_at'],
            official_events=sorted(official.statuses, key=lambda row: row.ledger_revision),
        )
        if event is None:
            return None
        sources = {row.source_id: row for row in official.sources}
        evidence = {row.evidence_id: row for row in official.evidence}
        for key in event.evidence_ids:
            row = evidence[key]
            if row.package_id != context['package_id'] or row.use != 'official_status':
                raise ValueError('official_evidence_package_or_use_mismatch')
            for citation in row.citations:
                sources[citation.source_id].require_as_of(event.available_at)
        updated = {key: value for key, value in context.items() if key not in {
            'prediction', 'core_prediction', 'reason_code', 'reason', 'official_status', 'within_horizon',
        }}
        updated.update(
            result_status=status, mode_used=None,
            enhanced_status='not_requested' if context['mode_requested'] == 'CORE' else 'not_run',
            fallback_reason=None, official_status=event.model_dump(exclude={'available_at'}),
            applicability=Applicability(status='unknown', policy_version=POLICY_VERSION,
                                        reasons=[status], limitations=[]),
            sources=merge_records(context['sources'], official.sources, 'source_id'),
            evidence=merge_records(context['evidence'], [evidence[key] for key in event.evidence_ids], 'evidence_id'),
            generated_at=self.now(),
        )
        if status == 'scheduled_cessation':
            updated['within_horizon'] = within
        return validate_final_report(updated)

    def _model_b_reason(self, core: CoreFeaturePackage, request: AnalyzeRequest) -> str | None:
        model = self.enhanced_model
        if model is None:
            return 'model_b_unavailable'
        try:
            manifest = ModelManifest.model_validate(model.manifest.model_dump())
            if model.release_eligible is not True:
                return 'model_b_release_gates_not_met'
            if (manifest.feature_set not in {'E1', 'E2'}
                    or manifest.feature_schema_version != core.snapshot.schema_version
                    or model.compatible(core.model_copy(deep=True)) is not True):
                return 'model_b_incompatible'
            if manifest.frozen_at > self.now() or (request.purpose == 'current_inference'
                                                  and manifest.frozen_at > core.snapshot.cutoff_at):
                return 'model_b_unavailable_at_decision'
        except Exception:
            return 'model_b_registration_invalid'
        return None

    def _optional_context(self, report: FinalReport, bundle: AnalysisBundle,
                          request: AnalyzeRequest, wants_external: bool):
        data, external = report.model_dump(), None
        if not wants_external:
            return data, external
        if bundle.external is None:
            data.update(external_insights_status='unavailable', external_insights_reason='external_input_unavailable')
        else:
            try:
                external = analyze_external_context(bundle.external.model_copy(deep=True),
                                                    verifier=self.evidence_verifier)
                data['sources'] = merge_records(report.sources, external.sources, 'source_id')
                data['evidence'] = merge_records(report.evidence, external.evidence, 'evidence_id')
                data['coverage'] = merge_records(report.coverage, external.coverage, 'coverage_id')
                data.update(external_insights_status=external.external_insights_status,
                            external_insights_reason=external.external_insights_reason)
                data['limitations'].extend(external.limitations)
                # Validate this stage before allowing an optional failure to touch CORE lineage.
                validate_final_report(data)
            except Exception:
                data, external = report.model_dump(), None
                data.update(external_insights_status='failed', external_insights_reason='external_analysis_failed')
        if request.include_agents:
            before = validate_final_report(data).model_dump()
            try:
                coordinator = self.agent_coordinator or (self.agent_factory() if self.agent_factory else None)
                if coordinator is None:
                    raise ValueError('agent_provider_unavailable')
                child = agent_request(bundle, report.analysis_run_id)
                result = coordinator.analyze(child)
                result = MultiAgentRunResult.model_validate(result.model_dump())
                all_sources = merge_records(
                    [SourceProvenance.model_validate(row) if isinstance(row, dict) else row
                     for row in data['sources']], bundle.agent_sources, 'source_id')
                evidence, coverage, omitted = report_agent_output(
                    bundle, child, result, {row.source_id: row for row in all_sources})
                used = {c.source_id for item in evidence for c in item.citations}
                original_sources = {row['source_id'] if isinstance(row, dict) else row.source_id
                                    for row in data['sources']}
                data['sources'] = [row for row in all_sources if row.source_id in used | original_sources]
                data['evidence'] = merge_records(
                    [AnalysisEvidence.model_validate(row) if isinstance(row, dict) else row
                     for row in data['evidence']], evidence, 'evidence_id')
                data['coverage'] = merge_records(
                    [AnalysisCoverage.model_validate(row) if isinstance(row, dict) else row
                     for row in data['coverage']], [coverage], 'coverage_id')
                data.update(external_insights_status='partial', external_insights_reason='agent_output_report_only')
                data['limitations'].extend(result.limitations)
                if omitted:
                    data['limitations'].append(f'{omitted} agent findings omitted because exact report source/offset lineage is unavailable.')
                if result.overall_status == 'failed':
                    data['external_insights_reason'] = 'agent_analysis_failed'
                validate_final_report(data)
            except Exception:
                data = before
                data.update(external_insights_status='partial' if data['evidence'] else 'failed',
                            external_insights_reason='agent_analysis_unavailable_or_invalid')
        return data, external

    def _enhance(self, data, core, request, external, preflight_reason):
        if data['result_status'] != 'predicted' or request.mode_requested != 'ENHANCED':
            return data
        reason = preflight_reason
        if reason is None:
            if (external is None or external.external_insights_status != 'complete'
                    or not external.e1 or any(row.count is None for row in external.e1)):
                reason = 'external_coverage_insufficient'
            elif self.enhanced_model.manifest.feature_set == 'E2' and (
                not external.e2 or any(row.count is None for row in external.e2)
            ):
                reason = 'agent_processing_insufficient'
        if reason is None:
            try:
                prediction = self.enhanced_model.predict(core.model_copy(deep=True), external.model_copy(deep=True))
                prediction = ModelResult.model_validate(prediction.model_dump())
                if prediction.model != self.enhanced_model.manifest:
                    raise ValueError('enhanced_model_manifest_mismatch')
                enhanced = {**data, 'mode_used': 'ENHANCED', 'enhanced_status': 'used', 'fallback_reason': None,
                            'core_prediction': data['prediction'], 'prediction': prediction,
                            'generated_at': self.now()}
                return validate_final_report(enhanced).model_dump()
            except Exception:
                reason = 'model_b_inference_or_contract_failed'
        return {**data, 'mode_used': 'CORE', 'enhanced_status': 'fallback', 'fallback_reason': reason}

    def _finish(self, report: FinalReport, request: AnalyzeRequest, bundle: AnalysisBundle | None):
        data = report.model_dump()
        if request.include_dependency_impact:
            try:
                if self.impact_provider:
                    context = self.impact_provider(report.package_id, report.cutoff_at)
                elif bundle and bundle.dependency_impact:
                    context = ImpactContext(impact=bundle.dependency_impact, sources=bundle.impact_sources)
                else:
                    context = ImpactContext(impact=UnavailableDependencyImpact(
                        impact_status='unavailable', missing_reason='dependency_corpus_unavailable'))
                context = ImpactContext.model_validate(context.model_dump())
                data['dependency_impact'] = context.impact
                data['sources'] = merge_records(report.sources, context.sources, 'source_id')
                data['generated_at'] = self.now()
                validate_final_report(data)
            except Exception:
                data = report.model_dump()
                data['dependency_impact'] = UnavailableDependencyImpact(impact_status='unavailable', missing_reason='dependency_impact_failed_or_invalid')
        data['generated_at'] = self.now()
        data['limitations'] = list(dict.fromkeys(data['limitations']))
        report = validate_final_report(data)
        if self.reports_dir is not None:
            try:
                write_json_atomic(Path(self.reports_dir) / f'{report.analysis_run_id}.json', report.model_dump(mode='json'))
            except OSError:
                data = report.model_dump()
                data['limitations'].append('report_persistence_failed; the validated report is returned to the caller.')
                report = validate_final_report(data)
        return report

    def analyze_repository(self, identifier: str, **options) -> FinalReport:
        request = AnalyzeRequest(identifier=identifier, **options)
        if request.cutoff_at is not None and request.cutoff_at > self.now():
            raise AnalysisInputError('cutoff_cannot_be_in_the_future')
        target = self.backend.resolve(request.model_copy(deep=True))
        # Revalidate dependency output rather than trusting mutable Pydantic instances.
        target = ResolvedTarget.model_validate(target.model_dump())
        requested_package, _ = parse_identifier(request.package_name or request.identifier)
        if requested_package is not None and requested_package != target.package_id:
            raise AnalysisInputError('resolved_package_mismatch')
        cutoff, run_id = target.cutoff_at or self.now(), str(uuid4())
        if cutoff > self.now():
            raise AnalysisInputError('cutoff_cannot_be_in_the_future')
        mapping = target.bundle.core.mapping if target.bundle else None
        base = self._base(request, target.package_id, cutoff, run_id, mapping=mapping,
                          sources=target.bundle.core.sources if target.bundle else [])
        try:
            official = self._official(target.bundle, target.package_id, cutoff)
            known = self._known_report(base, official)
        except Exception:
            return self._finish(self._no_score(base, 'official_status_check_failed', status='failed'), request, None)
        if known is not None:
            return self._finish(known, request, target.bundle)
        try:
            registered_b = False
            if self.enhanced_model is not None:
                try:
                    manifest = ModelManifest.model_validate(self.enhanced_model.manifest.model_dump())
                    registered_b = self.enhanced_model.release_eligible is True and manifest.feature_set in {'E1', 'E2'}
                except Exception:
                    pass
            # An unregistered Model B never triggers external collection just by selecting ENHANCED.
            collection_request = request.model_copy(update={'include_external_insights': (
                request.include_external_insights or request.include_agents
                or (request.mode_requested == 'ENHANCED' and registered_b))})
            bundle = self.backend.collect(target.model_copy(deep=True), collection_request, run_id)
            bundle = AnalysisBundle.model_validate(bundle.model_dump())
            core = bundle.core
            if core.snapshot.package_id != target.package_id or (
                target.cutoff_at is not None and core.snapshot.cutoff_at != target.cutoff_at
            ):
                raise ValueError('collected_package_or_cutoff_mismatch')
            collected_base = self._base(request, target.package_id, core.snapshot.cutoff_at, run_id,
                                        mapping=core.mapping, sources=core.sources)
            known = self._known_report(collected_base, self._official(bundle, target.package_id, core.snapshot.cutoff_at))
            if known is not None:
                return self._finish(known, request, bundle)
            report = analyze_core(core, self.model_path or Path('__no_registered_model_a__'),
                                  mode_requested=request.mode_requested, purpose=request.purpose,
                                  clock=self.clock, analysis_run_id=run_id)
        except AnalysisInputError as error:
            codes = {'package_repository_mapping_unresolved', 'requested_repository_does_not_match_package'}
            allowed = codes | {'reviewed_core_input_unavailable', 'core_features_unavailable',
                               'unsupported_repository', 'collection_stage_failed', 'snapshot_raw_lineage_unavailable'}
            reason = str(error) if str(error) in allowed else 'collection_failed'
            status = ('mapping_unresolved' if reason in codes else 'unsupported' if reason == 'unsupported_repository'
                      else 'failed' if reason in {'collection_stage_failed', 'collection_failed'} else 'insufficient_evidence')
            return self._finish(self._no_score(base, reason, status=status), request, None)
        except Exception:
            return self._finish(self._no_score(base, 'collection_or_core_analysis_failed', status='failed'), request, None)
        if report.result_status in {'already_abandoned', 'scheduled_cessation'}:
            return self._finish(report, request, bundle)
        reason = (self._model_b_reason(core, request) if request.mode_requested == 'ENHANCED'
                  and report.result_status == 'predicted' else None)
        wants_external = (request.include_external_insights or request.include_agents
                          or (request.mode_requested == 'ENHANCED' and reason is None and report.result_status == 'predicted'))
        data, external = self._optional_context(report, bundle, request, wants_external)
        # Recheck only an independent ledger after optional stages; agents cannot set official status.
        try:
            known = self._known_report(data, self._official(bundle, target.package_id, core.snapshot.cutoff_at))
        except Exception:
            failure_base = self._base(request, target.package_id, core.snapshot.cutoff_at, run_id,
                                      mapping=core.mapping, sources=core.sources)
            return self._finish(self._no_score(failure_base, 'official_status_recheck_failed', status='failed'), request, bundle)
        if known is not None:
            return self._finish(known, request, bundle)
        data = self._enhance(data, core, request, external, reason)
        return self._finish(validate_final_report(data), request, bundle)


def analyze_repository(identifier: str, *, model_path: Path | None = None,
                       data_dir: Path = Path('data'), backend: AnalysisBackend | None = None,
                       **options) -> FinalReport:
    """Safe default: read explicit local bundles and save one validated final report."""
    backend = backend or StoredAnalysisBackend.from_files(sorted((Path(data_dir) / 'analysis_inputs').glob('*.json')))
    return ApplicationOrchestrator(backend, model_path=model_path,
                                   reports_dir=Path(data_dir) / 'reports/analysis').analyze_repository(identifier, **options)
