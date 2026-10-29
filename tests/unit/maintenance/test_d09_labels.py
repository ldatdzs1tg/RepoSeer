"""Unit tests for D09 historical label pipeline and deterministic detectors."""
from __future__ import annotations

import polars as pl
import pytest

from reposeer.contracts.analysis import OfficialStatus
from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    PackageRelease,
    Provenance,
    RepositoryActivity,
)
from reposeer.maintenance.detector import MaintenanceDetector
from reposeer.maintenance.labels import (
    HISTORICAL_LABEL_SCHEMA,
    HistoricalLabelRecord,
    labels_to_polars,
    polars_to_labels,
)
from reposeer.maintenance.replacement_detector import ReplacementDetector
from reposeer.maintenance.resumed_detector import ResumedDetector
from reposeer.pipelines.maintenance import MaintenancePipeline
from reposeer.schemas.github.repository import RepositoryRecord

CUTOFF = "2024-03-31T23:59:59.999Z"
HORIZON_END = add_calendar_months(CUTOFF, 12)  # 2025-03-31T23:59:59.999Z
OBSERVATION_END = "2025-04-15T00:00:00.000Z"
PACKAGE_ID = "pypi:demo-pkg"
REPO_ID = "github:repository:900000001"


def make_official_status(
    *,
    event_type: str = "ARCHIVED",
    cessation_confirmed: bool = True,
    announced_at: str = "2024-01-01T00:00:00.000Z",
    effective_at: str = "2024-01-01T00:00:00.000Z",
    verified_at: str = "2024-01-01T00:00:00.000Z",
    revoked_at: str | None = None,
    evidence_ids: list[str] | None = None,
) -> OfficialStatus:
    if cessation_confirmed and verified_at < effective_at:
        verified_at = effective_at
    if verified_at < announced_at:
        verified_at = announced_at
    return OfficialStatus(
        event_type=event_type,
        cessation_confirmed=cessation_confirmed,
        announced_at=announced_at,
        effective_at=effective_at,
        verified_at=verified_at,
        revoked_at=revoked_at,
        ledger_revision="rev-001",
        verifier="analyst:test",
        verification_policy_version="0.1.0",
        evidence_ids=evidence_ids or ["src:test-evidence-1"],
    )


def make_provenance(
    *,
    retrieved_at: str = "2025-05-01T00:00:00.000Z",
    valid_at: str = "2024-01-01T00:00:00.000Z",
    available_at: str = "2024-01-01T00:00:00.000Z",
    raw_id: str = "raw:00000000-0000-4000-8000-000000000001",
) -> Provenance:
    return Provenance(
        source="pypi_json",
        source_url="https://pypi.org/pypi/demo-pkg/json",
        retrieved_at=retrieved_at,
        valid_at=valid_at,
        available_at=available_at,
        temporal_basis="historical_reconstruction",
        raw_record_ids=[raw_id],
        missing_fields=[],
    )


def make_release(
    version: str,
    uploaded_at: str,
    available_at: str | None = None,
) -> PackageRelease:
    av = available_at or uploaded_at
    prov = make_provenance(
        retrieved_at=max("2025-05-01T00:00:00.000Z", uploaded_at, av),
        valid_at=uploaded_at,
        available_at=av,
    )
    return PackageRelease(
        schema_version="0.1.0",
        record_type="package_release",
        package_id=PACKAGE_ID,
        repo_id=REPO_ID,
        version=version,
        uploaded_at=uploaded_at,
        requires_python=">=3.10",
        dependencies=None,
        is_yanked=None,
        yanked_reason=None,
        yanked_status_at=None,
        provenance=prov,
    )


def make_activity(
    activity_id: str,
    kind: str,
    event_at: str,
    actor_id: str | None = "1001",
    actor_is_bot: bool | None = False,
    available_at: str | None = None,
) -> RepositoryActivity:
    av = available_at or event_at
    prov = make_provenance(
        retrieved_at=max("2025-05-01T00:00:00.000Z", event_at, av),
        valid_at=event_at,
        available_at=av,
    )
    return RepositoryActivity(
        schema_version="0.1.0",
        record_type="repository_activity",
        activity_id=f"github:activity:900000001:{kind}:{activity_id}",
        repo_id=REPO_ID,
        activity_kind=kind,
        source_object_id=activity_id,
        event_at=event_at,
        actor_id=actor_id,
        actor_is_bot=actor_is_bot,
        provenance=prov,
    )


