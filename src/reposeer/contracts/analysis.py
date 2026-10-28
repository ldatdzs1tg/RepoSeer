"""Shared PyPI analysis boundary. No inference, agent or graph execution lives here.

Generate JSON Schema and TypeScript with scripts/export_analysis_contract.py. The wire
union forbids prediction fields entirely on known-status and no-score reports.
"""

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator

from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    Confidence,
    ContractModel,
    Count,
    NonEmpty,
    PackageId,
    Provenance,
    RepoId,
    Sha256,
    TimeRange,
    UtcTimestamp,
    UuidText,
    _checked_horizon,
    unique,
)

ANALYSIS_SCHEMA_VERSION = "0.1.0"
type AnalysisMode = Literal["CORE", "ENHANCED"]
type AnalysisPurpose = Literal[
    "current_inference", "retrospective_analysis", "prospective_backtest"
]
type NoScoreStatus = Literal[
    "mapping_unresolved", "mapping_history_unverified", "unsupported", "insufficient_evidence",
    "failed",
]
SourceIds = Annotated[list[NonEmpty], Field(min_length=1)]


class SourceProvenance(Provenance):
    """A particular observed source revision, not just a mutable URL."""

    source_id: NonEmpty
    source_record_id: NonEmpty | None
    revision: NonEmpty | None
    content_sha256: Sha256 | None
    query_set_hash: Sha256 | None

    def require_as_of(self, cutoff_at: str) -> None:
        if self.available_at is None or self.available_at > cutoff_at or self.valid_at > cutoff_at:
            raise ValueError("used source requires known availability and validity at cutoff")
        if self.revision is None and self.content_sha256 is None:
            raise ValueError("used source requires a revision or content hash")


class PackageMapping(ContractModel):
    repo_id: RepoId
    repo_group_id: RepoId
    mapping_revision: NonEmpty
    scope: NonEmpty
    scope_quality: Literal["package_scoped", "repository_only"]
    valid_from: UtcTimestamp
    valid_to: UtcTimestamp | None
    available_at: UtcTimestamp
    source_ids: SourceIds

    @model_validator(mode="after")
    def check(self):
        unique(self.source_ids, "mapping.source_ids")
        if self.repo_group_id != self.repo_id:
            raise ValueError("repo_group_id must equal the stable repo_id")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("mapping validity must be a nonempty [valid_from, valid_to) interval")
        return self


class AnalysisCoverage(ContractModel):
    """Execution completeness, source census and temporal validity are distinct."""

    coverage_id: NonEmpty
    stage: Literal["core", "external", "agent", "impact"]
    source_ids: list[NonEmpty]
    status: Literal["complete", "partial", "unavailable", "not_requested"]
    source_coverage: Literal["complete", "partial", "unknown"]
    historical_validity: Literal["verified", "unverified", "not_applicable"]
    window: TimeRange | None
    records_observed: Count | None
    eligible_documents: Count | None
    processed_documents: Count | None
    missing_reason: NonEmpty | None

    @model_validator(mode="after")
    def check(self):
        unique(self.source_ids, "coverage.source_ids")
        if (self.status == "complete") != (self.missing_reason is None):
            raise ValueError("non-complete coverage requires a missing reason")
        if self.status in {"unavailable", "not_requested"}:
            if any(x is not None for x in (
                self.records_observed, self.eligible_documents, self.processed_documents
            )):
                raise ValueError("unavailable/not-requested counts must be null, not zero")
            if self.source_coverage == "complete":
                raise ValueError("unavailable sources cannot have a complete census")
        if self.records_observed == 0 and self.source_coverage != "complete":
            raise ValueError("zero observations require a complete source census")
        if self.processed_documents is not None:
            if self.eligible_documents is None or self.processed_documents > self.eligible_documents:
                raise ValueError("processed documents require an eligible denominator")
        if self.stage == "agent" and self.status == "complete":
            if self.eligible_documents is None or self.processed_documents != self.eligible_documents:
                raise ValueError("complete agent coverage requires all eligible documents processed")
        return self


