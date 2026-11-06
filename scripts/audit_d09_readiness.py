"""Audit completed D09 batches offline without modifying the live collection."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from scripts.audit_model_a_splits import SPLITS, audit_splits
from scripts.build_d09_reviewed import load_universe, write_reviewed_run
from scripts.collect_d09_batch import parse_rows
from scripts.collect_d09_pilot import stored_source_matches
from scripts.run_d09_drive import (
    DEFAULT_INVENTORY,
    SPLIT_TIMES,
    canonical_json,
    digest,
    file_digest,
    jsonl,
    shard_reviews_digest,
)
from scripts.verify_d09_evidence import (
    OBJECT_NAME,
    load_candidates,
    matching_original,
    pypi_evidence,
    verified_revision,
)
from tools.coordination._common import atomic_bytes

from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import checked_timestamp, format_utc_timestamp, package_id
from reposeer.features.builder import CORE_DATA_GROUPS, FEATURE_COLUMNS
from reposeer.pipelines.d09 import ReviewedD09Target

PENDING = {"download_budget_reached", "archive_download_pending"}


class OfflineHTTP:
    def get(self, *args, **kwargs):
        raise ValueError("offline audit requires the retained provider response")


def split_at(cutoff: str) -> str:
    if cutoff < SPLIT_TIMES["validation_start_at"]:
        return "train"
    return "validation" if cutoff < SPLIT_TIMES["test_start_at"] else "test"


def new_label_timing(as_of: str) -> dict:
    """Apply the existing policy's verification-time floor, without backdating labels."""
    decisions = {
        "train": SPLIT_TIMES["fold_fit_at"],
        "validation": SPLIT_TIMES["selection_freeze_at"],
        "test": as_of,
    }
    return {
        "new_verification_at": as_of,
        "policy": "official verified_at and negative followup.verified_at constrain label_available_at",
        "splits_accepting_a_label_first_verified_now": {
            split: as_of <= decision for split, decision in decisions.items()
        },
        "limitation": "This date check alone does not establish horizon, class, feature or group eligibility.",
    }


def completed_batches(root: Path) -> tuple[list[tuple[Path, dict]], list[str]]:
    completed, pending = [], []
    for directory in sorted((root / "batches").glob("batch-*")):
        path = directory / "verification/summary.json"
        if not path.is_file():
            pending.append(directory.name)
            continue
        receipt = json.loads(path.read_bytes())
        if any(receipt.get("statuses", {}).get(status, 0) for status in PENDING):
            pending.append(directory.name)
            continue
        expected = shard_reviews_digest(directory)
        collection = json.loads((directory / "collection.json").read_bytes())
        if (
            not stored_source_matches(directory / "verification", receipt)
            or receipt.get("source_reviews_sha256") != expected
            or collection.get("source_reviews_sha256") != expected
        ):
            raise ValueError(
                f"completed batch checkpoint failed integrity checks: {directory.name}"
            )
        completed.append((directory, receipt))
    if not completed:
        raise ValueError("no completed batches are available to audit")
    return completed, pending


def checked_mapping(
    directory: Path, row: dict, inventory_dir: Path, inventory, listings, sources: dict
) -> None:
    pypi_source, _ = pypi_evidence(OfflineHTTP(), directory, row)
    if pypi_source != row["pypi_source"]:
        raise ValueError("PyPI source receipt differs from the retained mapping proof")
    source = row["archive_source"]
    if not stored_source_matches(directory, source):
        raise ValueError("original event proof checksum mismatch")
    revision = source["revision"]
    match = OBJECT_NAME.fullmatch(revision["name"])
    if match is None:
        raise ValueError("invalid archive name")
    timestamp = f"{match[1]}T{int(match[2]):02d}:00:00.000Z"
    if inventory.get(timestamp) != revision:
        raise ValueError("mapping proof differs from the frozen archive inventory")
    listing = verified_revision(inventory_dir, timestamp, revision, listings)
    if listing["content_sha256"] != row["inventory_listing_sha256"]:
        raise ValueError("mapping proof inventory receipt changed")
    lines = (directory / source["path"]).read_bytes().splitlines(keepends=True)
    if matching_original(row, source, lines) != row["archive_event"]:
        raise ValueError("mapping proof fails numeric identity or historical availability checks")
    if row["repo_group_id"] != row["repo_id"] or row["official_outcome_review"] != "required":
        raise ValueError("mapping proof has inconsistent identity or review status")
    for base, retained in ((directory, pypi_source), (directory, source), (inventory_dir, listing)):
        sources[str((base / retained["path"]).resolve())] = retained["content_sha256"]
    inventory_receipt = inventory_dir / revision["inventory_receipt"]
    sources[str(inventory_receipt.resolve())] = file_digest(inventory_receipt)


