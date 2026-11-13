"""Assemble a first retrospective Model A sample from checked D09 preparations."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from scripts.audit_model_a_splits import audit_splits
from scripts.build_d09_reviewed import build_reviewed
from scripts.collect_d09_batch import parse_rows
from scripts.run_d09_drive import canonical_json, digest, file_digest, jsonl
from scripts.train_model_a import DEFAULT_WINDOWS, load_prepared, minimum_sample_deficits
from tools.coordination._common import atomic_bytes

from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.storage.features.parquet import FeatureStorage


def target_key(row: dict) -> tuple[str, str]:
    return row["package_id"], row["cutoff_at"]


def label_identity(row: dict) -> tuple:
    return row["repo_group_id"], row["label"], row["label_available_at"], row.get("event_id")


def run(prepared_dirs: list[Path], registry_path: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("choose a new sample directory")
    body = registry_path.read_bytes()
    registry = json.loads(body)
    packages = registry["packages"]
    if digest(canonical_json(packages)) != registry["packages_sha256"]:
        raise ValueError("frozen Drive registry changed")
    manifest = {str(registry_path.resolve()): digest(body)}
    reviews, labels = {}, {}
    repo_groups, subjects = set(), set()
    for directory in prepared_dirs:
        _, _, strict, files = load_prepared(directory)
        for path, expected in files.items():
            if manifest.setdefault(path, expected) != expected:
                raise ValueError(f"input manifests disagree on {path}")
        selected = {target_key(row): row for row in strict}
        if len(selected) != len(strict):
            raise ValueError("duplicate strict rows in a prepared dataset")
        review_path = directory / "reviews.jsonl"
        review_body = review_path.read_bytes()
        if digest(review_body) != manifest[str(review_path.resolve())]:
            raise ValueError("prepared reviews changed while assembling the sample")
        for review in parse_rows(review_body):
            key = target_key(review["snapshot"])
            row = selected.pop(key, None)
            if row is None:
                continue
            group, subject = row["repo_group_id"], row["package_id"]
            if subject in subjects or group in repo_groups:
                raise ValueError(
                    f"the sample requires one cutoff per package and repository: {key}"
                )
            if group != review["mapping"]["repo_group_id"]:
                raise ValueError(f"strict row and reviewed target disagree on repository: {key}")
            subjects.add(subject)
            repo_groups.add(group)
            labels[key] = label_identity(row)
            reviews[key] = review
        if selected:
            raise ValueError("strict rows are missing their checked review records")
    if not reviews:
        raise ValueError("at least one verified snapshot and label is required")

    now = format_utc_timestamp(datetime.now(UTC))
    universe = {key: packages[key[0]]["first_upload_at"] for key in reviews}
    selected_reviews = [reviews[key] for key in sorted(reviews)]
    snapshots, outcomes, strict, eligibility = build_reviewed(universe, selected_reviews, now)
    rows = strict.to_dicts()
    if {target_key(row): label_identity(row) for row in rows} != labels:
        raise ValueError("rebuilding changed the label, availability or event identity")
    audit = audit_splits(
        eligibility,
        rows,
        **DEFAULT_WINDOWS,
        fold_fit_at=now,
        selection_freeze_at=now,
        evaluation_as_of=now,
        purpose="retrospective_analysis",
    )
    deficits = minimum_sample_deficits(audit)
    missing = sum(d["negative"] + d["positive"] for d in deficits.values())
    ready = not any(n for split in deficits.values() for n in split.values())
    counts = Counter(row["label"] for row in rows)
    readiness = {
        "status": "minimum_sample_ready" if ready else "blocked_minimum_sample",
        "purpose": audit["purpose"],
        "selection": "Case-selected sample from verified historical inputs",
        "prospective_accuracy_claim_supported": False,
        "eligible_counts": {
            split: report["repo_group_disjoint"] for split, report in audit["splits"].items()
        },
        "minimum_sample_deficits": deficits,
        "minimum_additional_labeled_rows": missing,
    }

    directory = output / "reviewed"
    output.mkdir(parents=True, exist_ok=False)
    directory.mkdir()
    storage = FeatureStorage()
    for filename, frame in (
        ("repository_package_snapshots.parquet", snapshots),
        ("package_labels.parquet", outcomes),
        ("strict_e0_rows.parquet", strict),
    ):
        storage.save_features(directory / filename, frame)
    write_json_atomic(directory / "eligibility.json", eligibility)
    hashes = {str(path.resolve()): file_digest(path) for path in directory.iterdir()}
    review_body = jsonl(selected_reviews)
    atomic_bytes(output / "reviews.jsonl", review_body, immutable=True)
    write_json_atomic(output / "source-manifest.json", manifest)
    write_json_atomic(output / "split-audit.json", audit)
    write_json_atomic(output / "training-readiness.json", readiness)
    release_complete = sum(
        any(
            c["data_group"] == "releases" and c["status"] == "complete"
            for c in r["snapshot"]["coverage"]
        )
        for r in selected_reviews
    )
    write_json_atomic(
        output / "summary.json",
        {
            **readiness,
            "reviewed_at": now,
            "strict_rows": strict.height,
            "strict_class_counts": {str(label): counts[label] for label in (0, 1)},
            "complete_release_censuses": release_complete,
            "reviewed_directories": [str(directory.resolve())],
            "artifact_hashes": hashes,
            "reviews_sha256": digest(review_body),
            "source_manifest_sha256": file_digest(output / "source-manifest.json"),
        },
    )
    return readiness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, action="append", required=True)
    parser.add_argument(
        "--registry", type=Path, default=Path("data/d09/drive-full-v1/historical-registry.json")
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.prepared_dir, args.registry, args.output_dir)
    except (OSError, ValueError, KeyError, pl.exceptions.PolarsError) as error:
        parser.exit(1, f"Model A sample assembly stopped: {error}\n")
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), **report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
