"""Feature pipeline orchestrating FeatureService and MaintenancePipeline."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import polars as pl

from reposeer.contracts.analysis import OfficialStatus
from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    Coverage,
    PackageId,
    PackageRelease,
    PackageSnapshot,
    Provenance,
    RepoId,
    RepositoryActivity,
    SnapshotBuildRequest,
    SnapshotBuildResult,
    UtcTimestamp,
)
from reposeer.maintenance.labels import HistoricalLabelRecord
from reposeer.pipelines.maintenance import MaintenancePipeline
from reposeer.services.feature_service import FeatureService
from reposeer.storage.features.parquet import FeatureStorage
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.parquet import ParquetStorage


@dataclass
class FeaturesPipelineSummary:
    """Summary of features pipeline run."""

    results: list[SnapshotBuildResult]
    snapshots_df: pl.DataFrame
    labels_df: pl.DataFrame | None = None


def _load_requests_from_file(path: Path) -> list[SnapshotBuildRequest]:
    """Parse SnapshotBuildRequest from single JSON, JSON array, or JSONL file."""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    if content.startswith("{"):
        return [SnapshotBuildRequest.model_validate_json(content)]
    elif content.startswith("["):
        data = json.loads(content)
        return [SnapshotBuildRequest.model_validate(item) for item in data]
    else:
        requests: list[SnapshotBuildRequest] = []
        for line in content.splitlines():
            line = line.strip()
            if line:
                requests.append(SnapshotBuildRequest.model_validate_json(line))
        return requests


class FeaturesPipeline:
    """Canonical features pipeline orchestrating extraction, validation, and ground-truth labeling."""

    def __init__(
        self,
        paths: StoragePaths | None = None,
        feature_service: FeatureService | None = None,
        maintenance_pipeline: MaintenancePipeline | None = None,
        storage: FeatureStorage | None = None,
    ) -> None:
        self.paths = paths or StoragePaths()
        self.feature_service = feature_service or FeatureService(paths=self.paths)
        self.maintenance_pipeline = maintenance_pipeline or MaintenancePipeline()
        self.storage = storage or FeatureStorage()
        self.parquet_storage = ParquetStorage()

    def _persist_labels(self, records: Sequence[HistoricalLabelRecord]) -> None:
        """Persist label records deduplicated on (package_id, cutoff_at)."""
        if not records:
            return
        dest = self.paths.features_path("package_labels")
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.is_file():
            existing_df = self.storage.load_features(dest)
            existing_recs = (
                self.maintenance_pipeline.polars_to_labels(existing_df)
                if not existing_df.is_empty()
                else []
            )
            rec_map = {(r.package_id, r.cutoff_at): r for r in existing_recs}
        else:
            rec_map = {}

        for r in records:
            rec_map[(r.package_id, r.cutoff_at)] = r

        all_records = list(rec_map.values())
        out_df = self.maintenance_pipeline.labels_to_polars(all_records)
        self.storage.save_features(dest, out_df)

    def _load_horizon_activities(
        self,
        repo_id: RepoId,
        cutoff_at: UtcTimestamp,
        horizon_end_at: UtcTimestamp,
        observation_end_at: UtcTimestamp,
    ) -> list[RepositoryActivity]:
        """Load activities taking place during the 12m horizon window."""
        path = self.paths.processed_path("github_activity")
        if not path.is_file():
            return []
        df = self.parquet_storage.read(path)
        if df.is_empty():
            return []

        matching = df.filter(
            (pl.col("repo_id") == repo_id)
            & (pl.col("event_at") > cutoff_at)
            & (pl.col("event_at") <= horizon_end_at)
            & pl.col("available_at").is_not_null()
            & (pl.col("available_at") <= observation_end_at)
        )
        if matching.is_empty():
            return []

        acts: list[RepositoryActivity] = []
        for row in matching.iter_rows(named=True):
            event_at = row["event_at"]
            valid_at = row["valid_at"]
            avail_at = row["available_at"]
            ret_at = max(row.get("retrieved_at") or observation_end_at, event_at, valid_at, avail_at)
            prov = Provenance(
                source=row.get("source") or "github_rest",
                source_url=row.get("source_url") or "https://api.github.com",
                retrieved_at=ret_at,
                valid_at=valid_at,
                available_at=avail_at,
                temporal_basis=row.get("temporal_basis") or "historical_reconstruction",
                raw_record_ids=[row.get("raw_record_id") or "raw:00000000-0000-4000-8000-000000000001"],
                missing_fields=[],
            )
            actor_id_val = row.get("actor_id")
            actor_id_str = str(actor_id_val) if actor_id_val is not None and str(actor_id_val).strip() else None
            actor_bot_val = row.get("actor_is_bot")
            actor_bot_bool = bool(actor_bot_val) if actor_bot_val is not None else None

            acts.append(
                RepositoryActivity(
                    schema_version="0.1.0",
                    record_type="repository_activity",
                    activity_id=row["activity_id"],
                    repo_id=repo_id,
                    activity_kind=row["activity_kind"],
                    source_object_id=str(row["source_object_id"]),
                    event_at=event_at,
                    actor_id=actor_id_str,
                    actor_is_bot=actor_bot_bool,
                    provenance=prov,
                )
            )
        return acts

    def _load_horizon_releases(
        self,
        package_id: PackageId,
        repo_id: RepoId,
        cutoff_at: UtcTimestamp,
        horizon_end_at: UtcTimestamp,
        observation_end_at: UtcTimestamp,
    ) -> list[PackageRelease]:
        """Load releases taking place during the 12m horizon window."""
        path = self.paths.processed_path("package_releases")
        if not path.is_file():
            path = self.paths.processed_path("releases")
        if not path.is_file():
            return []
        df = self.parquet_storage.read(path)
        if df.is_empty():
            return []

        matching = df.filter(
            (pl.col("package_id") == package_id)
            & (pl.col("uploaded_at") > cutoff_at)
            & (pl.col("uploaded_at") <= horizon_end_at)
            & pl.col("available_at").is_not_null()
            & (pl.col("available_at") <= observation_end_at)
        )
        if matching.is_empty():
            return []

        releases: list[PackageRelease] = []
        for row in matching.iter_rows(named=True):
            up_at = row["uploaded_at"]
            v_at = row["valid_at"]
            av_at = row["available_at"]
            ret_at = max(row.get("retrieved_at") or observation_end_at, up_at, v_at, av_at)
            prov = Provenance(
                source=row.get("source") or "pypi_json",
                source_url=row.get("source_url") or "https://pypi.org",
                retrieved_at=ret_at,
                valid_at=v_at,
                available_at=av_at,
                temporal_basis=row.get("temporal_basis") or "historical_reconstruction",
                raw_record_ids=[row.get("raw_record_id") or "raw:00000000-0000-4000-8000-000000000001"],
                missing_fields=[],
            )
            releases.append(
                PackageRelease(
                    schema_version="0.1.0",
                    record_type="package_release",
                    package_id=package_id,
                    repo_id=repo_id,
                    version=str(row["version"]),
                    uploaded_at=up_at,
                    requires_python=row.get("requires_python") or ">=3.10",
                    dependencies=None,
                    is_yanked=None,
                    yanked_reason=None,
                    yanked_status_at=None,
                    provenance=prov,
                )
            )
        return releases

    def run(
        self,
        *,
        request_file: Path | str | None = None,
        targets: Sequence[tuple[PackageId, RepoId, UtcTimestamp]] | None = None,
        cutoffs: Sequence[UtcTimestamp] | None = None,
        coverage_overrides: Mapping[str, list[Coverage]] | None = None,
        evaluate_labels: bool = False,
        official_events: Sequence[OfficialStatus] | None = None,
        observation_end_at: UtcTimestamp | None = None,
        horizon_coverage_complete: bool = True,
        persist: bool = True,
        output_path: Path | None = None,
    ) -> FeaturesPipelineSummary:
        """Run the feature and label pipelines."""
        results: list[SnapshotBuildResult] = []

        if request_file is not None:
            requests = _load_requests_from_file(Path(request_file))
            for req in requests:
                res = self.feature_service.build_from_request(req, persist=False)
                results.append(res)
        else:
            results = self.feature_service.build_from_processed(
                targets=targets,
                cutoffs=cutoffs,
                coverage_overrides=coverage_overrides,
                persist=False,
            )

        valid_snapshots: list[PackageSnapshot] = [
            r.snapshot for r in results if r.snapshot is not None
        ]
        snapshots_df = self.feature_service.feature_builder.snapshots_to_polars(valid_snapshots)

        if persist and not snapshots_df.is_empty():
            if output_path is not None:
                out = Path(output_path)
                out.parent.mkdir(parents=True, exist_ok=True)
                self.storage.save_features(out, snapshots_df)
            else:
                self.feature_service._persist_snapshots(valid_snapshots)

        labels_df: pl.DataFrame | None = None
        if evaluate_labels or official_events is not None:
            label_records: list[HistoricalLabelRecord] = []
            for s in valid_snapshots:
                horizon_end = s.horizon_end_at
                obs_end = observation_end_at or horizon_end
                horizon_acts = self._load_horizon_activities(
                    s.repo_id, s.cutoff_at, horizon_end, obs_end
                )
                horizon_rels = self._load_horizon_releases(
                    s.package_id, s.repo_id, s.cutoff_at, horizon_end, obs_end
                )
                rec = self.maintenance_pipeline.evaluate_horizon_label(
                    package_id=s.package_id,
                    repo_id=s.repo_id,
                    cutoff_at=s.cutoff_at,
                    observation_end_at=obs_end,
                    official_events=official_events or (),
                    horizon_releases=horizon_rels,
                    horizon_activities=horizon_acts,
                    horizon_coverage_complete=horizon_coverage_complete,
                )
                label_records.append(rec)

            labels_df = self.maintenance_pipeline.labels_to_polars(label_records)
            if persist and label_records:
                self._persist_labels(label_records)

        return FeaturesPipelineSummary(
            results=results,
            snapshots_df=snapshots_df,
            labels_df=labels_df,
        )
