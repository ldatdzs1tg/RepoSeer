"""Feature builder orchestrating feature extraction across subsystems."""
from __future__ import annotations

import json
import re
from typing import Literal, Sequence

import polars as pl

from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    Coverage,
    DataError,
    MissingField,
    PackageSnapshot,
    SnapshotBuildRequest,
    SnapshotBuildResult,
    SnapshotFeatures,
)
from reposeer.contracts.validation import validate_snapshot_pair
from reposeer.features.activity import ActivityFeatures
from reposeer.features.package import PackageFeatures

FEATURE_COLUMNS: tuple[str, ...] = (
    "package_age_days",
    "releases_total",
    "days_since_latest_release",
    "release_count_3m",
    "release_count_6m",
    "release_count_12m",
    "commit_count_3m",
    "commit_count_6m",
    "commit_count_12m",
    "contributor_count_12m",
    "issue_opened_count_12m",
    "issue_closed_count_12m",
    "pr_opened_count_12m",
    "pr_merged_count_12m",
)

POLARS_SNAPSHOT_SCHEMA: dict[str, pl.DataType] = {
    "schema_version": pl.String,
    "package_id": pl.String,
    "repo_id": pl.String,
    "repo_group_id": pl.String,
    "cutoff_at": pl.String,
    "horizon_months": pl.Int64,
    "horizon_end_at": pl.String,
    "package_age_days": pl.Int64,
    "releases_total": pl.Int64,
    "days_since_latest_release": pl.Int64,
    "release_count_3m": pl.Int64,
    "release_count_6m": pl.Int64,
    "release_count_12m": pl.Int64,
    "commit_count_3m": pl.Int64,
    "commit_count_6m": pl.Int64,
    "commit_count_12m": pl.Int64,
    "contributor_count_12m": pl.Int64,
    "issue_opened_count_12m": pl.Int64,
    "issue_closed_count_12m": pl.Int64,
    "pr_opened_count_12m": pl.Int64,
    "pr_merged_count_12m": pl.Int64,
    "coverage_json": pl.String,
    "missing_fields_json": pl.String,
    "raw_record_ids_json": pl.String,
}

CORE_DATA_GROUPS = ("releases", "commits", "contributors", "issues", "pull_requests")
RAW_ID_REGEX = re.compile(r"^raw:[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$")


def _safe_raw_id(record: object) -> str | None:
    prov = getattr(record, "provenance", None)
    raw_ids = getattr(prov, "raw_record_ids", None)
    if raw_ids and isinstance(raw_ids[0], str) and RAW_ID_REGEX.match(raw_ids[0]):
        return raw_ids[0]
    return None


def _make_error(
    message: str,
    code: str = "validation_error",
    raw_record_id: str | None = None,
    source_record_id: str | None = None,
) -> DataError:
    return DataError(
        code=code,
        message=message,
        retryable=False,
        source_record_id=source_record_id,
        raw_record_id=raw_record_id,
    )


