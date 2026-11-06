"""Analyze one verified cutoff feature package with a frozen CORE Model A."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from reposeer.contracts.analysis import (
    ANALYSIS_SCHEMA_VERSION,
    AnalysisCoverage,
    AnalysisEvidence,
    AnalysisMode,
    Applicability,
    FinalReport,
    ModelManifest,
    ModelResult,
    NoScoreStatus,
    PackageMapping,
    SourceIds,
    SourceProvenance,
    validate_final_report,
)
from reposeer.contracts.identity import add_calendar_months, format_utc_timestamp
from reposeer.contracts.models import (
    Confidence,
    ContractModel,
    Count,
    DataGroup,
    NonEmpty,
    PackageSnapshot,
    UtcTimestamp,
    unique,
)
from reposeer.contracts.validation import validate_snapshot_coverage
from reposeer.features.builder import CORE_DATA_GROUPS, FEATURE_COLUMNS, FeatureBuilder
from reposeer.maintenance.labels import HistoricalOfficialStatus
from reposeer.modeling.model_a import ModelA
from reposeer.pipelines.d09 import ReviewedD09Target
from reposeer.pipelines.maintenance import MaintenancePipeline

PREFLIGHT_POLICY_VERSION = "core-inference-0.1.0"
RELEASE_GATES = (
    "sample_and_group_checks",
    "prospective_evaluation_completed",
    "population_calibration_established",
)


class CoreFeaturePackage(ContractModel):
    """Cutoff snapshot and its provenance from the reviewed feature producer."""

    snapshot: PackageSnapshot
    mapping: PackageMapping | None
    sources: list[SourceProvenance]
    core_coverage_sources: dict[DataGroup, SourceIds]
    official_status: HistoricalOfficialStatus | None = None
    official_evidence: list[AnalysisEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_provenance(self):
        validate_snapshot_coverage(self.snapshot)
        unique([source.source_id for source in self.sources], "sources.source_id")
        sources = {row.source_id: row for row in self.sources}
        if self.mapping is not None and self.mapping.repo_id != self.snapshot.repo_id:
            raise ValueError("mapping repository differs from the cutoff snapshot")
        groups = {
            row.data_group
            for row in self.snapshot.coverage
            if row.data_group in CORE_DATA_GROUPS and row.status in {"complete", "partial"}
        }
        if not self.core_coverage_sources.keys() <= groups:
            raise ValueError("CORE source references require a collected CORE group")
        refs = set(self.mapping.source_ids if self.mapping else ())
        for ids in self.core_coverage_sources.values():
            unique(ids, "core_coverage_sources")
            refs.update(ids)
        evidence_ids = [row.evidence_id for row in self.official_evidence]
        unique(evidence_ids, "official_evidence.evidence_id")
        if set(evidence_ids) != set(
            self.official_status.evidence_ids if self.official_status else ()
        ):
            raise ValueError("official status must reference the supplied package evidence")
        official_refs = set()
        for row in self.official_evidence:
            if row.package_id != self.snapshot.package_id or row.use != "official_status":
                raise ValueError("official evidence must refer directly to the analyzed package")
            official_refs.update(citation.source_id for citation in row.citations)
        if (refs | official_refs) - sources.keys():
            raise ValueError("feature package has source references without provenance")
        if self.official_status is not None:
            for source_id in official_refs:
                sources[source_id].require_as_of(self.official_status.available_at)
        return self

    @classmethod
    def from_review(cls, review: ReviewedD09Target) -> "CoreFeaturePackage":
        """Export features and evidence available at the cutoff from a D09 review."""
        review = ReviewedD09Target.model_validate(review.model_dump())
        built = FeatureBuilder().build_snapshot(review.snapshot)
        if built.snapshot is None:
            raise ValueError("the reviewed D09 cutoff could not produce a feature snapshot")
        _, status, _ = MaintenancePipeline().evaluate_as_of_status(
            cutoff_at=built.snapshot.cutoff_at,
            official_events=review.official_events,
        )
        evidence = [
            row
            for row in review.official_evidence
            if status and row.evidence_id in status.evidence_ids
        ]
        ids = set(review.mapping.source_ids)
        ids.update(
            source_id for group in review.core_coverage_sources.values() for source_id in group
        )
        ids.update(citation.source_id for row in evidence for citation in row.citations)
        return cls(
            snapshot=built.snapshot,
            mapping=review.mapping,
            sources=[row for row in review.sources if row.source_id in ids],
            core_coverage_sources=review.core_coverage_sources,
            official_status=status,
            official_evidence=evidence,
        )


class FeatureRange(ContractModel):
    minimum: Count
    maximum: Count

    @model_validator(mode="after")
    def check_order(self):
        if self.minimum > self.maximum:
            raise ValueError("feature range must be ordered")
        return self


class FrozenCorePolicy(ContractModel):
    """Release metadata and the calibrated feature profile frozen with an artifact."""

    preprocessing_version: NonEmpty
    calibration_version: NonEmpty
    calibration_population: NonEmpty
    trained_through_at: UtcTimestamp
    risk_threshold_version: NonEmpty
    moderate_at: Confidence
    high_at: Confidence
    applicability_version: NonEmpty
    feature_ranges: dict[NonEmpty, FeatureRange] = Field(min_length=1)

    @model_validator(mode="after")
    def check_policy(self):
        if not 0 < self.moderate_at < self.high_at < 1:
            raise ValueError("risk thresholds must be strictly ordered inside (0, 1)")
        if self.feature_ranges.keys() - set(FEATURE_COLUMNS):
            raise ValueError("applicability policy must describe CORE features")
        return self


def _available_at(ids: list[str], sources: dict[str, SourceProvenance], cutoff: str) -> bool:
    try:
        for source_id in ids:
            sources[source_id].require_as_of(cutoff)
    except ValueError:
        return False
    return bool(ids)


def _core_coverage(
    package: CoreFeaturePackage, sources: dict[str, SourceProvenance]
) -> list[AnalysisCoverage]:
    cutoff = package.snapshot.cutoff_at
    by_group = {row.data_group: row for row in package.snapshot.coverage}
    results = []
    for group in CORE_DATA_GROUPS:
        row = by_group.get(group)
        status = row.status if row else "unavailable"
        if status == "not_applicable":
            status = "not_requested"
        observed = status in {"complete", "partial"}
        window = row.time_range if row else None
        ids = package.core_coverage_sources.get(group, [])
        verified = (
            observed
            and window is not None
            and _available_at(ids, sources, cutoff)
            and (
                status == "partial"
                or (window.end_at == cutoff and window.start_at <= add_calendar_months(cutoff, -12))
            )
        )
        results.append(
            AnalysisCoverage(
                coverage_id=f"core-{group}",
                stage="core",
                source_ids=ids,
                status=status,
                source_coverage=status if observed else "unknown",
                historical_validity="verified" if verified else "unverified",
                window=window,
                records_observed=(
                    row.records_observed
                    if observed and (status == "complete" or row.records_observed)
                    else None
                ),
                eligible_documents=None,
                processed_documents=None,
                missing_reason=row.missing_reason if row else "not_collected",
            )
        )
    return results


def analyze_core(
    package: CoreFeaturePackage,
    model_path: str | Path,
    *,
    mode_requested: AnalysisMode = "CORE",
    purpose: Literal["current_inference", "retrospective_analysis"] = "retrospective_analysis",
) -> FinalReport:
    """Apply release and as-of gates and return a validated analysis result."""
    if purpose not in {"current_inference", "retrospective_analysis"}:
        raise ValueError("Analyze requires current_inference or retrospective_analysis")
    package = CoreFeaturePackage.model_validate(package.model_dump())
    snapshot, mapping = package.snapshot, package.mapping
    cutoff = snapshot.cutoff_at
    generated_at = format_utc_timestamp(datetime.now(UTC))
    sources = {row.source_id: row for row in package.sources}
    coverage = _core_coverage(package, sources)
    observed = {
        name: value for name, value in snapshot.features.model_dump().items() if value is not None
    }
    context = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "record_type": "final_report",
        "analysis_run_id": str(uuid4()),
        "package_id": snapshot.package_id,
        "mapping": mapping,
        "cutoff_at": cutoff,
        "generated_at": generated_at,
        "purpose": purpose,
        "horizon_months": snapshot.horizon_months,
        "horizon_end_at": snapshot.horizon_end_at,
        "mode_requested": mode_requested,
        "mode_used": None,
        "enhanced_status": "not_requested" if mode_requested == "CORE" else "not_run",
        "fallback_reason": None,
        "external_insights_status": "not_requested",
        "external_insights_reason": None,
        "coverage": coverage,
        "sources": package.sources,
        "evidence": [],
        "dependency_impact": None,
        "backtest": None,
        "limitations": [
            f"The cutoff snapshot contains {len(observed)} of {len(FEATURE_COLUMNS)} CORE feature values."
        ],
    }

    def no_score(
        code: str,
        reason: str,
        *,
        status: NoScoreStatus = "insufficient_evidence",
        applicability: Applicability | None = None,
    ) -> FinalReport:
        return validate_final_report(
            {
                **context,
                "result_status": status,
                "reason_code": code,
                "reason": reason,
                "applicability": applicability
                or Applicability(
                    status="unknown",
                    policy_version=PREFLIGHT_POLICY_VERSION,
                    reasons=[code],
                    limitations=[],
                ),
            }
        )

    as_of_status, official, within_horizon = MaintenancePipeline().evaluate_as_of_status(
        cutoff_at=cutoff,
        official_events=[package.official_status] if package.official_status else [],
    )
    if official is not None:
        return validate_final_report(
            {
                **context,
                "result_status": as_of_status,
                "evidence": package.official_evidence,
                "official_status": official.model_dump(exclude={"available_at"}),
                "applicability": Applicability(
                    status="unknown",
                    policy_version=PREFLIGHT_POLICY_VERSION,
                    reasons=[as_of_status],
                    limitations=[],
                ),
                **(
                    {"within_horizon": within_horizon}
                    if as_of_status == "scheduled_cessation"
                    else {}
                ),
            }
        )

    if mapping is None:
        return no_score(
            "no_verified_package_repository_mapping",
            "A verified repository mapping is required.",
            status="mapping_unresolved",
        )
    if (
        max(mapping.valid_from, mapping.available_at) > cutoff
        or (mapping.valid_to is not None and mapping.valid_to <= cutoff)
        or not _available_at(mapping.source_ids, sources, mapping.available_at)
    ):
        return no_score(
            "mapping_unavailable_at_cutoff",
            "The repository mapping requires verification at the cutoff.",
            status="mapping_history_unverified",
        )
    if not observed:
        return no_score(
            "no_usable_core_features", "Model A requires usable CORE features at the cutoff."
        )
    complete = [row for row in coverage if row.status == "complete"]
    if any(row.historical_validity != "verified" for row in complete):
        return no_score(
            "core_history_unverified",
            "CORE feature coverage requires source revisions available at the cutoff.",
        )
    if mapping.scope_quality != "package_scoped" and any(
        row.coverage_id != "core-releases" for row in complete
    ):
        return no_score(
            "package_scope_unverified", "Repository activity requires verified package scope."
        )

    try:
        body = Path(model_path).read_bytes()
        model = ModelA.from_bytes(body)
        artifact = model.artifact
        if snapshot.schema_version != artifact.get("feature_schema_version", "0.1.0"):
            return no_score(
                "feature_schema_mismatch",
                "The cutoff snapshot and frozen model require matching feature schemas.",
            )
        gates = artifact.get("release_gates", {})
        if not (
            artifact["release_eligible"]
            and isinstance(gates, dict)
            and all(gates.get(key) is True for key in RELEASE_GATES)
        ):
            return no_score(
                "model_release_gates_not_met",
                f"Frozen Model A {artifact['model_version']} requires release approval before scoring.",
            )
        if artifact.get("analysis_policy") is None:
            return no_score(
                "model_policy_unavailable",
                "The frozen model requires a calibrated analysis policy.",
            )
        policy = FrozenCorePolicy.model_validate(artifact["analysis_policy"])
        manifest = ModelManifest(
            model_id="model-a",
            model_version=artifact["model_version"],
            artifact_sha256=sha256(body).hexdigest(),
            feature_set=artifact["feature_set"],
            feature_schema_version=artifact["feature_schema_version"],
            preprocessing_version=policy.preprocessing_version,
            calibration_version=policy.calibration_version,
            risk_threshold_version=policy.risk_threshold_version,
            training_population=artifact["training_population"],
            calibration_population=policy.calibration_population,
            trained_through_at=policy.trained_through_at,
            frozen_at=artifact["frozen_at"],
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return no_score(
            "model_load_failed",
            "The selected Model A artifact could not be loaded or validated.",
            status="failed",
        )

    if manifest.feature_set != "E0":
        return no_score(
            "unsupported_feature_set",
            "Analyze requires the frozen CORE E0 model.",
            status="unsupported",
        )
    if manifest.frozen_at > generated_at or (
        purpose == "current_inference" and manifest.frozen_at > cutoff
    ):
        return no_score(
            "model_unavailable_at_decision",
            "The selected model requires a freeze by the analysis decision time.",
        )
    ranges = policy.feature_ranges
    if observed.keys() != ranges.keys() or any(
        not ranges[name].minimum <= value <= ranges[name].maximum
        for name, value in observed.items()
    ):
        return no_score(
            "outside_calibrated_feature_profile",
            "The cutoff features fall outside the calibrated Model A profile.",
            applicability=Applicability(
                status="out_of_distribution",
                policy_version=policy.applicability_version,
                reasons=["outside_calibrated_feature_profile"],
                limitations=[],
            ),
        )

    try:
        probability = model.predict_snapshot(snapshot)["probability"]
        prediction = ModelResult(
            result_status="predicted",
            probability=probability,
            risk_level="high"
            if probability >= policy.high_at
            else "moderate"
            if probability >= policy.moderate_at
            else "low",
            model=manifest,
            feature_source_ids=sorted(
                {source_id for row in complete for source_id in row.source_ids}
            ),
            feature_evidence_ids=[],
            explanation=None,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return no_score(
            "model_inference_failed",
            "Model A could not score the cutoff snapshot.",
            status="failed",
        )
    enhanced = mode_requested == "ENHANCED"
    return validate_final_report(
        {
            **context,
            "result_status": "predicted",
            "mode_used": "CORE",
            "enhanced_status": "fallback" if enhanced else "not_requested",
            "fallback_reason": "model_b_unavailable" if enhanced else None,
            "applicability": Applicability(
                status="in_domain",
                policy_version=policy.applicability_version,
                reasons=[],
                limitations=[],
            ),
            "prediction": prediction,
            "core_prediction": None,
        }
    )
