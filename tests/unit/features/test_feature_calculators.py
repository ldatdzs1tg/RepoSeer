"""Unit tests for domain feature calculators and temporal/coverage gates."""
from __future__ import annotations

import pytest

from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    Coverage,
    PackageRelease,
    PackageSnapshot,
    Provenance,
    RepoIdentity,
    RepositoryActivity,
    SnapshotBuildRequest,
    SnapshotBuildResult,
    SnapshotFeatures,
    TimeRange,
)
from reposeer.contracts.validation import validate_snapshot_pair
from reposeer.features.activity import ActivityFeatures
from reposeer.features.package import PackageFeatures
from reposeer.features.repository import (
    FORBIDDEN_COUNTER_FIELDS,
    RepositoryFeatures,
    assert_no_counter_leakage,
    compute_horizon_end_at,
    compute_lookback_boundaries,
    group_missing_reason,
    index_coverage,
    validate_snapshot_grain,
)

CUTOFF = "2024-03-31T23:59:59.999Z"
PACKAGE_ID = "pypi:demo-pkg"
REPO_ID = "github:repository:900000001"


def make_provenance(
    retrieved_at: str = "2024-03-31T23:59:59.999Z",
    valid_at: str = "2024-01-01T00:00:00.000Z",
    available_at: str | None = "2024-01-01T00:00:00.000Z",
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
    package_id: str = PACKAGE_ID,
    available_at: str | None = None,
    valid_at: str | None = None,
    raw_id: str = "raw:00000000-0000-4000-8000-000000000001",
) -> PackageRelease:
    av = available_at if available_at is not None else uploaded_at
    va = valid_at if valid_at is not None else uploaded_at
    prov = make_provenance(retrieved_at=CUTOFF, valid_at=va, available_at=av, raw_id=raw_id)
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
    activity_id_suffix: str,
    kind: str,
    event_at: str,
    repo_id: str = REPO_ID,
    actor_id: str | None = "1001",
    actor_is_bot: bool | None = False,
    available_at: str | None = None,
    valid_at: str | None = None,
    raw_id: str = "raw:00000000-0000-4000-8000-000000000002",
) -> RepositoryActivity:
    av = available_at if available_at is not None else event_at
    va = valid_at if valid_at is not None else event_at
    prov = make_provenance(retrieved_at=CUTOFF, valid_at=va, available_at=av, raw_id=raw_id)
    repo_num = repo_id.split(":")[-1]
    act_id = f"github:activity:{repo_num}:{kind}:{activity_id_suffix}"
    return RepositoryActivity(
        schema_version="0.1.0",
        record_type="repository_activity",
        activity_id=act_id,
        repo_id=repo_id,
        activity_kind=kind,
        event_at=event_at,
        actor_id=actor_id,
        actor_is_bot=actor_is_bot,
        source_object_id=activity_id_suffix,
        provenance=prov,
    )


def make_coverage(group: str, status: str = "complete", missing_reason: str | None = None) -> Coverage:
    if status == "complete":
        return Coverage(
            data_group=group,
            status="complete",
            time_range=TimeRange(start_at="2023-01-01T00:00:00.000Z", end_at=CUTOFF),
            records_observed=1,
            missing_reason=None,
        )
    return Coverage(
        data_group=group,
        status=status,
        time_range=None,
        records_observed=0,
        missing_reason=missing_reason or "unavailable",
    )


