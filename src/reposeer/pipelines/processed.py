"""Offline raw replay into typed, observation-preserving processed datasets."""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from reposeer.collectors.github.repository import repository_exclusion_reasons
from reposeer.contracts import RawRecord, validate_record
from reposeer.contracts.identity import checked_timestamp, format_utc_timestamp
from reposeer.contracts.models import UuidText
from reposeer.exceptions import StorageError
from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.storage.manifest import DatasetWrite, ProcessingManifest, write_manifest
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.dataset_writer import ProcessedDatasetWriter, stable_json
from reposeer.storage.raw.jsonl import JsonlStorage


class ProcessingWriteResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    processing_run_id: UuidText
    status: Literal["complete", "partial", "failed"]
    manifest_path: Path
    input_record_count: int = Field(ge=0)
    dataset_row_counts: dict[str, int]
    error_count: int = Field(ge=0)
    datasets: dict[str, DatasetWrite]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProcessedDatasetPipeline:
    def __init__(self, storage_paths: StoragePaths):
        self.paths = storage_paths
        self.writer = ProcessedDatasetWriter(storage_paths)

    def run(self, raw_paths: Sequence[Path], *, targets: Sequence[VerifiedTargetContext] = (),
            cutoff_at: str | None = None) -> ProcessingWriteResult:
        run_id = str(uuid4())
        manifest_path = self.paths.processing_manifest_path(run_id)
        if manifest_path.exists():
            raise FileExistsError("processing_manifest_already_exists")
        started = format_utc_timestamp(datetime.now(UTC))
        inputs = [Path(path).resolve() for path in raw_paths]
        fingerprints, raw_fingerprints, collection_runs = {}, {}, set()
        result = None
        errors = []
        missing = ["source_coverage_not_verified"]
        self.writer.dataset_results = {}

        def manifest(status: str) -> ProcessingManifest:
            return ProcessingManifest(
                processing_run_id=run_id, started_at=started,
                finished_at=format_utc_timestamp(datetime.now(UTC)),
                processing_status=status, cutoff_at=cutoff_at,
                input_raw_paths=[str(path) for path in inputs], input_fingerprints=fingerprints,
                raw_observation_fingerprints=raw_fingerprints,
                collection_run_ids=sorted(collection_runs), datasets=self.writer.dataset_results,
                processing_summary=result.processing_summary if result is not None else None,
                errors=errors, missing_reasons=missing)

        stage = "input_validation"
        try:
            if cutoff_at is not None:
                checked_timestamp(cutoff_at)
            if not inputs:
                missing.append("no_raw_inputs")
                raise ValueError("no_raw_inputs")
            for path in inputs:
                if not path.is_file():
                    raise FileNotFoundError(path)
                fingerprints[str(path)] = _file_sha256(path)
                for value in JsonlStorage().iter_records(path):
                    try:
                        raw = validate_record(value)
                    except (ValueError, TypeError):
                        continue  # T-018 retains a rejection, not an empty success.
                    if isinstance(raw, RawRecord):
                        digest = hashlib.sha256(stable_json(raw.model_dump(mode="json")).encode()).hexdigest()
                        previous = raw_fingerprints.get(raw.raw_record_id)
                        if previous is not None and previous != digest:
                            raise ValueError("conflicting_raw_record_id")
                        raw_fingerprints[raw.raw_record_id] = digest
                        collection_runs.add(raw.run_id)
            # A UUID identifies an immutable observation even when changed native fields
            # would project to the same flat row. Include failed/partial processing attempts.
            directory = self.paths.metadata_dir / "collection_runs"
            for path in sorted(directory.glob("processing_*.json")):
                previous = ProcessingManifest.model_validate_json(path.read_bytes())
                if any(raw_id in raw_fingerprints and raw_fingerprints[raw_id] != digest
                       for raw_id, digest in previous.raw_observation_fingerprints.items()):
                    raise ValueError("conflicting_persisted_raw_record_id")
            stage = "normalization"
            result = NormalizationPipeline().process_jsonl(inputs, targets=targets, cutoff_at=cutoff_at)
            errors.extend(dict(code=error.code, raw_record_id=error.raw_record_id)
                          for error in result.processing_errors)
            if any(_file_sha256(path) != fingerprints[str(path)] for path in inputs):
                raise ValueError("raw_input_changed_during_processing")
            stage = "persistence"
            counts = self.writer.write(result, processing_run_id=run_id)
            stage = "processing"
            valid_count = (len(result.canonical_activities) + len(result.internal_activity_entities)
                           + len(result.canonical_articles)
                           + sum(not repository_exclusion_reasons(record)
                                 for record in result.canonical_repositories))
            if not result.processing_summary.accepted_observation_count or not valid_count:
                missing.append("no_eligible_normalized_records")
                raise ValueError("no_eligible_normalized_records")
            status = "partial" if errors or result.processing_summary.rejected_observation_count else "complete"
            stage = "manifest"
            write_manifest(self.paths, manifest(status))
            return ProcessingWriteResult(
                processing_run_id=run_id, status=status, manifest_path=manifest_path,
                input_record_count=result.processing_summary.input_record_count,
                dataset_row_counts=counts, error_count=len(errors), datasets=self.writer.dataset_results)
        except (OSError, ValueError, TypeError, StorageError, pl.exceptions.PolarsError) as error:
            errors.append(dict(code=stage + "_failure", error_type=type(error).__name__))
            # Invalid cutoff strings must not prevent the failed-run manifest itself.
            if cutoff_at is not None:
                try:
                    checked_timestamp(cutoff_at)
                except ValueError:
                    cutoff_at = None
                    missing.append("invalid_cutoff_at")
            try:
                write_manifest(self.paths, manifest("failed"))
            except (OSError, ValueError, TypeError) as manifest_error:
                error.add_note("failed_manifest_write: " + type(manifest_error).__name__)
            raise
