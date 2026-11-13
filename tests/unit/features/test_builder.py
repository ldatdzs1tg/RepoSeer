"""Unit tests for canonical FeatureBuilder and contract pair validation."""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from reposeer.contracts.models import (
    Coverage,
    DataError,
    PackageRelease,
    PackageSnapshot,
    Provenance,
    RepoIdentity,
    RepositoryActivity,
    SnapshotBuildRequest,
    SnapshotBuildResult,
    TimeRange,
)
from reposeer.contracts.validation import validate_snapshot_pair
from reposeer.features.builder import FEATURE_COLUMNS, FeatureBuilder

ROOT = Path(__file__).resolve().parents[3]
CUTOFF = "2024-03-31T23:59:59.999Z"
PACKAGE_ID = "pypi:demo-pkg"
REPO_ID = "github:repository:900000001"

REPO_IDENTITY = RepoIdentity(
    repo_id=REPO_ID,
    github_repo_id="900000001",
    github_node_id="R_1",
    repo_full_name="synthetic/demo",
    repo_url="https://github.com/synthetic/demo",
    known_urls=[],
)


def make_provenance(
    retrieved_at: str | None = None,
    valid_at: str = "2024-01-01T00:00:00.000Z",
    available_at: str | None = "2024-01-01T00:00:00.000Z",
    raw_id: str = "raw:00000000-0000-4000-8000-000000000001",
) -> Provenance:
    candidates = [CUTOFF, valid_at]
    if available_at is not None:
        candidates.append(available_at)
    ret_at = retrieved_at or max(candidates)
    return Provenance(
        source="pypi_json",
        source_url="https://pypi.org/pypi/demo-pkg/json",
        retrieved_at=ret_at,
        valid_at=valid_at,
        available_at=available_at,
        temporal_basis="historical_reconstruction",
        raw_record_ids=[raw_id],
        missing_fields=[],
    )


def make_release(
    version: str,
    uploaded_at: str,
    package_id: str = PACKAGE_ID,
    available_at: str | None = "DEFAULT",
    valid_at: str | None = None,
    raw_id: str = "raw:00000000-0000-4000-8000-000000000001",
) -> PackageRelease:
    av = uploaded_at if available_at == "DEFAULT" else available_at
    va = valid_at if valid_at is not None else uploaded_at
    candidates = [uploaded_at, va]
    if av is not None:
        candidates.append(av)
    ret_at = max(CUTOFF, *candidates)
    prov = make_provenance(retrieved_at=ret_at, valid_at=va, available_at=av, raw_id=raw_id)
    return PackageRelease(
        schema_version="0.1.0",
        record_type="package_release",
        package_id=package_id,
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
    repo_id: str = REPO_ID,
    actor_id: str | None = "1001",
    actor_is_bot: bool | None = False,
    available_at: str | None = "DEFAULT",
    valid_at: str | None = None,
    raw_id: str = "raw:00000000-0000-4000-8000-000000000002",
) -> RepositoryActivity:
    av = event_at if available_at == "DEFAULT" else available_at
    va = valid_at if valid_at is not None else event_at
    candidates = [event_at, va]
    if av is not None:
        candidates.append(av)
    ret_at = max(CUTOFF, *candidates)
    prov = make_provenance(retrieved_at=ret_at, valid_at=va, available_at=av, raw_id=raw_id)
    raw_num = repo_id.split(":")[-1]
    return RepositoryActivity(
        schema_version="0.1.0",
        record_type="repository_activity",
        activity_id=f"github:activity:{raw_num}:{kind}:{activity_id}",
        repo_id=repo_id,
        activity_kind=kind,
        source_object_id=activity_id,
        event_at=event_at,
        actor_id=actor_id,
        actor_is_bot=actor_is_bot,
        provenance=prov,
    )


def make_coverage(
    data_group: str,
    status: str = "complete",
    missing_reason: str | None = None,
    records_observed: int = 1,
) -> Coverage:
    tr = (
        TimeRange(start_at="2023-01-01T00:00:00.000Z", end_at=CUTOFF)
        if status in ("complete", "partial")
        else None
    )
    return Coverage(
        data_group=data_group,
        status=status,
        time_range=tr,
        records_observed=records_observed,
        missing_reason=missing_reason if status != "complete" else None,
    )