class TestRepositoryFeatures:
    def test_validate_snapshot_grain(self):
        validate_snapshot_grain("github:repository:123", "github:repository:123")
        with pytest.raises(ValueError, match="must equal repo_id"):
            validate_snapshot_grain("github:repository:123", "github:repository:456")

    def test_compute_horizon_end_at(self):
        # Normal month
        assert compute_horizon_end_at("2024-03-31T23:59:59.999Z", 12) == "2025-03-31T23:59:59.999Z"
        # Leap day clamping
        assert compute_horizon_end_at("2024-02-29T23:59:59.999Z", 12) == "2025-02-28T23:59:59.999Z"

    def test_compute_lookback_boundaries(self):
        bounds = compute_lookback_boundaries("2024-03-31T23:59:59.999Z")
        assert bounds["3m"] == "2023-12-31T23:59:59.999Z"
        assert bounds["6m"] == "2023-09-30T23:59:59.999Z"
        assert bounds["12m"] == "2023-03-31T23:59:59.999Z"

    def test_index_coverage(self):
        covs = [
            make_coverage("releases", "complete"),
            make_coverage("commits", "complete"),
        ]
        indexed = index_coverage(covs, CUTOFF)
        assert set(indexed.keys()) == {"releases", "commits"}

        # Duplicate detection
        with pytest.raises(ValueError, match="duplicates"):
            index_coverage([covs[0], covs[0]], CUTOFF)

        # Future coverage leak detection
        future_cov = Coverage(
            data_group="issues",
            status="complete",
            time_range=TimeRange(start_at="2023-01-01T00:00:00.000Z", end_at="2025-01-01T00:00:00.000Z"),
            records_observed=1,
            missing_reason=None,
        )
        with pytest.raises(ValueError, match="extends past cutoff"):
            index_coverage([future_cov], CUTOFF)

    def test_group_missing_reason(self):
        cov_map = {
            "releases": make_coverage("releases", "complete"),
            "commits": make_coverage("commits", "partial", "parse_error"),
        }
        assert group_missing_reason(cov_map, "releases") == (True, None, "Data group 'releases' coverage is complete")
        assert group_missing_reason(cov_map, "commits") == (False, "parse_error", "Data group 'commits' coverage status is 'partial'")
        assert group_missing_reason(cov_map, "issues") == (False, "not_collected", "Data group 'issues' not present in coverage")

    def test_assert_no_counter_leakage(self):
        assert_no_counter_leakage({"commit_count_3m": 5, "releases_total": 2})
        for forbidden in FORBIDDEN_COUNTER_FIELDS:
            with pytest.raises(ValueError, match="Anti-leakage violation"):
                assert_no_counter_leakage({forbidden: 10})


class TestExactWindowBoundaryInclusivity:
    def test_release_boundary_inclusivity(self):
        # Boundary for 3m is 2023-12-31T23:59:59.999Z
        bounds = compute_lookback_boundaries(CUTOFF)
        t_3m = bounds["3m"]

        # Exactly at cutoff - 3m: excluded from 3m, included in 6m and 12m
        r_boundary = make_release("1.0.0", t_3m)
        # Exactly at cutoff: included in 3m, 6m, and 12m
        r_cutoff = make_release("2.0.0", CUTOFF)

        cov_map = {"releases": make_coverage("releases", "complete")}
        res = PackageFeatures.compute(PACKAGE_ID, CUTOFF, [r_boundary, r_cutoff], cov_map)

        assert res.features["releases_total"] == 2
        assert res.features["release_count_3m"] == 1  # Only r_cutoff
        assert res.features["release_count_6m"] == 2  # Both
        assert res.features["release_count_12m"] == 2  # Both

    def test_activity_boundary_inclusivity(self):
        bounds = compute_lookback_boundaries(CUTOFF)
        t_3m = bounds["3m"]

        # Commit exactly at 3m boundary: excluded from 3m, included in 6m and 12m
        c_boundary = make_activity("c1", "commit", t_3m)
        # Commit exactly at cutoff: included in 3m, 6m, and 12m
        c_cutoff = make_activity("c2", "commit", CUTOFF)

        cov_map = {
            "commits": make_coverage("commits", "complete"),
            "contributors": make_coverage("contributors", "complete"),
            "issues": make_coverage("issues", "complete"),
            "pull_requests": make_coverage("pull_requests", "complete"),
        }
        res = ActivityFeatures.compute(REPO_ID, CUTOFF, [c_boundary, c_cutoff], cov_map)

        assert res.features["commit_count_3m"] == 1
        assert res.features["commit_count_6m"] == 2
        assert res.features["commit_count_12m"] == 2


class TestMonotonicity:
    def test_monotonicity_across_windows(self):
        releases = [
            make_release("1.0.0", "2023-01-01T00:00:00.000Z"),  # older than 12m
            make_release("1.1.0", "2023-05-01T00:00:00.000Z"),  # in 12m
            make_release("1.2.0", "2023-11-01T00:00:00.000Z"),  # in 6m, 12m
            make_release("1.3.0", "2024-02-01T00:00:00.000Z"),  # in 3m, 6m, 12m
        ]
        cov_map = {"releases": make_coverage("releases", "complete")}
        res = PackageFeatures.compute(PACKAGE_ID, CUTOFF, releases, cov_map)
        f = res.features

        assert f["release_count_3m"] == 1
        assert f["release_count_6m"] == 2
        assert f["release_count_12m"] == 3
        assert f["releases_total"] == 4
        assert f["release_count_3m"] <= f["release_count_6m"] <= f["release_count_12m"] <= f["releases_total"]
        assert f["days_since_latest_release"] is not None
        assert f["package_age_days"] is not None
        assert f["package_age_days"] >= f["days_since_latest_release"] >= 0