class EvidenceCitation(ContractModel):
    source_id: NonEmpty
    quote: NonEmpty
    start_offset: Count
    end_offset: Count

    @model_validator(mode="after")
    def check(self):
        if self.end_offset - self.start_offset != len(self.quote):
            raise ValueError("citation offsets must match the quoted Unicode code points")
        return self


class AgentProvenance(ContractModel):
    agent_id: NonEmpty
    agent_version: NonEmpty
    model_version: NonEmpty
    prompt_version: NonEmpty


class AnalysisEvidence(ContractModel):
    """Grounded evidence/signal, never a label or a risk probability."""

    evidence_id: NonEmpty
    package_id: PackageId
    origin: Literal["official", "external", "agent"]
    context: Literal["direct_package", "same_function", "alternative"]
    signal_type: Literal["maintenance", "functional_demand", "migration", "context"]
    claim: NonEmpty
    event_at: UtcTimestamp | None
    citations: Annotated[list[EvidenceCitation], Field(min_length=1)]
    use: Literal["feature", "official_status", "report_only"]
    exclusion_reason: NonEmpty | None
    uncertainty: NonEmpty | None
    agent: AgentProvenance | None

    @model_validator(mode="after")
    def check(self):
        if (self.origin == "agent") != (self.agent is not None):
            raise ValueError("only agent evidence requires agent provenance")
        if (self.use == "report_only") != (self.exclusion_reason is not None):
            raise ValueError("report-only evidence requires an exclusion reason")
        if self.use == "official_status" and (
            self.origin != "official" or self.context != "direct_package"
        ):
            raise ValueError("official status requires independently verified direct official evidence")
        return self


class OfficialStatus(ContractModel):
    """Independently verified as-of ledger revision; an archive flag alone is insufficient."""

    event_type: Literal["ARCHIVED", "MAINTENANCE_CEASED", "REPLACED"]
    cessation_confirmed: bool
    announced_at: UtcTimestamp
    effective_at: UtcTimestamp
    verified_at: UtcTimestamp
    revoked_at: UtcTimestamp | None
    ledger_revision: NonEmpty
    verifier: NonEmpty
    verification_policy_version: NonEmpty
    evidence_ids: SourceIds

    @model_validator(mode="after")
    def check(self):
        unique(self.evidence_ids, "official_status.evidence_ids")
        if self.announced_at > self.verified_at:
            raise ValueError("official announcement cannot follow verification")
        if self.cessation_confirmed and self.effective_at > self.verified_at:
            raise ValueError("cessation cannot be confirmed before it takes effect")
        if self.revoked_at is not None and self.revoked_at < self.announced_at:
            raise ValueError("revocation cannot precede announcement")
        return self


class ModelManifest(ContractModel):
    model_id: NonEmpty
    model_version: NonEmpty
    artifact_sha256: Sha256
    feature_set: Literal["E0", "E1", "E2"]
    feature_schema_version: NonEmpty
    preprocessing_version: NonEmpty
    calibration_version: NonEmpty
    risk_threshold_version: NonEmpty
    training_population: NonEmpty
    calibration_population: NonEmpty
    trained_through_at: UtcTimestamp
    frozen_at: UtcTimestamp

    @model_validator(mode="after")
    def check(self):
        if self.trained_through_at > self.frozen_at:
            raise ValueError("model freeze must follow training data")
        return self


class FeatureAttribution(ContractModel):
    feature: NonEmpty
    value: float | None
    contribution: float


class ModelExplanation(ContractModel):
    method: Literal["shap"]
    model_version: NonEmpty
    output_space: Literal["raw_margin", "probability"]
    base_value: float
    attributions: Annotated[list[FeatureAttribution], Field(min_length=1)]

    @model_validator(mode="after")
    def check(self):
        unique([item.feature for item in self.attributions], "attributions.feature")
        return self


