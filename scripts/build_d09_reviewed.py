"""Build offline D09 snapshots, labels and an eligibility audit from reviewed inputs."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from scripts.collect_d09_batch import CUTOFFS, parse_rows, roster_rows
from scripts.collect_d09_pilot import stored_source_matches

from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import add_calendar_months, checked_timestamp, format_utc_timestamp
from reposeer.features.builder import FEATURE_COLUMNS, FeatureBuilder
from reposeer.pipelines.d09 import ReviewedD09Target
from reposeer.pipelines.maintenance import MaintenancePipeline
from reposeer.storage.features.parquet import FeatureStorage

JOIN_KEYS = ("package_id", "cutoff_at", "repo_id", "repo_group_id", "horizon_end_at")


def load_universe(batch_dir: Path) -> dict[tuple[str, str], str]:
    cohort = json.loads((batch_dir / "cohort.json").read_bytes())
    receipt = json.loads((batch_dir / "sources/cohort.json").read_bytes())
    if not stored_source_matches(batch_dir, receipt):
        raise ValueError("frozen cohort response is missing or its checksum changed")
    packages = roster_rows(parse_rows((batch_dir / receipt["path"]).read_bytes()))
    if packages != cohort["packages"] or cohort["config"]["cutoffs"] != list(CUTOFFS):
        raise ValueError("cohort differs from the archived roster or configured cutoffs")
    return {
        (package["package_id"], cutoff): package["first_upload_at"]
        for package in packages
        for cutoff in CUTOFFS
        if package["first_upload_at"] < cutoff
    }


def build_reviewed(
    universe: dict[tuple[str, str], str], reviews: list[dict], observation_end_at: str
):
    builder = FeatureBuilder()
    maintenance = MaintenancePipeline()
    snapshots, labels = [], []
    audit = {
        key: {
            "package_id": key[0],
            "cutoff_at": key[1],
            "horizon_end_at": add_calendar_months(key[1], 12),
            "disposition": "review_missing",
        }
        for key in sorted(universe)
    }
    seen = set()
    for row in reviews:
        review = ReviewedD09Target.model_validate(row)
        request = review.snapshot
        key = (request.package_id, request.cutoff_at)
        if key not in audit or key in seen:
            raise ValueError(f"duplicate or out-of-cohort review: {key}")
        if request.cutoff_at > observation_end_at:
            raise ValueError(f"review cutoff exceeds observation end: {key}")
        for coverage in request.coverage:
            if coverage.data_group == "releases" and coverage.status == "complete":
                first_upload = min(
                    (release.uploaded_at for release in request.releases), default=None
                )
                if (
                    first_upload is None
                    or first_upload != universe[key]
                    or coverage.time_range.start_at >= first_upload
                    or coverage.records_observed != len(request.releases)
                ):
                    raise ValueError(
                        f"complete PyPI coverage requires the full release history: {key}"
                    )
        seen.add(key)
        result = builder.build_snapshot(request)
        if result.snapshot is None:
            raise ValueError(f"reviewed snapshot failed validation: {key}: {result.errors}")
        snapshot = result.snapshot
        snapshots.append(snapshot)
        label = maintenance.evaluate_horizon_label(
            package_id=request.package_id,
            repo_id=review.mapping.repo_id,
            cutoff_at=request.cutoff_at,
            observation_end_at=observation_end_at,
            official_events=review.official_events,
            horizon_releases=review.horizon_releases,
            horizon_activities=review.horizon_activities,
            horizon_coverage_complete=review.followup is not None,
            horizon_coverage_available_at=review.followup.verified_at if review.followup else None,
        )
        labels.append(label)
        core_usable = any(value is not None for value in snapshot.features.model_dump().values())
        audit[key].update(
            repo_group_id=review.mapping.repo_group_id,
            mapping_revision=review.mapping.mapping_revision,
            scope_quality=review.mapping.scope_quality,
            core_usable=core_usable,
            label_status=label.label_status,
            label_available_at=label.label_available_at,
            censor_reason=label.censor_reason,
            disposition="core_unavailable" if not core_usable else label.label_status,
        )

    snapshot_df = builder.snapshots_to_polars(snapshots)
    label_df = maintenance.labels_to_polars(labels)
    strict_labels = label_df.filter(pl.col("in_risk_set") & pl.col("label").is_in([0, 1]))
    strict = (
        snapshot_df.filter(
            pl.any_horizontal(pl.col(column).is_not_null() for column in FEATURE_COLUMNS)
        )
        .join(strict_labels, on=list(JOIN_KEYS), how="inner", validate="1:1")
        .with_columns(pl.col("horizon_end_at").alias("outcome_end_at"))
        .sort("cutoff_at", "package_id")
    )
    return snapshot_df, label_df, strict, list(audit.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--reviews", type=Path, help="JSONL with one ReviewedD09Target per line")
    parser.add_argument("--observation-end-at", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        observation_end_at = checked_timestamp(args.observation_end_at)
        if observation_end_at > format_utc_timestamp(datetime.now(UTC)):
            raise ValueError("observation end cannot be in the future")
        body = args.reviews.read_bytes() if args.reviews else b""
        snapshots, labels, strict, audit = build_reviewed(
            load_universe(args.batch_dir), parse_rows(body), observation_end_at
        )
        args.output_dir.mkdir(parents=True, exist_ok=False)
        storage = FeatureStorage()
        if not snapshots.is_empty():
            storage.save_features(
                args.output_dir / "repository_package_snapshots.parquet", snapshots
            )
            storage.save_features(args.output_dir / "package_labels.parquet", labels)
        if not strict.is_empty():
            storage.save_features(args.output_dir / "strict_e0_rows.parquet", strict)
        write_json_atomic(args.output_dir / "eligibility.json", audit)
        class_counts = {
            str(value): strict.filter(pl.col("label") == value).height for value in (0, 1)
        }
        write_json_atomic(
            args.output_dir / "summary.json",
            {
                "observation_end_at": observation_end_at,
                "cohort_sha256": hashlib.sha256(
                    (args.batch_dir / "cohort.json").read_bytes()
                ).hexdigest(),
                "reviews_sha256": hashlib.sha256(body).hexdigest() if args.reviews else None,
                "universe_rows": len(audit),
                "reviewed_rows": labels.height,
                "strict_rows": strict.height,
                "strict_class_counts": class_counts,
                "strict_repo_groups": strict["repo_group_id"].n_unique(),
                "dispositions": dict(Counter(row["disposition"] for row in audit)),
                "status": "ready_for_split_audit"
                if all(class_counts.values())
                else "insufficient_strict_labels",
            },
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"D09 reviewed build stopped: {error}\n")
    print(f"D09 eligibility audit: {args.output_dir.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
