"""Integration tests for FeatureService, FeaturesPipeline, and build-features CLI (T-022)."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import polars as pl
import pytest
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.contracts.analysis import OfficialStatus
from reposeer.contracts.models import Coverage, TimeRange
from reposeer.features.builder import FeatureBuilder
from reposeer.pipelines.features import FeaturesPipeline
from reposeer.storage.features.parquet import FeatureStorage
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.dataset_writer import DATASET_SCHEMAS
from reposeer.storage.processed.parquet import ParquetStorage


def _make_repo_row(
    repo_id: str = "github:repository:900000001",
    github_repo_id: str = "900000001",
    repo_full_name: str = "synthetic/demo-pkg",
    stargazers_count: int = 1500,
    forks_count: int = 300,
    open_issues_and_prs_count: int = 25,
) -> dict:
    return {
        "schema_version": "0.1.0",
        "repo_id": repo_id,
        "github_repo_id": github_repo_id,
        "repo_full_name": repo_full_name,
        "repo_url": f"https://github.com/{repo_full_name}",
        "retrieved_at": "2025-01-01T00:00:00.000Z",
        "valid_at": "2024-01-01T00:00:00.000Z",
        "available_at": "2024-01-01T00:00:00.000Z",
        "temporal_basis": "historical_reconstruction",
        "source": "github_rest",
        "source_url": f"https://api.github.com/repos/{repo_full_name}",
        "raw_record_id": f"raw:{uuid4()}",
        "is_archived": False,
        "is_fork": False,
        "is_template": False,
        "stargazers_count": stargazers_count,
        "forks_count": forks_count,
        "open_issues_and_prs_count": open_issues_and_prs_count,
    }


def _make_activity_row(
    repo_id: str,
    activity_id: str,
    activity_kind: str,
    event_at: str,
    source_object_id: str,
    actor_id: str | None = "1001",
    actor_is_bot: bool | None = False,
    available_at: str | None = None,
    valid_at: str | None = None,
) -> dict:
    av = available_at or event_at
    va = valid_at or event_at
    ret = max("2025-05-01T00:00:00.000Z", event_at, va, av)
    return {
        "schema_version": "0.1.0",
        "activity_id": activity_id,
        "repo_id": repo_id,
        "activity_kind": activity_kind,
        "source_object_id": source_object_id,
        "event_at": event_at,
        "actor_id": actor_id,
        "actor_is_bot": actor_is_bot,
        "raw_record_id": f"raw:{uuid4()}",
        "run_id": str(uuid4()),
        "source": "github_rest",
        "source_url": f"https://api.github.com/activity/{activity_id}",
        "retrieved_at": ret,
        "valid_at": va,
        "available_at": av,
        "temporal_basis": "historical_reconstruction",
        "missing_fields_json": "[]",
    }


def _make_release_row(
    package_id: str,
    repo_id: str,
    version: str,
    uploaded_at: str,
    available_at: str | None = None,
    valid_at: str | None = None,
) -> dict:
    av = available_at or uploaded_at
    va = valid_at or uploaded_at
    ret = max("2025-05-01T00:00:00.000Z", uploaded_at, va, av)
    return {
        "schema_version": "0.1.0",
        "package_id": package_id,
        "repo_id": repo_id,
        "version": version,
        "uploaded_at": uploaded_at,
        "valid_at": va,
        "available_at": av,
        "requires_python": ">=3.10",
        "source": "pypi_json",
        "source_url": f"https://pypi.org/pypi/{package_id.split(':')[-1]}/json",
        "retrieved_at": ret,
        "temporal_basis": "historical_reconstruction",
        "raw_record_id": f"raw:{uuid4()}",
    }


class TestFeaturePipelineEndToEnd:
    """End-to-end integration tests for T-022."""

    def test_e2e_processed_to_features_multi_cutoff(self, tmp_path: Path) -> None:
        """Test 1: Multiple cutoffs (3m/6m/12m) verify point-in-time boundary enforcement."""
        paths = StoragePaths(base_dir=tmp_path)
        storage = ParquetStorage()

        repo_id = "github:repository:900000001"
        pkg_id = "pypi:demo-pkg"

        # 1. Seed repositories.parquet
        repo_row = _make_repo_row(repo_id=repo_id, repo_full_name="synthetic/demo-pkg")
        storage.write(
            paths.processed_path("repositories"),
            pl.DataFrame([repo_row], schema=DATASET_SCHEMAS["repositories"]),
        )

        # 2. Seed github_activity.parquet across 2023-2024
        activities = [
            # 2023-Q4: commit
            _make_activity_row(repo_id, "github:activity:900000001:commit:c1", "commit", "2023-10-15T00:00:00.000Z", "c1"),
            # 2024-Q1: commit + issue
            _make_activity_row(repo_id, "github:activity:900000001:commit:c2", "commit", "2024-01-10T00:00:00.000Z", "c2"),
            _make_activity_row(repo_id, "github:activity:900000001:issue_opened:i1", "issue_opened", "2024-02-15T00:00:00.000Z", "i1"),
            # 2024-Q2: commit + PRs (post Q1 cutoff 2024-03-31, pre Q2 cutoff 2024-06-30)
            _make_activity_row(repo_id, "github:activity:900000001:commit:c3", "commit", "2024-04-10T00:00:00.000Z", "c3"),
            _make_activity_row(repo_id, "github:activity:900000001:pr_opened:p1", "pr_opened", "2024-05-15T00:00:00.000Z", "p1"),
            _make_activity_row(repo_id, "github:activity:900000001:pr_merged:p2", "pr_merged", "2024-05-20T00:00:00.000Z", "p2"),
            # 2024-Q3: commit (post both cutoffs)
            _make_activity_row(repo_id, "github:activity:900000001:commit:c4", "commit", "2024-08-10T00:00:00.000Z", "c4"),
        ]
        storage.write(
            paths.processed_path("github_activity"),
            pl.DataFrame(activities, schema=DATASET_SCHEMAS["github_activity"]),
        )

        # 3. Seed package releases
        releases = [
            _make_release_row(pkg_id, repo_id, "1.0.0", "2023-11-01T00:00:00.000Z"),
            _make_release_row(pkg_id, repo_id, "1.1.0", "2024-05-01T00:00:00.000Z"),  # Q2 release
            _make_release_row(pkg_id, repo_id, "2.0.0", "2024-09-01T00:00:00.000Z"),  # Q3 release
        ]
        storage.write(paths.processed_path("package_releases"), pl.DataFrame(releases))

        # 4. Run FeaturesPipeline across 2 cutoffs: 2024-Q1 and 2024-Q2
        cutoffs = ["2024-03-31T23:59:59.999Z", "2024-06-30T23:59:59.999Z"]
        pipeline = FeaturesPipeline(paths=paths)
        summary = pipeline.run(
            targets=[(pkg_id, repo_id, cutoffs[0]), (pkg_id, repo_id, cutoffs[1])],
            persist=True,
        )

        assert len(summary.results) == 2
        assert all(r.status == "complete" for r in summary.results)
        assert summary.snapshots_df.height == 2

        # 5. Verify Q1 snapshot (excludes 2024-Q2 and 2024-Q3 events)
        res_q1 = next(r for r in summary.results if r.snapshot.cutoff_at == cutoffs[0])
        snap_q1 = res_q1.snapshot
        assert snap_q1 is not None
        feat_q1 = snap_q1.features

        assert feat_q1.releases_total == 1  # 1.0.0 only
        assert feat_q1.commit_count_3m == 1  # c2 (2024-01-10)
        assert feat_q1.commit_count_6m == 2  # c2 + c1 (2023-10-15)
        assert feat_q1.commit_count_12m == 2
        assert feat_q1.issue_opened_count_12m == 1  # i1
        assert feat_q1.pr_opened_count_12m == 0  # p1 not yet happened

        # 6. Verify Q2 snapshot (includes 2024-Q2 events, excludes 2024-Q3)
        res_q2 = next(r for r in summary.results if r.snapshot.cutoff_at == cutoffs[1])
        snap_q2 = res_q2.snapshot
        assert snap_q2 is not None
        feat_q2 = snap_q2.features

        assert feat_q2.releases_total == 2  # 1.0.0 + 1.1.0
        assert feat_q2.commit_count_3m == 1  # c3 (2024-04-10)
        assert feat_q2.commit_count_6m == 2  # c3 + c2 (2024-01-10)
        assert feat_q2.commit_count_12m == 3  # c3 + c2 + c1
        assert feat_q2.pr_opened_count_12m == 1  # p1
        assert feat_q2.pr_merged_count_12m == 1  # p2

        # 7. Verify persisted Parquet file and lossless round-trip
        parquet_path = paths.features_path("repository_package_snapshots")
        assert parquet_path.is_file()

        feat_storage = FeatureStorage()
        loaded_df = feat_storage.load_features(parquet_path)
        assert loaded_df.height == 2

        reconstructed_snapshots = FeatureBuilder().polars_to_snapshots(loaded_df)
        assert len(reconstructed_snapshots) == 2
        assert {s.cutoff_at for s in reconstructed_snapshots} == set(cutoffs)

    def test_anti_leakage_and_missing_is_not_zero(self, tmp_path: Path) -> None:
        """Test 2: Anti-leakage and missing-is-not-zero gating for incomplete/unavailable groups."""
        paths = StoragePaths(base_dir=tmp_path)
        storage = ParquetStorage()

        repo_id = "github:repository:900000002"
        pkg_id = "pypi:leakage-test-pkg"
        cutoff = "2024-03-31T23:59:59.999Z"

        # Seed repositories.parquet with current counters that MUST NOT leak into features
        repo_row = _make_repo_row(
            repo_id=repo_id,
            github_repo_id="900000002",
            repo_full_name="synthetic/leakage-test-pkg",
            stargazers_count=50000,
            forks_count=12000,
            open_issues_and_prs_count=120,
        )
        storage.write(
            paths.processed_path("repositories"),
            pl.DataFrame([repo_row], schema=DATASET_SCHEMAS["repositories"]),
        )

        # Seed commits and releases
        act_row = _make_activity_row(
            repo_id, "github:activity:900000002:commit:c1", "commit", "2024-01-10T00:00:00.000Z", "c1"
        )
        storage.write(
            paths.processed_path("github_activity"),
            pl.DataFrame([act_row], schema=DATASET_SCHEMAS["github_activity"]),
        )
        rel_row = _make_release_row(pkg_id, repo_id, "1.0.0", "2024-01-01T00:00:00.000Z")
        storage.write(paths.processed_path("package_releases"), pl.DataFrame([rel_row]))

        # Omit 'issues' coverage group explicitly
        tr = TimeRange(start_at="2023-01-01T00:00:00.000Z", end_at=cutoff)
        cov_override = [
            Coverage(data_group="releases", status="complete", time_range=tr, records_observed=1, missing_reason=None),
            Coverage(data_group="commits", status="complete", time_range=tr, records_observed=1, missing_reason=None),
            Coverage(data_group="contributors", status="complete", time_range=tr, records_observed=1, missing_reason=None),
            Coverage(data_group="pull_requests", status="complete", time_range=tr, records_observed=0, missing_reason=None),
            # 'issues' is omitted!
        ]

        pipeline = FeaturesPipeline(paths=paths)
        summary = pipeline.run(
            targets=[(pkg_id, repo_id, cutoff)],
            coverage_overrides={"default": cov_override},
            persist=True,
        )

        assert len(summary.results) == 1
        res = summary.results[0]
        assert res.status == "partial"
        snap = res.snapshot
        assert snap is not None

        # Verify no counter fields leaked into features dictionary
        feat_dict = snap.features.model_dump()
        for forbidden in ("stargazers_count", "forks_count", "open_issues_and_prs_count", "stars", "forks"):
            assert forbidden not in feat_dict

        # Verify missing-is-not-zero gate on omitted issues
        assert feat_dict["issue_opened_count_12m"] is None
        assert feat_dict["issue_closed_count_12m"] is None

        # Verify exact MissingField audit trails
        missing_paths = {m.field_path: m for m in snap.missing_fields}
        assert "features.issue_opened_count_12m" in missing_paths
        assert "features.issue_closed_count_12m" in missing_paths
        assert missing_paths["features.issue_opened_count_12m"].reason == "not_collected"

    def test_independent_d09_labels_joined_cleanly(self, tmp_path: Path) -> None:
        """Test 3: FeaturesPipeline + MaintenancePipeline independent D09 label generation."""
        paths = StoragePaths(base_dir=tmp_path)
        storage = ParquetStorage()
        cutoff = "2024-03-31T23:59:59.999Z"
        obs_end = "2025-04-15T00:00:00.000Z"

        # 4 Packages representing the 4 D09 label outcomes
        targets_info = [
            ("pypi:pkg-pos", "github:repository:900000010"),
            ("pypi:pkg-neg", "github:repository:900000020"),
            ("pypi:pkg-unk", "github:repository:900000030"),
            ("pypi:pkg-exc", "github:repository:900000040"),
        ]

        repo_rows = [
            _make_repo_row(repo_id=rep, github_repo_id=rep.split(":")[-1], repo_full_name=f"synthetic/{pkg.split(':')[-1]}")
            for pkg, rep in targets_info
        ]
        storage.write(
            paths.processed_path("repositories"),
            pl.DataFrame(repo_rows, schema=DATASET_SCHEMAS["repositories"]),
        )

        # Activity setup:
        # - pkg-neg has human activity in horizon
        # - pkg-pos has pre-cutoff activity
        activities = [
            _make_activity_row("github:repository:900000010", "github:activity:900000010:commit:c1", "commit", "2024-01-01T00:00:00.000Z", "c1"),
            _make_activity_row("github:repository:900000020", "github:activity:900000020:commit:c2", "commit", "2024-01-01T00:00:00.000Z", "c2"),
            _make_activity_row("github:repository:900000020", "github:activity:900000020:commit:c_h", "commit", "2024-07-01T00:00:00.000Z", "c_h"),  # in horizon
            _make_activity_row("github:repository:900000030", "github:activity:900000030:commit:c3", "commit", "2024-01-01T00:00:00.000Z", "c3"),
            _make_activity_row("github:repository:900000040", "github:activity:900000040:commit:c4", "commit", "2024-01-01T00:00:00.000Z", "c4"),
        ]
        storage.write(
            paths.processed_path("github_activity"),
            pl.DataFrame(activities, schema=DATASET_SCHEMAS["github_activity"]),
        )

        # Official status events:
        # - pkg-pos: confirmed cessation in horizon (positive)
        # - pkg-exc: confirmed cessation effective BEFORE cutoff (already abandoned, excluded)
        ev_pos = OfficialStatus(
            event_type="ARCHIVED",
            cessation_confirmed=True,
            announced_at="2024-05-01T00:00:00.000Z",
            effective_at="2024-06-01T00:00:00.000Z",
            verified_at="2024-06-15T00:00:00.000Z",
            revoked_at=None,
            ledger_revision="rev-001",
            verifier="analyst:test",
            verification_policy_version="0.1.0",
            evidence_ids=["src:test-evidence-1"],
        )
        ev_exc = OfficialStatus(
            event_type="MAINTENANCE_CEASED",
            cessation_confirmed=True,
            announced_at="2023-11-01T00:00:00.000Z",
            effective_at="2023-12-01T00:00:00.000Z",
            verified_at="2023-12-05T00:00:00.000Z",
            revoked_at=None,
            ledger_revision="rev-002",
            verifier="analyst:test",
            verification_policy_version="0.1.0",
            evidence_ids=["src:test-evidence-2"],
        )

        pipeline = FeaturesPipeline(paths=paths)

        # Evaluate target by target with their respective official events
        targets = [(pkg, rep, cutoff) for pkg, rep in targets_info]

        summary_pos = pipeline.run(
            targets=[(targets_info[0][0], targets_info[0][1], cutoff)],
            evaluate_labels=True,
            official_events=[ev_pos],
            observation_end_at=obs_end,
            persist=True,
        )
        summary_neg = pipeline.run(
            targets=[(targets_info[1][0], targets_info[1][1], cutoff)],
            evaluate_labels=True,
            observation_end_at=obs_end,
            persist=True,
        )
        summary_unk = pipeline.run(
            targets=[(targets_info[2][0], targets_info[2][1], cutoff)],
            evaluate_labels=True,
            observation_end_at=obs_end,
            persist=True,
        )
        summary_exc = pipeline.run(
            targets=[(targets_info[3][0], targets_info[3][1], cutoff)],
            evaluate_labels=True,
            official_events=[ev_exc],
            observation_end_at=obs_end,
            persist=True,
        )

        # Read back combined package_labels.parquet
        labels_path = paths.features_path("package_labels")
        assert labels_path.is_file()
        labels_df = pl.read_parquet(labels_path)
        assert labels_df.height == 4

        # Validate each cohort label outcome
        labels_by_pkg = {
            row["package_id"]: row for row in labels_df.iter_rows(named=True)
        }

        # 1. Positive: label = 1
        pos_rec = labels_by_pkg["pypi:pkg-pos"]
        assert pos_rec["label"] == 1
        assert pos_rec["label_status"] == "positive"
        assert pos_rec["in_risk_set"] is True

        # 2. Negative: label = 0
        neg_rec = labels_by_pkg["pypi:pkg-neg"]
        assert neg_rec["label"] == 0
        assert neg_rec["label_status"] == "negative"
        assert neg_rec["in_risk_set"] is True

        # 3. Unknown (silent_unverified): label = None
        unk_rec = labels_by_pkg["pypi:pkg-unk"]
        assert unk_rec["label"] is None
        assert unk_rec["label_status"] == "unknown"
        assert unk_rec["censor_reason"] == "silent_unverified"
        assert unk_rec["in_risk_set"] is True

        # 4. Excluded: label = None, in_risk_set = False
        exc_rec = labels_by_pkg["pypi:pkg-exc"]
        assert exc_rec["label"] is None
        assert exc_rec["label_status"] == "excluded_known_status"
        assert exc_rec["censor_reason"] == "already_abandoned_at_cutoff"
        assert exc_rec["in_risk_set"] is False

        # Verify snapshots table and labels table join cleanly without mutating PackageSnapshot
        snapshots_df = pl.read_parquet(paths.features_path("repository_package_snapshots"))
        joined = snapshots_df.join(labels_df, on=["package_id", "cutoff_at"])
        assert joined.height == 4
        assert "package_age_days" in joined.columns
        assert "label" in joined.columns
        assert "censor_reason" in joined.columns

    def test_cli_execution_reproducible(self, tmp_path: Path) -> None:
        """Test 4: Typer CLI runner with --request-file and processed parquet arguments."""
        runner = CliRunner()

        # 1. Test CLI with --request-file
        req_file = Path("examples/contracts/snapshot_build_request.json")
        out_custom = tmp_path / "custom_features" / "features.parquet"

        res_file = runner.invoke(
            app,
            [
                "build-features",
                "--data-dir",
                str(tmp_path),
                "--request-file",
                str(req_file),
                "--output-path",
                str(out_custom),
            ],
        )
        assert res_file.exit_code == 0
        assert "Snapshots built: 1 total" in res_file.stdout
        assert out_custom.is_file()

        custom_df = pl.read_parquet(out_custom)
        assert custom_df.height == 1
        assert custom_df["package_id"][0] == "pypi:demo-maintenance-package"

        # 2. Test CLI against seeded processed parquet tables
        paths = StoragePaths(base_dir=tmp_path)
        storage = ParquetStorage()
        repo_row = _make_repo_row(
            repo_id="github:repository:900000005",
            github_repo_id="900000005",
            repo_full_name="synthetic/cli-demo",
        )
        storage.write(
            paths.processed_path("repositories"),
            pl.DataFrame([repo_row], schema=DATASET_SCHEMAS["repositories"]),
        )
        act_row = _make_activity_row(
            "github:repository:900000005",
            "github:activity:900000005:commit:cli_c1",
            "commit",
            "2024-01-01T00:00:00.000Z",
            "cli_c1",
        )
        storage.write(
            paths.processed_path("github_activity"),
            pl.DataFrame([act_row], schema=DATASET_SCHEMAS["github_activity"]),
        )

        res_processed = runner.invoke(
            app,
            [
                "build-features",
                "--data-dir",
                str(tmp_path),
                "--package-id",
                "pypi:cli-demo",
                "--repo-id",
                "github:repository:900000005",
                "--cutoff-at",
                "2024-03-31T23:59:59.999Z",
            ],
        )
        assert res_processed.exit_code == 0
        assert "Snapshots built: 1 total" in res_processed.stdout
        assert paths.features_path("repository_package_snapshots").is_file()