class TestD09AsOfStatus:
    """Test 1-3: As-of status evaluation at cutoff."""

    def test_already_abandoned_at_cutoff(self) -> None:
        pipeline = MaintenancePipeline()
        event = make_official_status(
            event_type="ARCHIVED",
            cessation_confirmed=True,
            announced_at="2024-01-01T00:00:00.000Z",
            effective_at="2024-01-15T00:00:00.000Z",
            verified_at="2024-01-20T00:00:00.000Z",
        )
        as_of_status, off_event, within_h = pipeline.evaluate_as_of_status(
            cutoff_at=CUTOFF, official_events=[event]
        )
        assert as_of_status == "already_abandoned"
        assert off_event == event
        assert within_h is None

        # Full horizon evaluation excludes from risk set
        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            official_events=[event],
        )
        assert record.as_of_status == "already_abandoned"
        assert record.in_risk_set is False
        assert record.label is None
        assert record.label_status == "excluded_known_status"
        assert record.censor_reason == "already_abandoned_at_cutoff"

    def test_scheduled_cessation_at_cutoff(self) -> None:
        pipeline = MaintenancePipeline()
        event = make_official_status(
            event_type="MAINTENANCE_CEASED",
            cessation_confirmed=False,
            announced_at="2024-02-01T00:00:00.000Z",
            effective_at="2024-08-01T00:00:00.000Z",
            verified_at="2024-02-05T00:00:00.000Z",
        )
        as_of_status, off_event, within_h = pipeline.evaluate_as_of_status(
            cutoff_at=CUTOFF, official_events=[event]
        )
        assert as_of_status == "scheduled_cessation"
        assert off_event == event
        assert within_h is True

        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            official_events=[event],
        )
        assert record.as_of_status == "scheduled_cessation"
        assert record.within_12m_horizon is True
        assert record.in_risk_set is False
        assert record.label is None
        assert record.label_status == "excluded_known_status"
        assert record.censor_reason == "scheduled_cessation_at_cutoff"

    def test_revoked_plan_reverts_to_unverified(self) -> None:
        pipeline = MaintenancePipeline()
        event = make_official_status(
            event_type="MAINTENANCE_CEASED",
            cessation_confirmed=False,
            announced_at="2024-01-01T00:00:00.000Z",
            effective_at="2024-08-01T00:00:00.000Z",
            verified_at="2024-01-05T00:00:00.000Z",
            revoked_at="2024-02-01T00:00:00.000Z",
        )
        as_of_status, off_event, within_h = pipeline.evaluate_as_of_status(
            cutoff_at=CUTOFF, official_events=[event]
        )
        assert as_of_status == "status_unverified"
        assert off_event is None
        assert within_h is None

        # Remains eligible for risk set
        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            official_events=[event],
            horizon_activities=[
                make_activity("c1", "commit", "2024-06-01T00:00:00.000Z"),
            ],
        )
        assert record.as_of_status == "status_unverified"
        assert record.in_risk_set is True
        assert record.label == 0
        assert record.label_status == "negative"


class TestD09HorizonLabels:
    """Test 4-6: Horizon label assignment."""

    def test_positive_label_confirmed_cessation_in_horizon(self) -> None:
        pipeline = MaintenancePipeline()
        event = make_official_status(
            event_type="ARCHIVED",
            cessation_confirmed=True,
            announced_at="2024-05-01T00:00:00.000Z",
            effective_at="2024-06-01T00:00:00.000Z",
            verified_at="2024-06-15T00:00:00.000Z",
        )
        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            official_events=[event],
        )
        assert record.in_risk_set is True
        assert record.label == 1
        assert record.label_status == "positive"
        assert record.event_effective_at == "2024-06-01T00:00:00.000Z"
        assert record.label_available_at == "2024-06-15T00:00:00.000Z"
        assert record.label_available_at >= record.event_effective_at

    def test_negative_label_human_activity_in_horizon(self) -> None:
        pipeline = MaintenancePipeline()
        releases = [make_release("1.1.0", "2024-05-01T00:00:00.000Z")]
        activities = [make_activity("c1", "commit", "2024-07-01T00:00:00.000Z")]

        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            horizon_releases=releases,
            horizon_activities=activities,
            horizon_coverage_complete=True,
        )
        assert record.in_risk_set is True
        assert record.label == 0
        assert record.label_status == "negative"
        assert record.label_available_at == HORIZON_END
        assert record.censor_reason is None

    def test_silent_unverified_zero_activity(self) -> None:
        pipeline = MaintenancePipeline()
        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            horizon_releases=[],
            horizon_activities=[],
            horizon_coverage_complete=True,
        )
        assert record.in_risk_set is True
        assert record.label is None
        assert record.label_status == "unknown"
        assert record.censor_reason == "silent_unverified"

    def test_bot_activity_does_not_confer_negative_label(self) -> None:
        pipeline = MaintenancePipeline()
        bot_act = make_activity(
            "b1", "commit", "2024-06-01T00:00:00.000Z", actor_is_bot=True
        )
        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            horizon_activities=[bot_act],
            horizon_coverage_complete=True,
        )
        assert record.label is None
        assert record.label_status == "unknown"
        assert record.censor_reason == "silent_unverified"

    def test_insufficient_followup_censoring(self) -> None:
        pipeline = MaintenancePipeline()
        short_obs = "2024-10-01T00:00:00.000Z"  # Before HORIZON_END 2025-03-31
        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=short_obs,
            horizon_activities=[make_activity("c1", "commit", "2024-05-01T00:00:00.000Z")],
        )
        assert record.in_risk_set is True
        assert record.label is None
        assert record.label_status == "unknown"
        assert record.censor_reason == "insufficient_followup"

    def test_incomplete_coverage_censoring(self) -> None:
        pipeline = MaintenancePipeline()
        record = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            horizon_coverage_complete=False,
        )
        assert record.in_risk_set is True
        assert record.label is None
        assert record.label_status == "unknown"
        assert record.censor_reason == "incomplete_coverage"


