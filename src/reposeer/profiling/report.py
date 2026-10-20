"""T-020 root-snapshot profiles, shared by orchestration and standalone CLI."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from reposeer.collection.persistence import write_json_atomic
from reposeer.profiling.completeness import CompletenessProfiler
from reposeer.profiling.dataset import DatasetProfiler
from reposeer.storage.manifest import ProcessingManifest
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.dataset_writer import DATASET_SCHEMAS
from reposeer.storage.query.duckdb import DuckDBQueryEngine

LIMITATIONS = (
    "source_coverage=not_verified",
    "historical_validity=unverified",
    "Single writer; atomicity per file, not a multi-dataset transaction.",
    "Output fingerprints describe current physical bytes, not immutable historical snapshots.",
    "Missing datasets and undefined rates are null, not verified empty upstream data.",
)


class ProfilingReportGenerator:
    def __init__(self, duckdb_engine: DuckDBQueryEngine | None = None, *,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.duckdb = duckdb_engine
        self.clock = clock or (lambda: datetime.now(UTC))
        self.dataset_profiler = DatasetProfiler()
        self.completeness_profiler = CompletenessProfiler()

    def generate_report(self, parquet_files: dict[str, Path]) -> dict[str, Any]:
        """Keep legacy present-table stats/missing_rate results; add richer metrics."""
        report: dict[str, Any] = {"datasets": {}}
        now = self.clock()
        for name, path in parquet_files.items():
            if not path.exists():
                continue
            frame = pl.read_parquet(path)
            stats = self.dataset_profiler.profile_dataframe(frame, name=name, now=now)
            report["datasets"][name] = dict(
                present=True, path=str(path), **stats, stats=stats, missing_rate=stats["null_rates"])
        return report

    def profile_storage(self, paths: StoragePaths, *, integration_run_id: str | None = None,
                        processing: ProcessingManifest | None = None,
                        external_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
        report = self.generate_report({
            name: paths.processed_dir / name / f"{name}.parquet" for name in DATASET_SCHEMAS})
        for name in DATASET_SCHEMAS:
            if name not in report["datasets"]:
                path = paths.processed_dir / name / f"{name}.parquet"
                report["datasets"][name] = dict(
                    present=False, path=str(path), row_count=None, column_count=None,
                    columns=None, schema=None, null_counts=None, null_rates=None,
                    stats=None, missing_rate=None)
            report["datasets"][name]["relative_path"] = f"processed/{name}/{name}.parquet"
        summary = processing.processing_summary if processing else None
        metrics = summary.model_dump(mode="json") if summary else {}
        count = metrics.get("input_record_count")
        metrics["duplicate_input_rate"] = metrics["duplicate_input_count"] / count if count else None
        metrics["dataset_writes"] = (
            {name: info.model_dump(mode="json") for name, info in processing.datasets.items()}
            if processing else {})
        audit = report["datasets"]["processing_audit"]
        metrics["content_duplicate_groups"] = None
        if processing and audit["present"]:
            frame = pl.read_parquet(Path(audit["path"]))
            metrics["content_duplicate_groups"] = frame.filter(
                (pl.col("processing_run_id") == processing.processing_run_id)
                & (pl.col("item_type") == "content_duplicate")).height
        relevance = report["datasets"]["external_relevance"]
        decisions = None
        if relevance["present"]:
            frame = pl.read_parquet(Path(relevance["path"]))
            decisions = {decision: frame.filter(pl.col("decision") == decision).height
                         for decision in ("relevant", "irrelevant", "unresolved")}
        denominator = sum(decisions.values()) if decisions else 0
        external = dict(external_metrics or {})
        external.update(
            collection_metrics_scope="integration_child_runs" if external_metrics else "not_supplied",
            relevance_scope="storage_root_snapshot_after_run",
            relevance_status="assessed" if denominator else "not_assessed",
            relevance_relevant=decisions["relevant"] if decisions else None,
            relevance_irrelevant=decisions["irrelevant"] if decisions else None,
            relevance_unresolved=decisions["unresolved"] if decisions else None,
            assessments=denominator,
            run_relevance_status=("assessed" if summary and sum(summary.relevance_decisions.values())
                                  else "not_assessed"),
            relevance_rate=decisions["relevant"] / denominator if denominator else None)
        return dict(
            report_version="1", integration_run_id=integration_run_id,
            processing_run_id=processing.processing_run_id if processing else None,
            scope="storage_root_snapshot_after_run", source_coverage="not_verified",
            historical_validity="unverified", **report, processing_metrics=metrics,
            external_metrics=external, limitations=list(LIMITATIONS))

    def save_report(self, report: dict[str, Any], output_path: Path) -> None:
        write_json_atomic(output_path, report)