class TestBotCommitsAndContributors:
    def test_bot_commits_counted_in_commits_but_excluded_from_contributors(self):
        bot_commit = make_activity("c_bot", "commit", "2024-02-01T00:00:00.000Z", actor_id="999", actor_is_bot=True)
        human_commit_1 = make_activity("c_h1", "commit", "2024-02-02T00:00:00.000Z", actor_id="101", actor_is_bot=False)
        human_commit_2 = make_activity("c_h2", "commit", "2024-02-03T00:00:00.000Z", actor_id="102", actor_is_bot=False)

        cov_map = {
            "commits": make_coverage("commits", "complete"),
            "contributors": make_coverage("contributors", "complete"),
        }
        res = ActivityFeatures.compute(REPO_ID, CUTOFF, [bot_commit, human_commit_1, human_commit_2], cov_map)

        assert res.features["commit_count_3m"] == 3
        assert res.features["commit_count_6m"] == 3
        assert res.features["commit_count_12m"] == 3
        assert res.features["contributor_count_12m"] == 2  # Bot excluded

    def test_ambiguous_actor_sets_contributor_count_to_none_with_missing_field(self):
        # Non-bot commit without bot classification (actor_is_bot is None)
        unclassified_commit = make_activity("c_unclass", "commit", "2024-02-01T00:00:00.000Z", actor_id="101", actor_is_bot=None)
        cov_map = {
            "commits": make_coverage("commits", "complete"),
            "contributors": make_coverage("contributors", "complete"),
        }
        res = ActivityFeatures.compute(REPO_ID, CUTOFF, [unclassified_commit], cov_map)

        assert res.features["commit_count_12m"] == 1
        assert res.features["contributor_count_12m"] is None
        assert any(
            m.field_path == "features.contributor_count_12m" and m.reason == "ambiguous"
            for m in res.missing_fields
        )

    def test_missing_actor_id_sets_contributor_count_to_none(self):
        # actor_is_bot is False, but actor_id is None
        no_actor_commit = make_activity("c_no_actor", "commit", "2024-02-01T00:00:00.000Z", actor_id=None, actor_is_bot=False)
        cov_map = {
            "commits": make_coverage("commits", "complete"),
            "contributors": make_coverage("contributors", "complete"),
        }
        res = ActivityFeatures.compute(REPO_ID, CUTOFF, [no_actor_commit], cov_map)

        assert res.features["contributor_count_12m"] is None
        assert any(
            m.field_path == "features.contributor_count_12m" and m.reason == "ambiguous"
            for m in res.missing_fields
        )


class TestZeroReleasesEdgeCase:
    def test_zero_releases_with_complete_coverage(self):
        cov_map = {"releases": make_coverage("releases", "complete")}
        res = PackageFeatures.compute(PACKAGE_ID, CUTOFF, [], cov_map)

        assert res.features["releases_total"] == 0
        assert res.features["release_count_3m"] == 0
        assert res.features["release_count_6m"] == 0
        assert res.features["release_count_12m"] == 0
        assert res.features["package_age_days"] is None
        assert res.features["days_since_latest_release"] is None

        missing_paths = {m.field_path: m for m in res.missing_fields}
        assert set(missing_paths.keys()) == {
            "features.package_age_days",
            "features.days_since_latest_release",
        }
        assert missing_paths["features.package_age_days"].reason == "not_applicable"
        assert missing_paths["features.days_since_latest_release"].reason == "not_applicable"