class TestD09PolarsConversion:
    """Test 7: Polars DataFrame conversion and lossless round-trip."""

    def test_polars_round_trip(self) -> None:
        pipeline = MaintenancePipeline()
        event = make_official_status(
            event_type="ARCHIVED",
            cessation_confirmed=True,
            announced_at="2024-05-01T00:00:00.000Z",
            effective_at="2024-06-01T00:00:00.000Z",
            verified_at="2024-06-15T00:00:00.000Z",
        )
        rec1 = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            official_events=[event],
        )
        rec2 = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
            horizon_activities=[make_activity("c1", "commit", "2024-05-01T00:00:00.000Z")],
        )
        rec3 = pipeline.evaluate_horizon_label(
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            cutoff_at=CUTOFF,
            observation_end_at=OBSERVATION_END,
        )

        records = [rec1, rec2, rec3]
        df = pipeline.labels_to_polars(records)
        assert isinstance(df, pl.DataFrame)
        assert len(df) == 3

        for col, dtype in HISTORICAL_LABEL_SCHEMA.items():
            assert col in df.columns
            assert df.schema[col] == dtype

        reconstructed = pipeline.polars_to_labels(df)
        assert len(reconstructed) == 3
        assert reconstructed[0] == rec1
        assert reconstructed[1] == rec2
        assert reconstructed[2] == rec3

    def test_empty_polars(self) -> None:
        pipeline = MaintenancePipeline()
        empty_df = pipeline.labels_to_polars([])
        assert isinstance(empty_df, pl.DataFrame)
        assert empty_df.is_empty()
        assert pipeline.polars_to_labels(empty_df) == []


class TestDeterministicDetectors:
    """Test 8: Upgraded deterministic detectors."""

    def test_replacement_detector(self) -> None:
        det = ReplacementDetector()
        text1 = "This project is deprecated and replaced by new-cool-pkg."
        ev1 = det.detect("org/old-repo", text1)
        assert ev1 is not None
        assert ev1.event_type == "replacement"
        assert "replaced by" in ev1.details.lower()

        text2 = "Superseded by other/better-repo for Python 3.12+"
        ev2 = det.detect("org/old-repo", text2)
        assert ev2 is not None
        assert ev2.event_type == "replacement"

        text3 = "Moved to new-org/new-repo"
        ev3 = det.detect("org/old-repo", text3)
        assert ev3 is not None
        assert ev3.event_type == "replacement"

        assert det.detect("org/repo", "Just a regular README description") is None

    def test_resumed_detector(self) -> None:
        det = ResumedDetector()
        text1 = "Good news: this project is no longer deprecated and active development continues."
        ev1 = det.detect("org/repo", text1)
        assert ev1 is not None
        assert ev1.event_type == "resumed"

        text2 = "Maintenance resumed as of v2.0."
        ev2 = det.detect("org/repo", text2)
        assert ev2 is not None
        assert ev2.event_type == "resumed"

        assert det.detect("org/repo", "Normal library documentation") is None

        # evaluate repository object unarchived
        class MockRepo:
            full_name = "org/repo"
            archived = False
            was_archived = True

        events = det.evaluate(MockRepo())
        assert len(events) == 1
        assert events[0].event_type == "resumed"

    def test_maintenance_detector_backward_compatible(self) -> None:
        det = MaintenanceDetector()
        repo = RepositoryRecord(
            owner="org",
            name="archived-repo",
            full_name="org/archived-repo",
            archived=True,
        )
        events = det.evaluate(repo)
        assert len(events) == 1
        assert events[0].event_type == "archived"