class TestFeatureBuilderGoldenFixture:
    """Test 1: Golden Contract Fixture Match."""

    def test_golden_fixture_build_snapshot(self) -> None:
        req_path = ROOT / "examples/contracts/snapshot_build_request.json"
        res_path = ROOT / "examples/contracts/snapshot_build_result.json"
        snap_path = ROOT / "examples/contracts/package_snapshot.json"

        request = SnapshotBuildRequest.model_validate_json(req_path.read_text(encoding="utf-8"))
        expected_result = json.loads(res_path.read_text(encoding="utf-8"))
        expected_snapshot = json.loads(snap_path.read_text(encoding="utf-8"))

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)

        # Validate pair contract
        validate_snapshot_pair(request, result)

        assert result.status == "partial"
        assert result.snapshot is not None
        assert result.errors == []

        # Feature values match (all 14 None)
        computed_features = result.snapshot.features.model_dump()
        assert computed_features == expected_snapshot["features"]
        assert all(v is None for v in computed_features.values())

        # Missing field paths match
        computed_missing_paths = {m.field_path for m in result.snapshot.missing_fields}
        expected_missing_paths = {m["field_path"] for m in expected_snapshot["missing_fields"]}
        assert computed_missing_paths == expected_missing_paths

        # Raw record IDs match
        assert result.snapshot.raw_record_ids == expected_snapshot["raw_record_ids"]


class TestFeatureBuilderAllComplete:
    """Test 2: All-Complete Snapshot."""

    def test_all_complete_snapshot(self) -> None:
        coverage = [
            make_coverage("releases", status="complete", records_observed=2),
            make_coverage("commits", status="complete", records_observed=1),
            make_coverage("contributors", status="complete", records_observed=1),
            make_coverage("issues", status="complete", records_observed=2),
            make_coverage("pull_requests", status="complete", records_observed=2),
        ]
        releases = [
            make_release("1.0.0", "2023-10-01T00:00:00.000Z", raw_id="raw:00000000-0000-4000-8000-000000000010"),
            make_release("1.1.0", "2024-02-01T00:00:00.000Z", raw_id="raw:00000000-0000-4000-8000-000000000011"),
        ]
        activities = [
            make_activity("c1", "commit", "2024-01-01T00:00:00.000Z", actor_id="101", actor_is_bot=False, raw_id="raw:00000000-0000-4000-8000-000000000020"),
            make_activity("i1", "issue_opened", "2024-01-10T00:00:00.000Z", raw_id="raw:00000000-0000-4000-8000-000000000021"),
            make_activity("i2", "issue_closed", "2024-01-15T00:00:00.000Z", raw_id="raw:00000000-0000-4000-8000-000000000022"),
            make_activity("p1", "pr_opened", "2024-01-20T00:00:00.000Z", raw_id="raw:00000000-0000-4000-8000-000000000023"),
            make_activity("p2", "pr_merged", "2024-01-25T00:00:00.000Z", raw_id="raw:00000000-0000-4000-8000-000000000024"),
        ]
        request = SnapshotBuildRequest(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000100",
            run_id="00000000-0000-4000-8000-000000000200",
            package_id=PACKAGE_ID,
            repository=REPO_IDENTITY,
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=releases,
            activities=activities,
            coverage=coverage,
        )

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)

        validate_snapshot_pair(request, result)
        assert result.status == "complete"
        assert result.snapshot is not None
        assert result.snapshot.missing_fields == []
        assert result.errors == []

        # All 14 features are integers
        features = result.snapshot.features.model_dump()
        assert len(features) == 14
        for name, val in features.items():
            assert isinstance(val, int), f"feature {name} is not int: {val}"

        assert features["releases_total"] == 2
        assert features["commit_count_12m"] == 1
        assert features["contributor_count_12m"] == 1
        assert features["issue_opened_count_12m"] == 1
        assert features["issue_closed_count_12m"] == 1
        assert features["pr_opened_count_12m"] == 1
        assert features["pr_merged_count_12m"] == 1