def mirror_material(directory: Path, sources: dict) -> tuple[set, list[dict]]:
    versions, leads = set(), []
    for shard in sorted(directory.glob("shard-*")):
        for receipt_path in sorted((shard / "sources").glob("*.json")):
            if not receipt_path.name.startswith(("distributions", "official-leads-")):
                continue
            receipt = json.loads(receipt_path.read_bytes())
            if not stored_source_matches(shard, receipt):
                raise ValueError(f"mirror response checksum mismatch: {receipt_path}")
            sources[str(receipt_path.resolve())] = file_digest(receipt_path)
            raw_path = shard / receipt["path"]
            sources[str(raw_path.resolve())] = receipt["content_sha256"]
            for row in parse_rows(raw_path.read_bytes()):
                if receipt_path.name.startswith("distributions"):
                    versions.add((package_id(row["name"]), row["version"]))
                else:
                    leads.append(
                        {**row, "source_path": str(raw_path.resolve()), "batch": directory.name}
                    )
    return versions, leads


def read_reviews(paths: list[Path], universe: set, as_of: str, sources: dict):
    accepted, catalog = {}, []
    for path in paths:
        body = path.read_bytes()
        sources[str(path.resolve())] = digest(body)
        rows, matching = parse_rows(body), 0
        for row in rows:
            snapshot = row["snapshot"]
            key = (snapshot["package_id"], snapshot["cutoff_at"])
            if key not in universe:
                continue
            review = ReviewedD09Target.model_validate(row)
            if review.reviewed_at > as_of:
                raise ValueError(f"future-dated review: {key}")
            if key in accepted:
                raise ValueError(f"duplicate reviewed package-cutoff: {key}")
            accepted[key] = row
            matching += 1
        catalog.append({"path": str(path.resolve()), "rows": len(rows), "matching_rows": matching})
    return accepted, catalog


def source_checks(path: Path | None, as_of: str, sources: dict) -> list[dict]:
    if path is None:
        return []
    body = path.read_bytes()
    sources[str(path.resolve())] = digest(body)
    checks = parse_rows(body)
    for check in checks:
        if (
            check.get("label_assigned") is not None
            or checked_timestamp(check["reviewed_at"]) > as_of
        ):
            raise ValueError("source spot checks cannot assign labels or use future review dates")
        for source in check["sources"]:
            retained = Path(source["path"])
            if file_digest(retained) != source["content_sha256"]:
                raise ValueError("source spot check checksum mismatch")
            sources[str(retained.resolve())] = source["content_sha256"]
    return checks


