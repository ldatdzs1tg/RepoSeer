"""T-020A pre-collection frame, deterministic SQLite sampling and atomic registry export."""

import csv
import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from reposeer.clients.pypi_index import (
    DEFAULT_INDEX_BYTES,
    PackageDiscoveryError,
    PyPIIndexClient,
    copy_snapshot,
    file_fingerprint,
    load_snapshot,
    parse_index,
    validate_byte_budget,
)
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import format_utc_timestamp, normalize_pypi_name, package_id
from reposeer.storage.paths import StoragePaths

ALGORITHM = "sha256_hash_priority_v1"
CSV_FIELDS = (
    "sampling_run_id",
    "sample_rank",
    "package_id",
    "package_name",
    "batch_id",
    "source_kind",
    "eligibility_status",
    "mapping_status",
    "collection_status",
)
LIMITATIONS = (
    "Package presence is not evidence of maintenance status.",
    "GitHub mappings have not been verified.",
    "Sampling describes the observed input frame, not all active PyPI packages.",
    "Current snapshots do not establish historical package presence or availability.",
    "Run T-020 separately for each batch; use one writer per storage root.",
)


@dataclass(frozen=True)
class PackageDiscoveryOptions:
    source: Literal["pypi-simple", "snapshot", "file"]
    count: int
    data_dir: Path
    seed: int = 42
    batch_size: int = 50
    snapshot: Path | None = None
    input_file: Path | None = None
    exclude_file: Path | None = None
    max_index_bytes: int = DEFAULT_INDEX_BYTES

    def validate(self) -> None:
        for value, low, high, name in (
            (self.count, 1, 10000, "count"),
            (self.batch_size, 1, 100, "batch_size"),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"invalid_{name}")
        if type(self.seed) is not int:
            raise ValueError("invalid_seed")
        validate_byte_budget(self.max_index_bytes)
        if self.source not in {"pypi-simple", "snapshot", "file"}:
            raise ValueError("invalid_source")
        if (
            (
                self.source == "pypi-simple"
                and (self.snapshot is not None or self.input_file is not None)
            )
            or (
                self.source == "snapshot" and (self.snapshot is None or self.input_file is not None)
            )
            or (self.source == "file" and (self.input_file is None or self.snapshot is not None))
        ):
            raise ValueError("invalid_source_parameters")


@dataclass(frozen=True)
class PackageDiscoveryResult:
    sampling_run_id: str
    status: Literal["complete"]
    selected_count: int
    batch_count: int
    manifest_path: Path
    candidates_path: Path
    targets_path: Path
    batches_dir: Path
    snapshot_path: Path | None


def _load_names(path: Path, on_name: Callable[[str], None], *, kind: str) -> tuple[str, int]:
    """Read and hash original bytes; reject bad rows without echoing their contents."""
    digest, valid = hashlib.sha256(), 0
    with path.open("rb") as stream:
        line_number = 0
        while raw := stream.readline(1048577):
            line_number += 1
            if len(raw) > 1048576:
                raise PackageDiscoveryError(f"invalid_{kind}_row:{line_number}")
            digest.update(raw)
            try:
                value = raw.decode("utf-8-sig" if line_number == 1 else "utf-8").strip()
                if not value or value.startswith("#"):
                    continue
                name = normalize_pypi_name(value[5:] if value.startswith("pypi:") else value)
            except (UnicodeError, ValueError) as error:
                raise PackageDiscoveryError(f"invalid_{kind}_row:{line_number}") from error
            on_name(name)
            valid += 1
    return digest.hexdigest(), valid