class TestCoverageGating:
    def test_incomplete_coverage_forces_null_and_missing_fields(self):
        # Incomplete coverage for releases
        rel_cov = make_coverage("releases", "partial", "parse_error")
        res_pkg = PackageFeatures.compute(
            PACKAGE_ID,
            CUTOFF,
            [make_release("1.0.0", "2024-01-01T00:00:00.000Z")],
            {"releases": rel_cov},
        )
        for name in PackageFeatures.RELEASE_FEATURE_NAMES:
            assert res_pkg.features[name] is None
        assert len(res_pkg.missing_fields) == 6
        assert all(m.reason == "parse_error" for m in res_pkg.missing_fields)

        # Incomplete coverage for activity groups
        act_cov = {
            "commits": make_coverage("commits", "unavailable", "historical_unavailable"),
            "contributors": make_coverage("contributors", "unavailable", "historical_unavailable"),
            "issues": make_coverage("issues", "not_applicable", "not_applicable"),
            # pull_requests missing entirely -> not_collected
        }
        res_act = ActivityFeatures.compute(
            REPO_ID,
            CUTOFF,
            [make_activity("c1", "commit", "2024-01-01T00:00:00.000Z")],
            act_cov,
        )
        assert res_act.features["commit_count_3m"] is None
        assert res_act.features["commit_count_6m"] is None
        assert res_act.features["commit_count_12m"] is None
        assert res_act.features["contributor_count_12m"] is None
        assert res_act.features["issue_opened_count_12m"] is None
        assert res_act.features["issue_closed_count_12m"] is None
        assert res_act.features["pr_opened_count_12m"] is None
        assert res_act.features["pr_merged_count_12m"] is None

        # Verify reasons
        reasons = {m.field_path: m.reason for m in res_act.missing_fields}
        assert reasons["features.commit_count_3m"] == "historical_unavailable"
        assert reasons["features.contributor_count_12m"] == "historical_unavailable"
        assert reasons["features.issue_opened_count_12m"] == "not_applicable"
        assert reasons["features.pr_opened_count_12m"] == "not_collected"


class TestTemporalLeakageValidation:
    def test_post_cutoff_release_rejected(self):
        post_cutoff_release = make_release("2.0.0", "2024-04-01T00:00:00.000Z")
        cov_map = {"releases": make_coverage("releases", "complete")}
        with pytest.raises(ValueError, match="is after cutoff"):
            PackageFeatures.compute(PACKAGE_ID, CUTOFF, [post_cutoff_release], cov_map)

    def test_post_cutoff_activity_rejected(self):
        post_cutoff_act = make_activity("c_late", "commit", "2024-04-01T00:00:00.000Z")
        cov_map = {"commits": make_coverage("commits", "complete")}
        with pytest.raises(ValueError, match="> cutoff"):
            ActivityFeatures.compute(REPO_ID, CUTOFF, [post_cutoff_act], cov_map)

    def test_package_id_mismatch_rejected(self):
        wrong_pkg_release = make_release("1.0.0", "2024-01-01T00:00:00.000Z", package_id="pypi:other-pkg")
        cov_map = {"releases": make_coverage("releases", "complete")}
        with pytest.raises(ValueError, match="package_id mismatch"):
            PackageFeatures.compute(PACKAGE_ID, CUTOFF, [wrong_pkg_release], cov_map)

    def test_repo_id_mismatch_rejected(self):
        wrong_repo_act = make_activity("c1", "commit", "2024-01-01T00:00:00.000Z", repo_id="github:repository:999")
        cov_map = {"commits": make_coverage("commits", "complete")}
        with pytest.raises(ValueError, match="repo_id mismatch"):
            ActivityFeatures.compute(REPO_ID, CUTOFF, [wrong_repo_act], cov_map)


