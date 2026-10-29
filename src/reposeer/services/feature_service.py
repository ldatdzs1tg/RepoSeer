"""Feature engineering service bridging storage datasets and FeatureBuilder."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

import polars as pl

from reposeer.contracts.adapters.github_repository import canonical_github_url
from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    Coverage,
    DataError,
    DataGroup,
    PackageId,
    PackageRelease,
    PackageSnapshot,
    Provenance,
    RepoId,
    RepoIdentity,
    RepositoryActivity,
    SnapshotBuildRequest,
    SnapshotBuildResult,
    TimeRange,
    UtcTimestamp,
)
from reposeer.contracts.validation import validate_snapshot_pair
from reposeer.features.builder import FeatureBuilder
from reposeer.features.repository import assert_no_counter_leakage
from reposeer.pipelines.maintenance import MaintenancePipeline
from reposeer.storage.features.parquet import FeatureStorage
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.query.duckdb import DuckDBQueryEngine


def _make_error(message: str) -> DataError:
    return DataError(
        code="validation_error",
        message=message,
        retryable=False,
        source_record_id=None,
        raw_record_id=None,
    )


class FeatureService:
    """Bridges processed columnar datasets with deterministic FeatureBuilder."""

    def __init__(
        self,
        paths: StoragePaths | None = None,
        feature_builder: FeatureBuilder | None = None,
        maintenance_pipeline: MaintenancePipeline | None = None,
        storage: FeatureStorage | None = None,
        query_engine: DuckDBQueryEngine | None = None,
    ) -> None:
        self.paths = paths or StoragePaths()
        self.feature_builder = feature_builder or FeatureBuilder()
        self.maintenance_pipeline = maintenance_pipeline or MaintenancePipeline()
        self.storage = storage or FeatureStorage()
        self.query_engine = query_engine
        self.parquet_storage = ParquetStorage()

    def _read_processed(self, entity: str) -> pl.DataFrame:
        """Read processed dataset via DuckDBQueryEngine or ParquetStorage."""
        path = self.paths.processed_path(entity)
        if not path.is_file():
            return pl.DataFrame()
        if self.query_engine is not None:
            try:
                self.query_engine.register_parquet(entity, path)
                return self.query_engine.query_to_polars(f"SELECT * FROM {entity}")
            except Exception:
                return self.parquet_storage.read(path)
        return self.parquet_storage.read(path)

    def _persist_snapshots(
        self,
        snapshots: Sequence[PackageSnapshot],
        target_path: Path | None = None,
    ) -> None:
        """Persist snapshots to Parquet deduplicated on (package_id, cutoff_at)."""
        if not snapshots:
            return
        dest = target_path or self.paths.features_path("repository_package_snapshots")
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.is_file():
            existing_df = self.storage.load_features(dest)
            existing_snapshots = (
                self.feature_builder.polars_to_snapshots(existing_df)
                if not existing_df.is_empty()
                else []
            )
            snapshot_map = {
                (s.package_id, s.cutoff_at): s for s in existing_snapshots
            }
        else:
            snapshot_map = {}

        for s in snapshots:
            snapshot_map[(s.package_id, s.cutoff_at)] = s

        all_snapshots = list(snapshot_map.values())
        out_df = self.feature_builder.snapshots_to_polars(all_snapshots)
        self.storage.save_features(dest, out_df)

    def build_from_request(
        self,
        request: SnapshotBuildRequest,
        *,
        persist: bool = True,
    ) -> SnapshotBuildResult:
        """Build snapshot from contract request and optionally persist."""
        result = self.feature_builder.build_snapshot(request)
        validate_snapshot_pair(request, result)
        if persist and result.snapshot is not None:
            self._persist_snapshots([result.snapshot])
        return result

    def build_from_processed(
        self,
        *,
        targets: Sequence[tuple[PackageId, RepoId, UtcTimestamp]] | None = None,
        cutoffs: Sequence[UtcTimestamp] | None = None,
        coverage_overrides: Mapping[str, list[Coverage]] | None = None,
        releases_override: Sequence[PackageRelease] | None = None,
        persist: bool = True,
    ) -> list[SnapshotBuildResult]:
        """Reconstruct canonical entities from processed Parquet and build snapshots."""
        repos_df = self._read_processed("repositories")
        if repos_df.is_empty():
            return []

        # Index repository records by repo_id
        repos_by_id: dict[str, dict] = {}
        for row in repos_df.iter_rows(named=True):
            repos_by_id[row["repo_id"]] = row

        # Resolve concrete (package_id, repo_id, cutoff_at) targets
        concrete_targets: list[tuple[PackageId, RepoId, UtcTimestamp]] = []
        if targets is not None:
            for t in targets:
                if len(t) == 3:
                    concrete_targets.append(t)
                elif len(t) == 2:
                    pkg_id, rep_id = t[0], t[1]
                    if cutoffs:
                        for c in cutoffs:
                            concrete_targets.append((pkg_id, rep_id, c))
                    else:
                        r_row = repos_by_id.get(rep_id)
                        c = r_row.get("valid_at") or r_row.get("retrieved_at") if r_row else None
                        if c:
                            concrete_targets.append((pkg_id, rep_id, c))
        else:
            for rep_id, r_row in repos_by_id.items():
                repo_full_name = r_row["repo_full_name"]
                pkg_name = repo_full_name.split("/")[-1].lower()
                pkg_id = f"pypi:{pkg_name}"
                if cutoffs:
                    for c in cutoffs:
                        concrete_targets.append((pkg_id, rep_id, c))
                else:
                    c = r_row.get("valid_at") or r_row.get("retrieved_at")
                    if c:
                        concrete_targets.append((pkg_id, rep_id, c))

        # Read activity dataset
        activities_df = self._read_processed("github_activity")
        has_activity_table = not activities_df.is_empty()

        # Read releases dataset
        releases_df = self._read_processed("package_releases")
        if releases_df.is_empty():
            releases_df = self._read_processed("releases")
        has_releases_table = not releases_df.is_empty()

        results: list[SnapshotBuildResult] = []
        snapshots_to_persist: list[PackageSnapshot] = []

        for pkg_id, rep_id, cutoff in concrete_targets:
            r_row = repos_by_id.get(rep_id)
            if r_row is None:
                fail_res = SnapshotBuildResult(
                    schema_version="0.1.0",
                    record_type="snapshot_build_result",
                    request_id=str(uuid4()),
                    run_id=str(uuid4()),
                    builder_version=self.feature_builder.BUILDER_VERSION,
                    status="failed",
                    snapshot=None,
                    errors=[_make_error(f"Repository {rep_id} not found in processed repositories")],
                )
                results.append(fail_res)
                continue

            # Strict Projection: never read counters
            repo_full_name = r_row["repo_full_name"]
            repo_identity = RepoIdentity(
                repo_id=rep_id,
                github_repo_id=str(r_row["github_repo_id"]),
                github_node_id=r_row.get("github_node_id"),
                repo_full_name=repo_full_name,
                repo_url=r_row.get("repo_url") or canonical_github_url(repo_full_name),
                known_urls=[],
            )
            assert_no_counter_leakage(repo_identity.model_dump())

            # Strict point-in-time filtering on activities
            acts_for_target: list[RepositoryActivity] = []
            if has_activity_table:
                matching_acts = activities_df.filter(
                    (pl.col("repo_id") == rep_id)
                    & (pl.col("event_at") <= cutoff)
                    & (pl.col("valid_at") <= cutoff)
                    & pl.col("available_at").is_not_null()
                    & (pl.col("available_at") <= cutoff)
                )

                # Group by activity_id to deduplicate and collect raw_record_ids
                grouped_acts: dict[str, list[dict]] = {}
                for a_row in matching_acts.iter_rows(named=True):
                    grouped_acts.setdefault(a_row["activity_id"], []).append(a_row)

                for act_id, rows in grouped_acts.items():
                    first = rows[0]
                    raw_ids: list[str] = []
                    for r in rows:
                        rid = r.get("raw_record_id")
                        if rid and rid not in raw_ids:
                            raw_ids.append(rid)
                    if not raw_ids:
                        raw_ids = [f"raw:{uuid4()}"]

                    event_at = first["event_at"]
                    valid_at = first["valid_at"]
                    avail_at = first["available_at"]
                    retrieved = first.get("retrieved_at") or cutoff
                    ret_at = max(retrieved, event_at, valid_at, avail_at)

                    missing_json = first.get("missing_fields_json")
                    missing = json.loads(missing_json) if missing_json else []

                    prov = Provenance(
                        source=first.get("source") or "github_rest",
                        source_url=first.get("source_url") or f"https://api.github.com/repos/{repo_full_name}/activity",
                        retrieved_at=ret_at,
                        valid_at=valid_at,
                        available_at=avail_at,
                        temporal_basis=first.get("temporal_basis") or "historical_reconstruction",
                        raw_record_ids=raw_ids,
                        missing_fields=missing,
                    )

                    actor_id_val = first.get("actor_id")
                    actor_id_str = str(actor_id_val) if actor_id_val is not None and str(actor_id_val).strip() else None
                    actor_bot_val = first.get("actor_is_bot")
                    actor_bot_bool = bool(actor_bot_val) if actor_bot_val is not None else None

                    acts_for_target.append(
                        RepositoryActivity(
                            schema_version="0.1.0",
                            record_type="repository_activity",
                            activity_id=act_id,
                            repo_id=rep_id,
                            activity_kind=first["activity_kind"],
                            source_object_id=str(first["source_object_id"]),
                            event_at=event_at,
                            actor_id=actor_id_str,
                            actor_is_bot=actor_bot_bool,
                            provenance=prov,
                        )
                    )

            # Strict point-in-time filtering on releases
            rels_for_target: list[PackageRelease] = []
            if releases_override is not None:
                for rel in releases_override:
                    if (
                        rel.package_id == pkg_id
                        and rel.uploaded_at <= cutoff
                        and rel.provenance.valid_at <= cutoff
                        and rel.provenance.available_at is not None
                        and rel.provenance.available_at <= cutoff
                    ):
                        rels_for_target.append(rel)
            elif has_releases_table:
                matching_rels = releases_df.filter(
                    (pl.col("package_id") == pkg_id)
                    & (pl.col("uploaded_at") <= cutoff)
                    & (pl.col("valid_at") <= cutoff)
                    & pl.col("available_at").is_not_null()
                    & (pl.col("available_at") <= cutoff)
                )
                for r_row in matching_rels.iter_rows(named=True):
                    up_at = r_row["uploaded_at"]
                    v_at = r_row["valid_at"]
                    av_at = r_row["available_at"]
                    ret_at = max(r_row.get("retrieved_at") or cutoff, up_at, v_at, av_at)
                    prov = Provenance(
                        source=r_row.get("source") or "pypi_json",
                        source_url=r_row.get("source_url") or f"https://pypi.org/pypi/{pkg_id.split(':')[-1]}/json",
                        retrieved_at=ret_at,
                        valid_at=v_at,
                        available_at=av_at,
                        temporal_basis=r_row.get("temporal_basis") or "historical_reconstruction",
                        raw_record_ids=[r_row.get("raw_record_id") or f"raw:{uuid4()}"],
                        missing_fields=[],
                    )
                    rels_for_target.append(
                        PackageRelease(
                            schema_version="0.1.0",
                            record_type="package_release",
                            package_id=pkg_id,
                            repo_id=rep_id,
                            version=str(r_row["version"]),
                            uploaded_at=up_at,
                            requires_python=r_row.get("requires_python") or ">=3.10",
                            dependencies=None,
                            is_yanked=None,
                            yanked_reason=None,
                            yanked_status_at=None,
                            provenance=prov,
                        )
                    )

            # Resolve Coverage
            cov_key_candidates = [
                f"{pkg_id}:{cutoff}",
                f"{rep_id}:{cutoff}",
                pkg_id,
                rep_id,
                "default",
            ]
            matched_coverage: list[Coverage] | None = None
            if coverage_overrides:
                for k in cov_key_candidates:
                    if k in coverage_overrides:
                        matched_coverage = list(coverage_overrides[k])
                        break

            if matched_coverage is not None:
                coverage = matched_coverage
            else:
                coverage = self._derive_default_coverage(
                    cutoff_at=cutoff,
                    releases=rels_for_target,
                    activities=acts_for_target,
                    has_releases_data=(has_releases_table or releases_override is not None),
                    has_activity_data=has_activity_table,
                )

            req = SnapshotBuildRequest(
                schema_version="0.1.0",
                record_type="snapshot_build_request",
                request_id=str(uuid4()),
                run_id=str(uuid4()),
                package_id=pkg_id,
                repository=repo_identity,
                cutoff_at=cutoff,
                horizon_months=12,
                releases=rels_for_target,
                activities=acts_for_target,
                coverage=coverage,
            )

            res = self.feature_builder.build_snapshot(req)
            validate_snapshot_pair(req, res)
            results.append(res)
            if res.snapshot is not None:
                snapshots_to_persist.append(res.snapshot)

        if persist and snapshots_to_persist:
            self._persist_snapshots(snapshots_to_persist)

        return results

    @staticmethod
    def _derive_default_coverage(
        *,
        cutoff_at: UtcTimestamp,
        releases: list[PackageRelease],
        activities: list[RepositoryActivity],
        has_releases_data: bool,
        has_activity_data: bool,
    ) -> list[Coverage]:
        """Construct canonical Coverage entries for core data groups."""
        covs: list[Coverage] = []
        default_start = add_calendar_months(cutoff_at, -12)

        # 1. Releases
        if releases:
            earliest = min(r.uploaded_at for r in releases)
            start_at = earliest if earliest < cutoff_at else default_start
            covs.append(
                Coverage(
                    data_group="releases",
                    status="complete",
                    time_range=TimeRange(start_at=start_at, end_at=cutoff_at),
                    records_observed=len(releases),
                    missing_reason=None,
                )
            )
        else:
            covs.append(
                Coverage(
                    data_group="releases",
                    status="unavailable",
                    time_range=None,
                    records_observed=0,
                    missing_reason="not_collected" if not has_releases_data else "unavailable",
                )
            )

        # 2. Commits & Contributors
        commits = [a for a in activities if a.activity_kind == "commit"]
        if commits:
            earliest = min(c.event_at for c in commits)
            start_at = earliest if earliest < cutoff_at else default_start
            covs.append(
                Coverage(
                    data_group="commits",
                    status="complete",
                    time_range=TimeRange(start_at=start_at, end_at=cutoff_at),
                    records_observed=len(commits),
                    missing_reason=None,
                )
            )
            covs.append(
                Coverage(
                    data_group="contributors",
                    status="complete",
                    time_range=TimeRange(start_at=start_at, end_at=cutoff_at),
                    records_observed=len({c.actor_id for c in commits if c.actor_id}),
                    missing_reason=None,
                )
            )
        elif has_activity_data:
            covs.append(
                Coverage(
                    data_group="commits",
                    status="complete",
                    time_range=TimeRange(start_at=default_start, end_at=cutoff_at),
                    records_observed=0,
                    missing_reason=None,
                )
            )
            covs.append(
                Coverage(
                    data_group="contributors",
                    status="complete",
                    time_range=TimeRange(start_at=default_start, end_at=cutoff_at),
                    records_observed=0,
                    missing_reason=None,
                )
            )
        else:
            covs.append(
                Coverage(
                    data_group="commits",
                    status="unavailable",
                    time_range=None,
                    records_observed=0,
                    missing_reason="not_collected",
                )
            )
            covs.append(
                Coverage(
                    data_group="contributors",
                    status="unavailable",
                    time_range=None,
                    records_observed=0,
                    missing_reason="not_collected",
                )
            )

        # 3. Issues
        issues = [
            a for a in activities if a.activity_kind in ("issue_opened", "issue_closed", "issue_reopened")
        ]
        if issues:
            earliest = min(i.event_at for i in issues)
            start_at = earliest if earliest < cutoff_at else default_start
            covs.append(
                Coverage(
                    data_group="issues",
                    status="complete",
                    time_range=TimeRange(start_at=start_at, end_at=cutoff_at),
                    records_observed=len(issues),
                    missing_reason=None,
                )
            )
        elif has_activity_data:
            covs.append(
                Coverage(
                    data_group="issues",
                    status="complete",
                    time_range=TimeRange(start_at=default_start, end_at=cutoff_at),
                    records_observed=0,
                    missing_reason=None,
                )
            )
        else:
            covs.append(
                Coverage(
                    data_group="issues",
                    status="unavailable",
                    time_range=None,
                    records_observed=0,
                    missing_reason="not_collected",
                )
            )

        # 4. Pull Requests
        prs = [
            a for a in activities if a.activity_kind in ("pr_opened", "pr_closed", "pr_reopened", "pr_merged")
        ]
        if prs:
            earliest = min(p.event_at for p in prs)
            start_at = earliest if earliest < cutoff_at else default_start
            covs.append(
                Coverage(
                    data_group="pull_requests",
                    status="complete",
                    time_range=TimeRange(start_at=start_at, end_at=cutoff_at),
                    records_observed=len(prs),
                    missing_reason=None,
                )
            )
        elif has_activity_data:
            covs.append(
                Coverage(
                    data_group="pull_requests",
                    status="complete",
                    time_range=TimeRange(start_at=default_start, end_at=cutoff_at),
                    records_observed=0,
                    missing_reason=None,
                )
            )
        else:
            covs.append(
                Coverage(
                    data_group="pull_requests",
                    status="unavailable",
                    time_range=None,
                    records_observed=0,
                    missing_reason="not_collected",
                )
            )

        return covs