def _flush(stream) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _write_targets(path: Path, names: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        for name in names:
            stream.write(package_id(name) + "\n")
        _flush(stream)


class PackageDiscoveryPipeline:
    def __init__(
        self,
        *,
        index_client: PyPIIndexClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.index_client = index_client
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(self, options: PackageDiscoveryOptions) -> PackageDiscoveryResult:
        options.validate()
        paths = StoragePaths(options.data_dir)
        registry = paths.base_dir / "registry"
        created_at = format_utc_timestamp(self.clock())
        run_id = str(uuid4())
        snapshot = None
        # Validate explicit exclusions before acquiring any network source.
        with tempfile.TemporaryDirectory(prefix="reposeer-sampling-") as scratch:
            with closing(sqlite3.connect(Path(scratch) / "frame.sqlite")) as db:
                db.execute("PRAGMA cache_size=-2048")
                db.execute("PRAGMA temp_store=FILE")
                db.execute(
                    "CREATE TABLE candidates (canonical_name TEXT PRIMARY KEY, priority BLOB NOT NULL)"
                )
                db.execute("CREATE TABLE exclusions (canonical_name TEXT PRIMARY KEY)")
                exclusion_rows = []

                def exclude(name):
                    exclusion_rows.append((name,))
                    if len(exclusion_rows) >= 1000:
                        db.executemany(
                            "INSERT OR IGNORE INTO exclusions VALUES (?)", exclusion_rows
                        )
                        exclusion_rows.clear()

                exclusion_hash = None
                if options.exclude_file is not None:
                    exclusion_hash, _ = _load_names(options.exclude_file, exclude, kind="exclusion")
                db.executemany("INSERT OR IGNORE INTO exclusions VALUES (?)", exclusion_rows)
                exclusion_rows.clear()
                excluded_name_count = db.execute("SELECT COUNT(*) FROM exclusions").fetchone()[0]
                source_kind = {
                    "pypi-simple": "pypi_simple_html",
                    "snapshot": "verified_snapshot",
                    "file": "user_supplied_file",
                }[options.source]
                rows = []

                def candidate(name):
                    priority = hashlib.sha256(
                        f"reposeer:pypi-sample:v1:{options.seed}:{name}".encode()
                    ).digest()
                    rows.append((name, priority))
                    if len(rows) >= 1000:
                        db.executemany("INSERT OR IGNORE INTO candidates VALUES (?, ?)", rows)
                        rows.clear()

                if options.source != "file":
                    snapshots = registry / "snapshots"
                    if options.source == "snapshot":
                        snapshot = copy_snapshot(
                            load_snapshot(options.snapshot, max_bytes=options.max_index_bytes),
                            snapshots,
                        )
                    elif self.index_client is not None:
                        snapshot = self.index_client.fetch_snapshot(
                            snapshots, max_decoded_bytes=options.max_index_bytes
                        )
                    else:
                        with PyPIIndexClient(clock=self.clock) as client:
                            snapshot = client.fetch_snapshot(
                                snapshots, max_decoded_bytes=options.max_index_bytes
                            )
                    parser, digest, size = parse_index(
                        snapshot.path, candidate, max_bytes=options.max_index_bytes
                    )
                    if (digest, size) != (snapshot.sha256, snapshot.decoded_byte_count):
                        raise PackageDiscoveryError("snapshot_changed_during_sampling")
                    valid_entries = parser.valid_project_entries
                    parsing = dict(
                        anchors_seen=parser.anchors_seen,
                        valid_project_entries=valid_entries,
                        invalid_entries=parser.invalid_entries,
                    )
                    source = dict(
                        kind=source_kind,
                        url=snapshot.source_url,
                        snapshot_path=snapshot.path.relative_to(paths.base_dir).as_posix(),
                        snapshot_sha256=snapshot.sha256,
                        snapshot_retrieved_at=snapshot.retrieved_at,
                        last_serial=snapshot.last_serial,
                        content_type=snapshot.content_type,
                        acquisition_mode="offline_replay"
                        if options.source == "snapshot"
                        else "live",
                        frame_scope="observed_pypi_index",
                        input_file_sha256=None,
                    )
                else:
                    digest, valid_entries = _load_names(options.input_file, candidate, kind="input")
                    parsing = dict(anchors_seen=None, valid_project_entries=None, invalid_entries=0)
                    source = dict(
                        kind=source_kind,
                        url=None,
                        snapshot_path=None,
                        snapshot_sha256=None,
                        snapshot_retrieved_at=None,
                        last_serial=None,
                        content_type=None,
                        acquisition_mode="offline_file",
                        frame_scope="user_supplied_subset",
                        input_file_sha256=digest,
                    )
                db.executemany("INSERT OR IGNORE INTO candidates VALUES (?, ?)", rows)
                rows.clear()
                db.commit()
                unique_count = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                excluded_count = db.execute(
                    "SELECT COUNT(*) FROM candidates WHERE canonical_name IN (SELECT canonical_name FROM exclusions)"
                ).fetchone()[0]
                counts = dict(
                    total_valid_entries=valid_entries,
                    unique_valid_candidates=unique_count,
                    normalized_duplicates=valid_entries - unique_count,
                    explicitly_excluded_candidates=excluded_count,
                    eligible_unique_candidates=unique_count - excluded_count,
                )
                parsing["duplicate_normalized_names"] = counts["normalized_duplicates"]
                if counts["eligible_unique_candidates"] < options.count:
                    raise PackageDiscoveryError("insufficient_eligible_candidates")
                names = [
                    row[0]
                    for row in db.execute(
                        """
                    SELECT canonical_name FROM candidates
                    WHERE canonical_name NOT IN (SELECT canonical_name FROM exclusions)
                    ORDER BY priority ASC, canonical_name ASC LIMIT ?
                """,
                        (options.count,),
                    )
                ]
        # SQLite connection closes before the system-temp directory is removed (Windows).
        return self._export(
            options,
            paths,
            names,
            run_id,
            created_at,
            source,
            counts,
            parsing,
            dict(file_sha256=exclusion_hash, excluded_name_count=excluded_name_count),
            snapshot.path if snapshot else None,
        )

    def _export(
        self,
        options,
        paths,
        names,
        run_id,
        created_at,
        source,
        counts,
        parsing,
        exclusion,
        snapshot_path,
    ):
        if len(names) != options.count or len(set(names)) != options.count:
            raise PackageDiscoveryError("invalid_selected_sample")
        runs = paths.base_dir / "registry" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        final = runs / run_id
        if final.exists():
            raise PackageDiscoveryError("sampling_run_already_exists")
        batch_count = (len(names) + options.batch_size - 1) // options.batch_size
        # Staging is a sibling of runs, not a completed UUID directory.
        with tempfile.TemporaryDirectory(prefix=".sampling-", dir=runs.parent) as staging:
            stage = Path(staging)
            (stage / "batches").mkdir()
            csv_path, targets_path = stage / "package_candidates.csv", stage / "targets.txt"
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(CSV_FIELDS)
                for rank, name in enumerate(names, 1):
                    writer.writerow(
                        (
                            run_id,
                            rank,
                            package_id(name),
                            name,
                            f"batch_{(rank - 1) // options.batch_size + 1:03d}",
                            source["kind"],
                            "name_valid_only",
                            "not_checked",
                            "not_started",
                        )
                    )
                _flush(stream)
            _write_targets(targets_path, names)

            def artifact(path, row_count):
                digest, _ = file_fingerprint(path)
                return dict(
                    relative_path=(final / path.relative_to(stage))
                    .relative_to(paths.base_dir)
                    .as_posix(),
                    sha256=digest,
                    row_count=row_count,
                )

            batches, replayed = [], []
            for index, offset in enumerate(range(0, len(names), options.batch_size), 1):
                batch_id = f"batch_{index:03d}"
                batch = stage / "batches" / f"{batch_id}.txt"
                selected = names[offset : offset + options.batch_size]
                _write_targets(batch, selected)
                actual = batch.read_text(encoding="utf-8").splitlines()
                if actual != [package_id(name) for name in selected] or not 1 <= len(actual) <= 100:
                    raise PackageDiscoveryError("invalid_exported_batch")
                replayed.extend(actual)
                batches.append(dict(batch_id=batch_id, **artifact(batch, len(selected))))
            if (
                targets_path.read_text(encoding="utf-8").splitlines() != replayed
                or len(set(replayed)) != options.count
            ):
                raise PackageDiscoveryError("invalid_exported_targets")
            with csv_path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                csv_ids = [row["package_id"] for row in reader]
                if tuple(reader.fieldnames) != CSV_FIELDS or csv_ids != replayed:
                    raise PackageDiscoveryError("invalid_exported_registry")
            manifest = dict(
                manifest_version="1",
                sampling_run_id=run_id,
                status="complete",
                created_at=created_at,
                finished_at=format_utc_timestamp(self.clock()),
                source=source,
                sampling=dict(
                    algorithm=ALGORITHM,
                    seed=options.seed,
                    requested_count=options.count,
                    selected_count=len(names),
                    batch_size=options.batch_size,
                    batch_count=batch_count,
                ),
                counts=counts,
                parsing=parsing,
                exclusion=exclusion,
                artifacts=dict(
                    candidates_csv=artifact(csv_path, len(names)),
                    targets=artifact(targets_path, len(names)),
                    batches=batches,
                ),
                limitations=list(LIMITATIONS),
            )
            write_json_atomic(stage / "manifest.json", manifest)
            os.replace(stage, final)
        return PackageDiscoveryResult(
            run_id,
            "complete",
            len(names),
            batch_count,
            final / "manifest.json",
            final / "package_candidates.csv",
            final / "targets.txt",
            final / "batches",
            snapshot_path,
        )