def render_report(report: dict, split_audit: dict) -> str:
    totals = report["counts"]
    lines = [
        "# D09 completed-batch readiness audit",
        "",
        f"Audited at {report['evaluated_at']}. Status: **{report['status']}**.",
        "",
        f"The frozen audit covers {totals['completed_batches']} completed batches, "
        f"{totals['packages']} packages and {totals['package_cutoffs']} package-cutoffs.",
        "",
        "| Check | Count |",
        "| --- | ---: |",
        f"| Original mapping proofs rechecked | {totals['mapping_sources_matched']} |",
        f"| PyPI evidence unverified | {totals['pypi_evidence_unverified']} |",
        f"| Raw keyword search leads | {totals['keyword_leads']} |",
        f"| Reviewed package-cutoffs | {totals['reviewed_rows']} |",
        f"| Built snapshots with at least one CORE feature | {totals['core_usable_rows']} |",
        f"| Strict labeled rows before split checks | {totals['strict_rows']} |",
        "",
        "| Split | Cohort rows | Chronological eligible rows | Unseen-repository eligible rows | Labels 0 / 1 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for split, row in split_audit["splits"].items():
        labels = row["repo_group_disjoint"]["labels"]
        lines.append(
            f"| {split} | {row['cohort_rows']} | {row['chronological']['rows']} | "
            f"{row['repo_group_disjoint']['rows']} | {labels['0']} / {labels['1']} |"
        )
    lines += [
        "",
        "## Findings",
        "",
        "Mapping proofs establish agreement between a historical PyPI artifact and an original "
        "GitHub repository event. They do not establish package ownership, continuous identity, "
        "complete feature coverage or official cessation.",
        "",
        "Proof checks use retained event bytes and the pinned inventory. Original full-object "
        "checksums were recorded by the collector; the audit downloads no archives.",
        "",
        "The retained release mirror provides candidate release histories. Version-specific PyPI "
        "responses and selected original GitHub events do not by themselves certify complete "
        "release or activity censuses. Missing feature groups remain nullable.",
        "",
        "Keyword search is capped and includes API deprecations, dependencies and maintainer "
        "transfers. It is a review queue. A zero lead count cannot certify no cessation. Negative "
        "labels require independently verified full follow-up and maintenance evidence.",
        "",
        "Under the current label policy, newly verified official outcomes and full follow-up "
        f"carry their actual verification date. Labels first verified at {report['evaluated_at']} "
        f"miss fit time {SPLIT_TIMES['fold_fit_at']} and selection time "
        f"{SPLIT_TIMES['selection_freeze_at']}. Historical effective dates cannot replace "
        "verification dates under this policy.",
        "",
        "## Next work",
        "",
        "1. Review package scope and historical identity for the frozen review sample.",
        "2. Verify a complete cutoff release census and build nullable CORE snapshots; obtain "
        "explicit 12-month coverage before claiming complete GitHub counts.",
        "3. Verify official outcomes and full follow-up uniformly across the review cohort. "
        "Keep unknown outcomes and dependencies separate from target-package cessation.",
        "4. Resolve the fold timeline using genuinely pre-existing verified historical labels "
        "or a new prospective evaluation design with an untouched future holdout. "
        "Do not backdate the current reviews.",
        "5. Rebuild strict rows and rerun class, horizon, availability and repository-group gates "
        "before scaling verification as a route to Model A training.",
        "",
        "The review sample is a reproducible readiness probe, not a training sample or a "
        "population performance estimate. Both classes are necessary; passing class presence "
        "alone does not establish sufficient sample size for fitting or calibration.",
        "",
        "This audit does not stop or modify the collection run. Input hashes are in "
        "input-manifest.json; per-row findings are in eligibility.jsonl.",
    ]
    if report["source_spot_checks"]:
        lines += ["", "## Source spot checks", ""]
        for check in report["source_spot_checks"]:
            citations = ", ".join(
                f"[source {index + 1}]({source['url']})"
                for index, source in enumerate(check["sources"])
            )
            lines.append(f"- {check['package_id']}: {check['finding']} {citations}.")
        lines += [
            "",
            "These targeted checks explain concrete failure modes. They are not a prevalence "
            "estimate or a complete follow-up review, and they assign no supervised labels.",
        ]
    return "\n".join(lines) + "\n"


def run(args) -> dict:
    as_of = checked_timestamp(args.as_of or format_utc_timestamp(datetime.now(UTC)))
    if as_of > format_utc_timestamp(datetime.now(UTC)):
        raise ValueError("audit date cannot be in the future")
    root, output = args.run_dir, args.output_dir
    if output.resolve() == root.resolve() or root.resolve().is_relative_to(output.resolve()):
        raise ValueError("use a separate audit output directory")
    batches, pending = completed_batches(root)
    output.mkdir(parents=True, exist_ok=False)
    sources, cohorts, mappings = {}, {}, {}
    all_versions, all_leads = set(), []
    counts, statuses = Counter(), Counter()
    inventory_path = args.inventory_dir / "inventory.json"
    inventory = json.loads(inventory_path.read_bytes())
    sources[str(inventory_path.resolve())] = file_digest(inventory_path)
    anchor = root / "archive-inventory.json"
    if json.loads(anchor.read_bytes())["sha256"] != sources[str(inventory_path.resolve())]:
        raise ValueError("archive inventory differs from the collection checkpoint")
    sources[str(anchor.resolve())] = file_digest(anchor)
    target_path = root / "drive-targets.json"
    frozen = json.loads(target_path.read_bytes())
    if digest(canonical_json(frozen["targets"])) != frozen["targets_sha256"]:
        raise ValueError("frozen Drive targets changed")
    sources[str(target_path.resolve())] = file_digest(target_path)
    target_ids = {row["package_id"] for row in frozen["targets"]}
    listings = {}
    for directory, receipt in batches:
        universe = load_universe(directory)
        if any(key[0] not in target_ids for key in universe):
            raise ValueError("completed batch contains packages outside the frozen Drive cohort")
        if cohorts.keys() & universe.keys():
            raise ValueError("duplicate package-cutoff across completed batches")
        cohorts.update(universe)
        for path in (
            directory / "cohort.json",
            directory / "collection.json",
            directory / "verification/summary.json",
        ):
            sources[str(path.resolve())] = file_digest(path)
        evidence = directory / "verification" / receipt["path"]
        sources[str(evidence.resolve())] = receipt["content_sha256"]
        rows = parse_rows(evidence.read_bytes())
        candidates = {
            (row["package_id"], row["cutoff_at"]): row for row in load_candidates(directory, set())
        }
        if len(rows) != len(candidates) or len(rows) != receipt["candidate_rows"]:
            raise ValueError("mapping candidate membership differs from the source reviews")
        if Counter(row["status"] for row in rows) != Counter(receipt["statuses"]):
            raise ValueError("mapping status counts differ from the checkpoint")
        for row in rows:
            key = (row["package_id"], row["cutoff_at"])
            if key not in universe or key in mappings:
                raise ValueError("invalid or duplicate mapping evidence row")
            candidate = candidates.get(key)
            if candidate is None or any(
                row.get(field) != value for field, value in candidate.items()
            ):
                raise ValueError("mapping evidence differs from its historical candidate")
            if row["status"] == "mapping_sources_matched":
                checked_mapping(
                    directory / "verification",
                    row,
                    args.inventory_dir,
                    inventory,
                    listings,
                    sources,
                )
            mappings[key] = {**row, "batch": directory.name}
            statuses[row["status"]] += 1
        versions, leads = mirror_material(directory, sources)
        all_versions.update(versions)
        all_leads.extend(leads)
        counts["packages"] += len(json.loads((directory / "cohort.json").read_bytes())["packages"])
        print(f"Checked {directory.name}: {len(universe)} package-cutoffs", flush=True)
        for path in directory.glob("shard-*/review.jsonl"):
            sources[str(path.resolve())] = file_digest(path)

    reviews, catalogs = read_reviews(args.reviews, set(cohorts), as_of, sources)
    spot_checks = source_checks(args.source_checks, as_of, sources)
    eligibility, strict, sample = [], [], defaultdict(list)
    feature_counts, coverage_counts = Counter(), {group: Counter() for group in CORE_DATA_GROUPS}
    for directory, _ in batches:
        keys = set(load_universe(directory))
        body = jsonl(reviews[key] for key in sorted(keys & reviews.keys()))
        destination = output / "reviewed" / directory.name
        summary = write_reviewed_run(directory, destination, body or None, as_of)
        for key in ("reviewed_rows", "strict_rows"):
            counts[key] += summary[key]
        feature_path = destination / "repository_package_snapshots.parquet"
        if feature_path.is_file():
            snapshots = pl.read_parquet(feature_path)
            for feature in FEATURE_COLUMNS:
                feature_counts[feature] += snapshots[feature].is_not_null().sum()
            counts["core_usable_rows"] += snapshots.select(
                pl.any_horizontal(pl.col(name).is_not_null() for name in FEATURE_COLUMNS).sum()
            ).item()
            for value in snapshots["coverage_json"]:
                for coverage in json.loads(value):
                    if coverage["data_group"] in coverage_counts:
                        coverage_counts[coverage["data_group"]][coverage["status"]] += 1
        strict_path = destination / "strict_e0_rows.parquet"
        if strict_path.is_file():
            strict.extend(pl.read_parquet(strict_path).iter_rows(named=True))
        for row in json.loads((destination / "eligibility.json").read_bytes()):
            key = (row["package_id"], row["cutoff_at"])
            mapping = mappings.get(key, {})
            row.update(
                batch=directory.name, mapping_status=mapping.get("status", "no_single_id_candidate")
            )
            eligibility.append(row)
            if mapping.get("status") == "mapping_sources_matched":
                sample[split_at(row["cutoff_at"])].append(
                    {
                        "package_id": key[0],
                        "cutoff_at": key[1],
                        "split": split_at(key[1]),
                        "batch": directory.name,
                        "repo_id": mapping["repo_id"],
                        "repo_url": mapping["repo_url"],
                        "scope_quality": mapping["scope_quality"],
                        "review_status": "required",
                        "purpose": "readiness_probe_only",
                    }
                )

    split_audit = audit_splits(eligibility, strict, **SPLIT_TIMES, evaluation_as_of=as_of)
    counts.update(
        completed_batches=len(batches), package_cutoffs=len(cohorts), keyword_leads=len(all_leads)
    )
    counts.update(statuses)
    selected = [
        row
        for split in SPLITS
        for row in sorted(sample[split], key=lambda row: digest(canonical_json(row)))[
            : args.sample_per_split
        ]
    ]
    report = {
        "status": "passes_basic_split_checks"
        if all(
            split_audit["splits"][split]["chronological"]["both_classes"]
            and split_audit["splits"][split]["repo_group_disjoint"]["both_classes"]
            for split in SPLITS
        )
        else "not_train_ready",
        "evaluated_at": as_of,
        "run_dir": str(root.resolve()),
        "audited_batches": [directory.name for directory, _ in batches],
        "pending_batches_excluded": pending,
        "counts": {
            key: counts[key]
            for key in (
                "completed_batches",
                "packages",
                "package_cutoffs",
                "mapping_sources_matched",
                "pypi_evidence_unverified",
                "keyword_leads",
                "reviewed_rows",
                "core_usable_rows",
                "strict_rows",
            )
        },
        "review_inputs": catalogs,
        "source_spot_checks": spot_checks,
        "nonnull_feature_rows": {feature: feature_counts[feature] for feature in FEATURE_COLUMNS},
        "reviewed_coverage_statuses": {
            group: dict(value) for group, value in coverage_counts.items()
        },
        "candidate_material": {
            "mirror_package_versions": len(all_versions),
            "mapping_repo_groups": len(
                {
                    row["repo_id"]
                    for row in mappings.values()
                    if row["status"] == "mapping_sources_matched"
                }
            ),
            "privileged_keyword_leads": sum(
                row.get("association") in ("OWNER", "MEMBER", "COLLABORATOR") for row in all_leads
            ),
            "certifies_feature_or_outcome_coverage": False,
        },
        "new_label_timing": new_label_timing(as_of),
        "class_presence_is_not_sample_size_approval": True,
    }
    # Refuse a moving input snapshot instead of publishing internally inconsistent counts.
    for path, expected in sources.items():
        if file_digest(Path(path)) != expected:
            raise ValueError(f"audit input changed while reading: {path}")
    write_json_atomic(
        output / "input-manifest.json", {"evaluated_at": as_of, "sha256_by_path": sources}
    )
    write_json_atomic(output / "split-audit.json", split_audit)
    write_json_atomic(output / "summary.json", report)
    atomic_bytes(output / "eligibility.jsonl", jsonl(eligibility), immutable=False)
    atomic_bytes(output / "review-sample.jsonl", jsonl(selected), immutable=False)
    atomic_bytes(output / "official-leads.jsonl", jsonl(all_leads), immutable=False)
    atomic_bytes(output / "report.md", render_report(report, split_audit).encode(), immutable=False)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/d09/drive-full-v1"))
    parser.add_argument("--inventory-dir", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, action="append", default=[])
    parser.add_argument("--as-of")
    parser.add_argument(
        "--source-checks",
        type=Path,
        help="Optional checked source observations; never treated as labels",
    )
    parser.add_argument("--sample-per-split", type=int, default=5)
    args = parser.parse_args()
    default_reviews = args.run_dir / "reviews.jsonl"
    if not args.reviews and default_reviews.is_file():
        args.reviews = [default_reviews]
    if not 1 <= args.sample_per_split <= 100:
        parser.error("sample-per-split must be between 1 and 100")
    try:
        result = run(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"D09 readiness audit failed: {error}\n")
    print(json.dumps({"status": result["status"], **result["counts"]}))
    print(f"Readiness report: {(args.output_dir / 'report.md').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