class FeatureBuilder:
    """Canonical feature builder orchestrating domain calculators and contract pairs."""

    BUILDER_VERSION: str = "0.1.0"

    def build_snapshot_features(
        self,
        repos_df: pl.DataFrame,
        activity_df: pl.DataFrame | None = None,
    ) -> pl.DataFrame:
        """Combine repository metadata and activity aggregations."""
        if repos_df.is_empty():
            return pl.DataFrame()
        return repos_df

    def build_snapshot(self, request: SnapshotBuildRequest) -> SnapshotBuildResult:
        """Validate inputs, run domain feature calculators, and return verified SnapshotBuildResult."""
        errors: list[DataError] = []

        # 1. Coverage validation
        for cov in request.coverage:
            if cov.time_range is not None and cov.time_range.end_at > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Coverage group '{cov.data_group}' extends past cutoff ({cov.time_range.end_at} > {request.cutoff_at})"
                    )
                )

        # 2. Release records validation
        for rel in request.releases:
            raw_id = _safe_raw_id(rel)
            if rel.package_id != request.package_id:
                errors.append(
                    _make_error(
                        f"Release package_id mismatch: expected {request.package_id}, got {rel.package_id}",
                        raw_record_id=raw_id,
                    )
                )
            if rel.repo_id is not None and rel.repo_id != request.repository.repo_id:
                errors.append(
                    _make_error(
                        f"Release repo_id mismatch: expected {request.repository.repo_id}, got {rel.repo_id}",
                        raw_record_id=raw_id,
                    )
                )
            rel_time = getattr(rel, "uploaded_at", None) or getattr(rel, "published_at", None)
            if rel_time is None:
                errors.append(
                    _make_error(
                        f"Release {rel.version} missing uploaded_at/published_at timestamp",
                        raw_record_id=raw_id,
                    )
                )
            elif rel_time > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Release {rel.version} timestamp {rel_time} > cutoff {request.cutoff_at}",
                        raw_record_id=raw_id,
                    )
                )
            if rel.provenance.valid_at > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Release {rel.version} valid_at {rel.provenance.valid_at} > cutoff {request.cutoff_at}",
                        raw_record_id=raw_id,
                    )
                )
            if rel.provenance.available_at is None:
                errors.append(
                    _make_error(
                        f"Release {rel.version} available_at is None",
                        raw_record_id=raw_id,
                    )
                )
            elif rel.provenance.available_at > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Release {rel.version} available_at {rel.provenance.available_at} > cutoff {request.cutoff_at}",
                        raw_record_id=raw_id,
                    )
                )
            if getattr(rel, "yanked_status_at", None) is not None and rel.yanked_status_at > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Release {rel.version} yanked_status_at {rel.yanked_status_at} > cutoff {request.cutoff_at}",
                        raw_record_id=raw_id,
                    )
                )

        # 3. Activity records validation
        for act in request.activities:
            raw_id = _safe_raw_id(act)
            if act.repo_id != request.repository.repo_id:
                errors.append(
                    _make_error(
                        f"Activity repo_id mismatch: expected {request.repository.repo_id}, got {act.repo_id}",
                        raw_record_id=raw_id,
                    )
                )
            if act.event_at > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Activity {act.activity_id} event_at {act.event_at} > cutoff {request.cutoff_at}",
                        raw_record_id=raw_id,
                    )
                )
            if act.provenance.valid_at > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Activity {act.activity_id} valid_at {act.provenance.valid_at} > cutoff {request.cutoff_at}",
                        raw_record_id=raw_id,
                    )
                )
            if act.provenance.available_at is None:
                errors.append(
                    _make_error(
                        f"Activity {act.activity_id} available_at is None",
                        raw_record_id=raw_id,
                    )
                )
            elif act.provenance.available_at > request.cutoff_at:
                errors.append(
                    _make_error(
                        f"Activity {act.activity_id} available_at {act.provenance.available_at} > cutoff {request.cutoff_at}",
                        raw_record_id=raw_id,
                    )
                )

        if errors:
            failed_result = SnapshotBuildResult(
                schema_version=request.schema_version,
                record_type="snapshot_build_result",
                request_id=request.request_id,
                run_id=request.run_id,
                builder_version=self.BUILDER_VERSION,
                status="failed",
                snapshot=None,
                errors=errors,
            )
            return failed_result

        # Run domain feature calculators
        try:
            pkg_res = PackageFeatures.compute(
                package_id=request.package_id,
                cutoff_at=request.cutoff_at,
                releases=request.releases,
                coverage_map=request.coverage,
            )
            act_res = ActivityFeatures.compute(
                repo_id=request.repository.repo_id,
                cutoff_at=request.cutoff_at,
                activities=request.activities,
                coverage_map=request.coverage,
            )
        except ValueError as exc:
            failed_result = SnapshotBuildResult(
                schema_version=request.schema_version,
                record_type="snapshot_build_result",
                request_id=request.request_id,
                run_id=request.run_id,
                builder_version=self.BUILDER_VERSION,
                status="failed",
                snapshot=None,
                errors=[_make_error(str(exc))],
            )
            return failed_result

        # Collect unique raw_record_ids preserving deterministic order
        seen_raw: set[str] = set()
        raw_record_ids: list[str] = []
        for record in [*request.releases, *request.activities]:
            for rid in record.provenance.raw_record_ids:
                if rid not in seen_raw:
                    seen_raw.add(rid)
                    raw_record_ids.append(rid)

        # Assemble features and missing fields
        all_features = {**pkg_res.features, **act_res.features}
        snapshot_features = SnapshotFeatures(**all_features)
        missing_fields = [*pkg_res.missing_fields, *act_res.missing_fields]

        horizon_end_at = add_calendar_months(request.cutoff_at, 12)
        snapshot = PackageSnapshot(
            schema_version="0.1.0",
            record_type="package_snapshot",
            package_id=request.package_id,
            repo_id=request.repository.repo_id,
            repo_group_id=request.repository.repo_id,
            cutoff_at=request.cutoff_at,
            horizon_months=12,
            horizon_end_at=horizon_end_at,
            features=snapshot_features,
            coverage=request.coverage,
            missing_fields=missing_fields,
            raw_record_ids=raw_record_ids,
        )

        # Determine status
        cov_status = {c.data_group: c.status for c in request.coverage}
        all_core_complete = all(cov_status.get(g) == "complete" for g in CORE_DATA_GROUPS)

        status: Literal["complete", "partial"] = (
            "complete" if (all_core_complete and not missing_fields) else "partial"
        )

        result = SnapshotBuildResult(
            schema_version=request.schema_version,
            record_type="snapshot_build_result",
            request_id=request.request_id,
            run_id=request.run_id,
            builder_version=self.BUILDER_VERSION,
            status=status,
            snapshot=snapshot,
            errors=[],
        )

        validate_snapshot_pair(request, result)
        return result

    def snapshots_to_polars(self, snapshots: Sequence[PackageSnapshot]) -> pl.DataFrame:
        """Flatten PackageSnapshot objects into tabular Polars DataFrame with provenance JSON columns."""
        if not snapshots:
            return pl.DataFrame(schema=POLARS_SNAPSHOT_SCHEMA)

        rows: list[dict] = []
        for s in snapshots:
            feat_dict = s.features.model_dump()
            row = {
                "schema_version": s.schema_version,
                "package_id": s.package_id,
                "repo_id": s.repo_id,
                "repo_group_id": s.repo_group_id,
                "cutoff_at": s.cutoff_at,
                "horizon_months": s.horizon_months,
                "horizon_end_at": s.horizon_end_at,
                **{k: feat_dict.get(k) for k in FEATURE_COLUMNS},
                "coverage_json": json.dumps([c.model_dump(mode="json") for c in s.coverage]),
                "missing_fields_json": json.dumps([m.model_dump(mode="json") for m in s.missing_fields]),
                "raw_record_ids_json": json.dumps(list(s.raw_record_ids)),
            }
            rows.append(row)

        return pl.DataFrame(rows, schema=POLARS_SNAPSHOT_SCHEMA)

    def polars_to_snapshots(self, df: pl.DataFrame) -> list[PackageSnapshot]:
        """Reconstruct validated PackageSnapshot objects losslessly from Polars DataFrame."""
        if df.is_empty():
            return []

        snapshots: list[PackageSnapshot] = []
        for row in df.iter_rows(named=True):
            features = SnapshotFeatures(**{k: row[k] for k in FEATURE_COLUMNS})
            coverage = [Coverage.model_validate(c) for c in json.loads(row["coverage_json"])]
            missing_fields = [
                MissingField.model_validate(m) for m in json.loads(row["missing_fields_json"])
            ]
            raw_record_ids = json.loads(row["raw_record_ids_json"])

            snapshot = PackageSnapshot(
                schema_version=row["schema_version"],
                record_type="package_snapshot",
                package_id=row["package_id"],
                repo_id=row["repo_id"],
                repo_group_id=row["repo_group_id"],
                cutoff_at=row["cutoff_at"],
                horizon_months=row["horizon_months"],
                horizon_end_at=row["horizon_end_at"],
                features=features,
                coverage=coverage,
                missing_fields=missing_fields,
                raw_record_ids=raw_record_ids,
            )
            snapshots.append(snapshot)

        return snapshots