class TestFullContractIntegration:
    def test_snapshot_features_and_snapshot_pair_validation(self):
        raw_r1 = "raw:00000000-0000-4000-8000-000000000011"
        raw_a1 = "raw:00000000-0000-4000-8000-000000000012"

        release = make_release("1.0.0", "2024-01-01T00:00:00.000Z", raw_id=raw_r1)
        commit = make_activity("c1", "commit", "2024-01-01T00:00:00.000Z", actor_id="101", raw_id=raw_a1)

        coverages = [
            make_coverage("releases", "complete"),
            make_coverage("commits", "complete"),
            make_coverage("contributors", "complete"),
            make_coverage("issues", "complete"),
            make_coverage("pull_requests", "complete"),
        ]
        cov_map = index_coverage(coverages, CUTOFF)

        pkg_res = PackageFeatures.compute(PACKAGE_ID, CUTOFF, [release], cov_map)
        act_res = ActivityFeatures.compute(REPO_ID, CUTOFF, [commit], cov_map)

        all_features = {**pkg_res.features, **act_res.features}
        all_missing = [*pkg_res.missing_fields, *act_res.missing_fields]

        features = SnapshotFeatures(**all_features)
        assert features.releases_total == 1
        assert features.commit_count_3m == 1
        assert features.contributor_count_12m == 1

        snapshot = PackageSnapshot(
            schema_version="0.1.0",
            record_type="package_snapshot",
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            repo_group_id=REPO_ID,
            cutoff_at=CUTOFF,
            horizon_months=12,
            horizon_end_at=compute_horizon_end_at(CUTOFF, 12),
            features=features,
            coverage=coverages,
            missing_fields=all_missing,
            raw_record_ids=[raw_r1, raw_a1],
        )

        request = SnapshotBuildRequest(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000001",
            run_id="00000000-0000-4000-8000-000000000002",
            package_id=PACKAGE_ID,
            repository=RepoIdentity(
                repo_id=REPO_ID,
                github_repo_id="900000001",
                github_node_id=None,
                repo_full_name="demo/pkg",
                repo_url="https://github.com/demo/pkg",
                known_urls=[],
            ),
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=[release],
            activities=[commit],
            coverage=coverages,
        )

        result = SnapshotBuildResult(
            schema_version="0.1.0",
            record_type="snapshot_build_result",
            request_id="00000000-0000-4000-8000-000000000001",
            run_id="00000000-0000-4000-8000-000000000002",
            builder_version="0.1.0",
            status="complete",
            snapshot=snapshot,
            errors=[],
        )

        # Validate contract pair
        validate_snapshot_pair(request, result)

    def test_snapshot_pair_validation_with_missing_fields_partial_status(self):
        raw_a1 = "raw:00000000-0000-4000-8000-000000000012"
        commit = make_activity("c1", "commit", "2024-01-01T00:00:00.000Z", actor_id="101", raw_id=raw_a1)

        coverages = [
            make_coverage("releases", "complete"),
            make_coverage("commits", "complete"),
            make_coverage("contributors", "complete"),
            make_coverage("issues", "complete"),
            make_coverage("pull_requests", "complete"),
        ]
        cov_map = index_coverage(coverages, CUTOFF)

        # 0 releases -> package_age_days and days_since_latest_release are None
        pkg_res = PackageFeatures.compute(PACKAGE_ID, CUTOFF, [], cov_map)
        act_res = ActivityFeatures.compute(REPO_ID, CUTOFF, [commit], cov_map)

        all_features = {**pkg_res.features, **act_res.features}
        all_missing = [*pkg_res.missing_fields, *act_res.missing_fields]
        assert len(all_missing) == 2

        features = SnapshotFeatures(**all_features)
        snapshot = PackageSnapshot(
            schema_version="0.1.0",
            record_type="package_snapshot",
            package_id=PACKAGE_ID,
            repo_id=REPO_ID,
            repo_group_id=REPO_ID,
            cutoff_at=CUTOFF,
            horizon_months=12,
            horizon_end_at=compute_horizon_end_at(CUTOFF, 12),
            features=features,
            coverage=coverages,
            missing_fields=all_missing,
            raw_record_ids=[raw_a1],
        )

        request = SnapshotBuildRequest(
            schema_version="0.1.0",
            record_type="snapshot_build_request",
            request_id="00000000-0000-4000-8000-000000000003",
            run_id="00000000-0000-4000-8000-000000000004",
            package_id=PACKAGE_ID,
            repository=RepoIdentity(
                repo_id=REPO_ID,
                github_repo_id="900000001",
                github_node_id=None,
                repo_full_name="demo/pkg",
                repo_url="https://github.com/demo/pkg",
                known_urls=[],
            ),
            cutoff_at=CUTOFF,
            horizon_months=12,
            releases=[],
            activities=[commit],
            coverage=coverages,
        )

        result = SnapshotBuildResult(
            schema_version="0.1.0",
            record_type="snapshot_build_result",
            request_id="00000000-0000-4000-8000-000000000003",
            run_id="00000000-0000-4000-8000-000000000004",
            builder_version="0.1.0",
            status="partial",
            snapshot=snapshot,
            errors=[],
        )

        validate_snapshot_pair(request, result)