class ModelResult(ContractModel):
    """A frozen calibrated model's output; only embeddable in a predicted report."""

    result_status: Literal["predicted"]
    probability: Confidence
    risk_level: Literal["low", "moderate", "high"]
    model: ModelManifest
    feature_source_ids: SourceIds
    feature_evidence_ids: list[NonEmpty]
    explanation: ModelExplanation | None

    @model_validator(mode="after")
    def check(self):
        unique(self.feature_source_ids, "model.feature_source_ids")
        unique(self.feature_evidence_ids, "model.feature_evidence_ids")
        if self.explanation and self.explanation.model_version != self.model.model_version:
            raise ValueError("explanation must belong to this model version")
        return self


class Applicability(ContractModel):
    status: Literal["in_domain", "out_of_distribution", "unknown"]
    policy_version: NonEmpty
    reasons: list[NonEmpty]
    limitations: list[NonEmpty]

    @model_validator(mode="after")
    def check(self):
        if self.status != "in_domain" and not self.reasons:
            raise ValueError("unknown/OOD applicability requires reasons")
        return self


class AvailableDependencyImpact(ContractModel):
    impact_status: Literal["available", "partial"]
    graph_kind: Literal["observed_potential", "snapshot_resolved"]
    graph_as_of: UtcTimestamp
    corpus_id: NonEmpty
    corpus_package_count: Annotated[Count, Field(ge=1)]
    target_version: NonEmpty | None
    environment: NonEmpty | None
    resolver_version: NonEmpty | None
    direct_dependents: Count
    transitive_dependents: Count
    counts_are_lower_bounds: bool
    source_ids: SourceIds
    missing_reason: NonEmpty | None

    @model_validator(mode="after")
    def check(self):
        unique(self.source_ids, "impact.source_ids")
        if not self.direct_dependents <= self.transitive_dependents <= self.corpus_package_count:
            raise ValueError("impact counts must be unique corpus members; transitive includes direct")
        if self.counts_are_lower_bounds != (self.impact_status == "partial"):
            raise ValueError("partial impact counts must be marked as lower bounds")
        if (self.impact_status == "available") != (self.missing_reason is None):
            raise ValueError("partial impact requires a missing reason")
        resolution = (self.target_version, self.environment, self.resolver_version)
        if self.graph_kind == "snapshot_resolved" and any(x is None for x in resolution):
            raise ValueError("resolved impact requires target version, environment and resolver")
        return self


class UnavailableDependencyImpact(ContractModel):
    impact_status: Literal["unavailable"]
    missing_reason: NonEmpty


type DependencyImpact = Annotated[
    AvailableDependencyImpact | UnavailableDependencyImpact, Field(discriminator="impact_status")
]


class BacktestProvenance(ContractModel):
    evaluation_id: NonEmpty
    fold_id: NonEmpty
    test_window: TimeRange
    manifest_sha256: Sha256


