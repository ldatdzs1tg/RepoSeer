"""Typed processing manifests; collection checkpoints remain owned by RunManager."""

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.models import RawRecordId, Sha256, UtcTimestamp, UuidText
from reposeer.pipelines.normalization import ProcessingSummary
from reposeer.storage.paths import StoragePaths


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DatasetWrite(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    path: str
    inserted_row_count: int = Field(ge=0)
    total_row_count: int | None = Field(ge=0)
    schema_version: Literal["1"] = "1"
    status: Literal["pending", "written", "unchanged", "failed"]
    content_sha256: Sha256 | None = None
    relative_path: str | None = None

    @field_validator("relative_path")
    @classmethod
    def portable_path(cls, value: str | None) -> str | None:
        if value is not None:
            path = PurePosixPath(value)
            if (not value.strip() or not path.parts or "\\" in value
                    or path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts):
                raise ValueError("dataset_path_must_be_relative_posix")
        return value


class ProcessingManifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    manifest_version: Literal["1"] = "1"
    processing_run_id: UuidText
    processing_version: Literal["0.1.0-t018"] = "0.1.0-t018"
    canonical_schema_version: Literal["0.1.0"] = "0.1.0"
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    processing_status: Literal["complete", "partial", "failed"]
    source_coverage: Literal["not_verified"] = "not_verified"
    cutoff_at: UtcTimestamp | None
    input_raw_paths: list[str]
    input_fingerprints: dict[str, Sha256]
    raw_observation_fingerprints: dict[RawRecordId, Sha256] = Field(default_factory=dict)
    collection_run_ids: list[UuidText]
    datasets: dict[str, DatasetWrite]
    processing_summary: ProcessingSummary | None
    errors: list[dict[str, str | None]]
    missing_reasons: list[str]

    @model_validator(mode="after")
    def completed(self):
        if self.finished_at < self.started_at:
            raise ValueError("processing_finish_before_start")
        if self.processing_status == "complete" and (
            self.errors or self.processing_summary is None
            or self.processing_summary.rejected_observation_count
            or any(item.status in {"failed", "pending"} or item.total_row_count is None
                   for item in self.datasets.values())
        ):
            raise ValueError("complete_processing_requires_successful_persistence")
        return self


def write_manifest(paths: StoragePaths, manifest: ProcessingManifest) -> Path:
    """Atomically publish one processing run, without sharing collection checkpoints."""
    manifest = ProcessingManifest.model_validate(manifest.model_dump())
    target = paths.processing_manifest_path(manifest.processing_run_id)
    if target.exists():
        raise FileExistsError("processing_manifest_already_exists")
    write_json_atomic(target, manifest.model_dump(mode="json"))
    return target