class TestFeatureBuilderPartialCoverage:
    """Test 3: Partial Coverage Snapshot."""

    def test_partial_coverage_snapshot(self) -> None:
        coverage = [
            make_coverage("releases", status="complete", records_observed=1),
            make_coverage("commits", status="complete", records_observed=1),
            make_coverage("contributors", status="complete", records_observed=1),
            make_coverage("issues", status="unavailable", missing_reason="unavailable", records_observed=0),
            make_coverage("pull_requests", status="partial", missing_reason="not_collected", records_observed=1),
        ]
        releases = [
            make_release("1.0.0", "2024-01-01T00:00:00.000Z", raw_id="raw:00000000-0000-4000-8000-000000000030"),
        ]
        activities = [
            make_activity("c1", "commit", "2024-01-01T00:00:00.000Z", actor_id="101", actor_is_bot=False, raw_id="raw:00000000-0000-4000-8000-000000000031"),
        ]
        request = SnapshotBuildRequest(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000101",
            run_id="00000000-0000-4000-8000-000000000201",
            package_id=PACKAGE_ID,
            repository=REPO_IDENTITY,
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=releases,
            activities=activities,
            coverage=coverage,
        )

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)

        validate_snapshot_pair(request, result)
        assert result.status == "partial"
        assert result.snapshot is not None

        features = result.snapshot.features
        # 4 issue and PR features are None
        assert features.issue_opened_count_12m is None
        assert features.issue_closed_count_12m is None
        assert features.pr_opened_count_12m is None
        assert features.pr_merged_count_12m is None

        # Other 10 features are integers
        assert isinstance(features.package_age_days, int)
        assert isinstance(features.releases_total, int)
        assert isinstance(features.days_since_latest_release, int)
        assert isinstance(features.release_count_3m, int)
        assert isinstance(features.release_count_6m, int)
        assert isinstance(features.release_count_12m, int)
        assert isinstance(features.commit_count_3m, int)
        assert isinstance(features.commit_count_6m, int)
        assert isinstance(features.commit_count_12m, int)
        assert isinstance(features.contributor_count_12m, int)

        assert len(result.snapshot.missing_fields) == 4
        missing_paths = {m.field_path for m in result.snapshot.missing_fields}
        assert missing_paths == {
            "features.issue_opened_count_12m",
            "features.issue_closed_count_12m",
            "features.pr_opened_count_12m",
            "features.pr_merged_count_12m",
        }


class TestFeatureBuilderLeakageRejection:
    """Test 4: Leakage & Invalid Input Rejection."""

    def test_postcutoff_available_at_rejection(self) -> None:
        leak_activity = make_activity(
            "c1",
            "commit",
            "2024-01-01T00:00:00.000Z",
            available_at="2024-04-01T00:00:00.000Z",  # after CUTOFF 2024-03-31
        )
        # Construct request bypassing Pydantic validation via model_construct
        request = SnapshotBuildRequest.model_construct(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000102",
            run_id="00000000-0000-4000-8000-000000000202",
            package_id=PACKAGE_ID,
            repository=REPO_IDENTITY,
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=[],
            activities=[leak_activity],
            coverage=[make_coverage("commits", status="complete")],
        )

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)

        assert result.status == "failed"
        assert result.snapshot is None
        assert len(result.errors) > 0
        assert result.errors[0].code == "validation_error"
        assert "available_at" in result.errors[0].message
        validate_snapshot_pair(request, result)

    def test_none_available_at_rejection(self) -> None:
        unverified_activity = make_activity(
            "c1",
            "commit",
            "2024-01-01T00:00:00.000Z",
            available_at=None,
        )

        request = SnapshotBuildRequest.model_construct(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000103",
            run_id="00000000-0000-4000-8000-000000000203",
            package_id=PACKAGE_ID,
            repository=REPO_IDENTITY,
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=[],
            activities=[unverified_activity],
            coverage=[make_coverage("commits", status="complete")],
        )

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)

        assert result.status == "failed"
        assert result.snapshot is None
        assert len(result.errors) > 0
        assert result.errors[0].code == "validation_error"
        assert "available_at" in result.errors[0].message
        validate_snapshot_pair(request, result)

    def test_mismatched_package_id_rejection(self) -> None:
        bad_release = make_release("1.0.0", "2024-01-01T00:00:00.000Z", package_id="pypi:other-pkg")
        request = SnapshotBuildRequest.model_construct(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000104",
            run_id="00000000-0000-4000-8000-000000000204",
            package_id=PACKAGE_ID,
            repository=REPO_IDENTITY,
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=[bad_release],
            activities=[],
            coverage=[make_coverage("releases", status="complete")],
        )

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)

        assert result.status == "failed"
        assert result.snapshot is None
        assert len(result.errors) > 0
        validate_snapshot_pair(request, result)