class ReportContext(ContractModel):
    schema_version: Literal["0.1.0"]
    record_type: Literal["final_report"]
    analysis_run_id: UuidText
    package_id: PackageId
    mapping: PackageMapping | None
    cutoff_at: UtcTimestamp
    generated_at: UtcTimestamp
    purpose: AnalysisPurpose
    horizon_months: Annotated[Literal[12], BeforeValidator(_checked_horizon)]
    horizon_end_at: UtcTimestamp
    mode_requested: AnalysisMode
    enhanced_status: Literal["not_requested", "used", "fallback", "not_run"]
    fallback_reason: NonEmpty | None
    external_insights_status: Literal["not_requested", "complete", "partial", "unavailable", "failed"]
    external_insights_reason: NonEmpty | None
    applicability: Applicability
    coverage: list[AnalysisCoverage]
    sources: list[SourceProvenance]
    evidence: list[AnalysisEvidence]
    dependency_impact: DependencyImpact | None
    backtest: BacktestProvenance | None
    limitations: list[NonEmpty]

    @model_validator(mode="after")
    def check_context(self):
        if self.generated_at < self.cutoff_at:
            raise ValueError("report generation cannot precede cutoff")
        if self.horizon_end_at != add_calendar_months(self.cutoff_at, 12):
            raise ValueError("horizon must be exactly 12 calendar months after cutoff")
        if (self.purpose == "prospective_backtest") != (self.backtest is not None):
            raise ValueError("only D10 prospective backtests require evaluation provenance")
        if self.backtest and not (
            self.backtest.test_window.start_at < self.cutoff_at <= self.backtest.test_window.end_at
        ):
            raise ValueError("backtest cutoff must be inside its (start, end] test interval")
        if (self.enhanced_status == "fallback") != (self.fallback_reason is not None):
            raise ValueError("fallback requires a reason, other execution states prohibit one")
        if self.mode_requested == "CORE" and self.enhanced_status != "not_requested":
            raise ValueError("CORE cannot request an enhanced execution")
        if self.mode_requested == "ENHANCED" and self.enhanced_status == "not_requested":
            raise ValueError("requested ENHANCED must record its outcome")
        needs_reason = self.external_insights_status in {"partial", "unavailable", "failed"}
        if needs_reason != (self.external_insights_reason is not None):
            raise ValueError("partial/unavailable/failed external insights require a reason")
        unique([s.source_id for s in self.sources], "sources.source_id")
        unique([e.evidence_id for e in self.evidence], "evidence.evidence_id")
        unique([c.coverage_id for c in self.coverage], "coverage.coverage_id")
        for source in self.sources:
            if source.retrieved_at > self.generated_at:
                raise ValueError("source retrieval cannot follow report generation")
        if self.mapping:
            self.require_sources(self.mapping.source_ids)
        for coverage in self.coverage:
            self.require_sources(coverage.source_ids)
            if coverage.historical_validity == "verified":
                if not coverage.source_ids:
                    raise ValueError("verified historical coverage requires source provenance")
                self.require_sources(coverage.source_ids, as_of=True)
                if coverage.window and coverage.window.end_at > self.cutoff_at:
                    raise ValueError("historical coverage cannot extend past cutoff")
        for evidence in self.evidence:
            if evidence.package_id != self.package_id:
                raise ValueError("evidence must be attributed to the report's package")
            self.require_sources(
                [c.source_id for c in evidence.citations], as_of=evidence.use != "report_only"
            )
            if evidence.use == "feature" and (
                evidence.event_at is not None and evidence.event_at > self.cutoff_at
            ):
                raise ValueError("future events are not predictive features")
        if self.external_insights_status == "not_requested" and any(
            evidence.origin in {"external", "agent"} for evidence in self.evidence
        ):
            raise ValueError("external/agent evidence requires an explicit insights outcome")
        if isinstance(self.dependency_impact, AvailableDependencyImpact):
            if self.dependency_impact.graph_as_of > self.cutoff_at:
                raise ValueError("impact graph cannot be after cutoff")
            self.require_sources(self.dependency_impact.source_ids, as_of=True)
            for source in self.sources:
                if source.source_id in self.dependency_impact.source_ids:
                    source.require_as_of(self.dependency_impact.graph_as_of)
        return self

    def require_sources(self, source_ids: list[str], *, as_of: bool = False) -> None:
        sources = {source.source_id: source for source in self.sources}
        for source_id in source_ids:
            if source_id not in sources:
                raise ValueError(f"orphan source reference: {source_id}")
            if as_of:
                sources[source_id].require_as_of(self.cutoff_at)

    def check_official_status(self, status: OfficialStatus) -> None:
        if status.announced_at > self.cutoff_at or status.verified_at > self.generated_at:
            raise ValueError("official status must be announced by cutoff and verified by generation")
        if status.revoked_at is not None and status.revoked_at <= self.cutoff_at:
            raise ValueError("revoked official status cannot override an as-of result")
        if status.revoked_at is not None and status.revoked_at > self.generated_at:
            raise ValueError("revocation cannot be known before it occurs")
        evidence = {item.evidence_id: item for item in self.evidence}
        for evidence_id in status.evidence_ids:
            item = evidence.get(evidence_id)
            if item is None or item.use != "official_status":
                raise ValueError("known status requires official evidence references")


