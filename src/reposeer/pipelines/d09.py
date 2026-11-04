"""Reviewed inputs for the strict offline D09 path."""

from pydantic import Field, model_validator

from reposeer.contracts.analysis import AnalysisEvidence, PackageMapping, SourceProvenance
from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    ContractModel,
    DataGroup,
    NonEmpty,
    PackageRelease,
    RepositoryActivity,
    SnapshotBuildRequest,
    TimeRange,
    UtcTimestamp,
    unique,
)
from reposeer.features.builder import CORE_DATA_GROUPS
from reposeer.maintenance.labels import HistoricalOfficialStatus


class ReviewedFollowup(ContractModel):
    window: TimeRange
    verified_at: UtcTimestamp
    source_ids: list[NonEmpty] = Field(min_length=1)


class ReviewedD09Target(ContractModel):
    """One package-cutoff and the provenance accepted by an independent reviewer."""

    snapshot: SnapshotBuildRequest
    mapping: PackageMapping
    verifier: NonEmpty
    reviewed_at: UtcTimestamp
    sources: list[SourceProvenance] = Field(min_length=1)
    core_coverage_sources: dict[DataGroup, list[NonEmpty]]
    official_search_sources: list[NonEmpty] = Field(min_length=1)
    official_evidence: list[AnalysisEvidence] = Field(default_factory=list)
    official_events: list[HistoricalOfficialStatus] = Field(default_factory=list)
    followup: ReviewedFollowup | None = None
    horizon_releases: list[PackageRelease] = Field(default_factory=list)
    horizon_activities: list[RepositoryActivity] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_review(self) -> "ReviewedD09Target":
        request = self.snapshot
        cutoff = request.cutoff_at
        horizon = add_calendar_months(cutoff, 12)
        mapping = self.mapping
        if (
            self.reviewed_at < cutoff
            or mapping.repo_id != request.repository.repo_id
            or max(mapping.valid_from, mapping.available_at) > cutoff
            or (mapping.valid_to is not None and mapping.valid_to <= cutoff)
        ):
            raise ValueError("mapping and review must cover the requested package-cutoff")
        sources = {source.source_id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("duplicate review source IDs")
        if any(source.retrieved_at > self.reviewed_at for source in self.sources):
            raise ValueError("review cannot predate evidence retrieval")

        def require_sources(ids: list[str], at: str) -> None:
            unique(ids, "review source IDs")
            if not ids or any(source_id not in sources for source_id in ids):
                raise ValueError("review references missing source revisions")
            for source_id in ids:
                sources[source_id].require_as_of(at)

        require_sources(mapping.source_ids, mapping.available_at)
        require_sources(self.official_search_sources, self.reviewed_at)
        usable = {
            coverage.data_group: coverage
            for coverage in request.coverage
            if coverage.data_group in CORE_DATA_GROUPS
            and coverage.status in ("complete", "partial")
        }
        if set(usable) != set(self.core_coverage_sources):
            raise ValueError("each usable CORE group requires explicit coverage sources")
        for group, coverage in usable.items():
            require_sources(self.core_coverage_sources[group], cutoff)
            if coverage.time_range is None or coverage.time_range.end_at > cutoff:
                raise ValueError("CORE coverage must declare a window ending by cutoff")
            if coverage.status == "complete" and (
                coverage.time_range.end_at != cutoff
                or coverage.time_range.start_at > add_calendar_months(cutoff, -12)
            ):
                raise ValueError("complete CORE coverage must span the 12-month lookback")
            if coverage.status == "partial" and coverage.records_observed == 0:
                raise ValueError("partial coverage cannot certify zero observations")

        evidence = {item.evidence_id: item for item in self.official_evidence}
        if len(evidence) != len(self.official_evidence) or any(
            item.package_id != request.package_id or item.use != "official_status"
            for item in evidence.values()
        ):
            raise ValueError("official evidence must be unique and scoped to the target package")
        unique([event.ledger_revision for event in self.official_events], "ledger revisions")
        for event in self.official_events:
            if max(event.verified_at, event.available_at) > self.reviewed_at or any(
                key not in evidence for key in event.evidence_ids
            ):
                raise ValueError("official event requires verified evidence by the review time")
            require_sources(
                sorted(
                    {
                        citation.source_id
                        for key in event.evidence_ids
                        for citation in evidence[key].citations
                    }
                ),
                event.available_at,
            )

        if self.followup is not None:
            followup = self.followup
            if not (
                followup.window.start_at <= cutoff
                and horizon <= followup.window.end_at <= followup.verified_at <= self.reviewed_at
            ):
                raise ValueError("verified follow-up must cover the full 12-month horizon")
            require_sources(followup.source_ids, followup.verified_at)

        if any(
            release.package_id != request.package_id
            or release.repo_id not in (None, mapping.repo_id)
            or not cutoff < release.uploaded_at <= horizon
            for release in self.horizon_releases
        ) or any(
            mapping.scope_quality != "package_scoped"
            or activity.repo_id != mapping.repo_id
            or not cutoff < activity.event_at <= horizon
            or (mapping.valid_to is not None and activity.event_at >= mapping.valid_to)
            for activity in self.horizon_activities
        ):
            raise ValueError("outcome evidence must be in the target package's horizon and scope")
        return self