class TestFeatureBuilderPolarsRoundTrip:
    """Test 5: Polars DataFrame Conversion & Round-Trip."""

    def test_polars_round_trip_complete(self) -> None:
        coverage = [
            make_coverage("releases", status="complete", records_observed=1),
            make_coverage("commits", status="complete", records_observed=1),
            make_coverage("contributors", status="complete", records_observed=1),
            make_coverage("issues", status="complete", records_observed=1),
            make_coverage("pull_requests", status="complete", records_observed=1),
        ]
        releases = [make_release("1.0.0", "2024-01-01T00:00:00.000Z")]
        activities = [
            make_activity("c1", "commit", "2024-01-01T00:00:00.000Z"),
            make_activity("i1", "issue_opened", "2024-01-01T00:00:00.000Z"),
            make_activity("p1", "pr_opened", "2024-01-01T00:00:00.000Z"),
        ]
        request = SnapshotBuildRequest(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000105",
            run_id="00000000-0000-4000-8000-000000000205",
            package_id=PACKAGE_ID,
            repository=REPO_IDENTITY,
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=releases,
            activities=activities,
            coverage=coverage,
        )

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)
        snapshot = result.snapshot
        assert snapshot is not None

        # Convert to polars
        df = builder.snapshots_to_polars([snapshot])
        assert isinstance(df, pl.DataFrame)
        assert len(df) == 1

        # Check key and feature columns
        assert "package_id" in df.columns
        assert "cutoff_at" in df.columns
        for col in FEATURE_COLUMNS:
            assert col in df.columns
            assert df.schema[col] == pl.Int64

        # Provenance columns exist
        assert "coverage_json" in df.columns
        assert "missing_fields_json" in df.columns
        assert "raw_record_ids_json" in df.columns

        # Reconstruct and compare
        reconstructed = builder.polars_to_snapshots(df)
        assert len(reconstructed) == 1
        assert reconstructed[0] == snapshot

    def test_polars_round_trip_partial(self) -> None:
        req_path = ROOT / "examples/contracts/snapshot_build_request.json"
        request = SnapshotBuildRequest.model_validate_json(req_path.read_text(encoding="utf-8"))

        builder = FeatureBuilder()
        result = builder.build_snapshot(request)
        snapshot = result.snapshot
        assert snapshot is not None

        df = builder.snapshots_to_polars([snapshot])
        assert len(df) == 1

        reconstructed = builder.polars_to_snapshots(df)
        assert len(reconstructed) == 1
        assert reconstructed[0] == snapshot

    def test_polars_empty(self) -> None:
        builder = FeatureBuilder()
        empty_df = builder.snapshots_to_polars([])
        assert isinstance(empty_df, pl.DataFrame)
        assert empty_df.is_empty()
        assert "package_id" in empty_df.columns
        assert builder.polars_to_snapshots(empty_df) == []

    def test_legacy_build_snapshot_features(self) -> None:
        builder = FeatureBuilder()
        df = pl.DataFrame({"a": [1, 2, 3]})
        assert builder.build_snapshot_features(df).equals(df)
        assert builder.build_snapshot_features(pl.DataFrame()).is_empty()