class PredictedReport(ReportContext):
    result_status: Literal["predicted"]
    mode_used: AnalysisMode
    prediction: ModelResult
    core_prediction: ModelResult | None

    @model_validator(mode="after")
    def check_prediction(self):
        if self.mapping is None or self.applicability.status != "in_domain":
            raise ValueError("prediction requires verified mapping and in-domain applicability")
        mapping = self.mapping
        if mapping.available_at > self.cutoff_at or mapping.valid_from > self.cutoff_at or (
            mapping.valid_to is not None and mapping.valid_to <= self.cutoff_at
        ):
            raise ValueError("prediction mapping must be valid and available at cutoff")
        self.require_sources(mapping.source_ids, as_of=True)
        if any(e.use == "official_status" for e in self.evidence):
            raise ValueError("verified official status takes precedence; remove all predictions")
        expected_enhanced = "not_requested" if self.mode_requested == "CORE" else (
            "used" if self.mode_used == "ENHANCED" else "fallback"
        )
        if self.enhanced_status != expected_enhanced:
            raise ValueError("prediction mode and enhanced execution disagree")
        if self.mode_used == "ENHANCED":
            if self.mode_requested != "ENHANCED" or self.prediction.model.feature_set == "E0":
                raise ValueError("ENHANCED requires an explicitly requested E1/E2 model")
            if self.core_prediction is None or self.core_prediction.model.feature_set != "E0":
                raise ValueError("ENHANCED must retain its independent CORE result")
            if self.external_insights_status not in {"complete", "partial"}:
                raise ValueError("ENHANCED requires complete or partial external insights")
            core_sources = set(self.core_prediction.feature_source_ids)
            selected_sources = set(self.prediction.feature_source_ids)
            external_sources = selected_sources - core_sources
            if not core_sources <= selected_sources or not external_sources:
                raise ValueError("ENHANCED requires CORE inputs plus external source lineage")
            sources_by_id = {source.source_id: source for source in self.sources}
            complete_external = set()
            for coverage in self.coverage:
                if not (
                    coverage.stage == "external" and coverage.status == "complete"
                    and coverage.source_coverage == "complete"
                    and coverage.historical_validity == "verified"
                    and coverage.window is not None
                    and external_sources.intersection(coverage.source_ids)
                ):
                    continue
                query_hashes = {
                    sources_by_id[source_id].query_set_hash for source_id in coverage.source_ids
                }
                if None in query_hashes or len(query_hashes) != 1:
                    raise ValueError("ENHANCED external coverage requires one non-null query-set hash")
                complete_external.update(coverage.source_ids)
            if not external_sources <= complete_external:
                raise ValueError("ENHANCED requires complete as-of external census coverage with a window")
            if self.prediction.model.feature_set == "E2" and not any(
                c.stage == "agent" and c.status == "complete" and c.historical_validity == "verified"
                for c in self.coverage
            ):
                raise ValueError("E2 requires complete as-of agent processing coverage")
        elif self.prediction.model.feature_set != "E0" or self.core_prediction is not None:
            raise ValueError("CORE selects only E0; core_prediction must be null")
        for prediction in [self.prediction, *([self.core_prediction] if self.core_prediction else [])]:
            self.require_sources(prediction.feature_source_ids, as_of=True)
            evidence_by_id = {item.evidence_id: item for item in self.evidence}
            for evidence_id in prediction.feature_evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None or evidence.use != "feature":
                    raise ValueError("model evidence must reference eligible feature evidence")
                if not {c.source_id for c in evidence.citations} <= set(prediction.feature_source_ids):
                    raise ValueError("model evidence citations must be in model source lineage")
                if evidence.origin == "agent" and prediction.model.feature_set != "E2":
                    raise ValueError("only E2 can consume specialist-agent features")
                if evidence.origin == "agent":
                    processed_sources = {
                        source_id for c in self.coverage for source_id in c.source_ids
                        if c.stage == "agent" and c.status == "complete"
                        and c.historical_validity == "verified"
                    }
                    if not {c.source_id for c in evidence.citations} <= processed_sources:
                        raise ValueError("used agent evidence requires complete processing coverage")
                if evidence.origin == "external" and prediction.model.feature_set == "E0":
                    raise ValueError("CORE cannot consume external features")
            if prediction.model.frozen_at > self.generated_at:
                raise ValueError("model artifact cannot be frozen after report generation")
            if self.backtest and prediction.model.frozen_at >= self.backtest.test_window.start_at:
                raise ValueError("prospective backtests require models frozen before the test period")
            allowed_stages = {"core"} if prediction.model.feature_set == "E0" else {
                "core", "external", "agent"
            }
            covered = {
                source_id for item in self.coverage
                if item.stage in allowed_stages
                and item.status in {"complete", "partial"}
                and item.historical_validity == "verified"
                for source_id in item.source_ids
            }
            if not set(prediction.feature_source_ids) <= covered:
                raise ValueError("model inputs require explicit as-of coverage in an allowed stage")
        return self


class UnscoredReport(ReportContext):
    mode_used: None

    @model_validator(mode="after")
    def check_unscored(self):
        if self.enhanced_status not in {"not_requested", "not_run"}:
            raise ValueError("unscored reports must clear model selection and fallback state")
        return self


class AlreadyAbandonedReport(UnscoredReport):
    result_status: Literal["already_abandoned"]
    official_status: OfficialStatus

    @model_validator(mode="after")
    def check_status(self):
        self.check_official_status(self.official_status)
        if not self.official_status.cessation_confirmed or self.official_status.effective_at > self.cutoff_at:
            raise ValueError("already_abandoned requires confirmed cessation effective by cutoff")
        return self


class ScheduledCessationReport(UnscoredReport):
    result_status: Literal["scheduled_cessation"]
    official_status: OfficialStatus
    within_horizon: bool

    @model_validator(mode="after")
    def check_status(self):
        self.check_official_status(self.official_status)
        if self.official_status.cessation_confirmed or self.official_status.effective_at <= self.cutoff_at:
            raise ValueError("scheduled_cessation requires an unfulfilled future official plan")
        if self.within_horizon != (self.official_status.effective_at <= self.horizon_end_at):
            raise ValueError("scheduled cessation horizon flag disagrees with effective date")
        return self


class NoScoreReport(UnscoredReport):
    result_status: NoScoreStatus
    reason_code: NonEmpty
    reason: NonEmpty

    @model_validator(mode="after")
    def check_no_score(self):
        if any(e.use == "official_status" for e in self.evidence):
            raise ValueError("verified official status requires a known-status report")
        if self.result_status == "mapping_unresolved" and self.mapping is not None:
            raise ValueError("unresolved mapping must be null")
        return self


FinalReport = Annotated[
    PredictedReport | AlreadyAbandonedReport | ScheduledCessationReport | NoScoreReport,
    Field(discriminator="result_status"),
]

FINAL_REPORT_ADAPTER = TypeAdapter(FinalReport)


def validate_final_report(data: dict | str | bytes | ReportContext) -> FinalReport:
    """Validate at every consumer boundary, including mutated model instances."""
    if isinstance(data, ReportContext):
        data = data.model_dump(mode="python")
    if isinstance(data, str | bytes):
        return FINAL_REPORT_ADAPTER.validate_json(data)
    return FINAL_REPORT_ADAPTER.validate_python(data)
