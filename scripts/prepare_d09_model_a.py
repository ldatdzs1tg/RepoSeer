"""Build cutoff CORE snapshots from checked PyPI histories and explicit outcome reviews."""

import argparse
import json
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import Field, model_validator
from scripts.audit_d09_readiness import completed_batches
from scripts.build_d09_reviewed import write_reviewed_run
from scripts.collect_d09_batch import parse_rows, release_history
from scripts.collect_d09_pilot import provider_time, pypi_json, run_lock, stored_source_matches
from scripts.d09_release_census import recover_artifacts
from scripts.run_d09_drive import canonical_json, digest, file_digest, jsonl
from scripts.verify_d09_evidence import rounded_up_time
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.analysis import AnalysisEvidence, SourceProvenance
from reposeer.contracts.identity import (
    add_calendar_months,
    format_utc_timestamp,
    package_id,
    parse_repo_id,
)
from reposeer.contracts.models import ContractModel, NonEmpty, PackageId, RepoId, UtcTimestamp
from reposeer.exceptions import HTTPClientError
from reposeer.maintenance.labels import HistoricalOfficialStatus
from reposeer.pipelines.d09 import ReviewedD09Target, ReviewedFollowup


class OutcomeReview(ContractModel):
    package_id: PackageId
    repo_id: RepoId
    cutoff_at: UtcTimestamp | None = None
    verifier: NonEmpty
    reviewed_at: UtcTimestamp
    sources: list[SourceProvenance] = Field(min_length=1)
    source_paths: dict[NonEmpty, NonEmpty]
    official_search_sources: list[NonEmpty] = Field(min_length=1)
    official_evidence: list[AnalysisEvidence] = Field(default_factory=list)
    official_events: list[HistoricalOfficialStatus] = Field(default_factory=list)
    followup: ReviewedFollowup | None = None

    @model_validator(mode="after")
    def check_followup_scope(self):
        if {row.source_id for row in self.sources} != set(self.source_paths):
            raise ValueError("every reviewed source requires a retained local revision")
        if self.followup is not None and self.cutoff_at is None:
            raise ValueError("negative follow-up approval must identify one package-cutoff")
        if self.cutoff_at is None and not self.official_events:
            raise ValueError("a package-wide review requires explicit official events")
        return self


def raw_id(value: str) -> str:
    return "raw:" + str(uuid.UUID(value[:32], version=4))


def provenance(kind, receipt, valid, available, sha=None):
    return {
        "source": kind,
        "source_url": receipt["url"],
        "retrieved_at": receipt["retrieved_at"],
        "valid_at": valid,
        "available_at": available,
        "temporal_basis": "historical_reconstruction",
        "raw_record_ids": [raw_id(sha or receipt["content_sha256"])],
        "missing_fields": [],
    }


def source(source_id, kind, receipt, valid, available, sha=None, revision=None, query_hash=None):
    return {
        **provenance(kind, receipt, valid, available, sha),
        "source_id": source_id,
        "source_record_id": None,
        "revision": revision,
        "content_sha256": sha or receipt["content_sha256"],
        "query_set_hash": query_hash,
    }


def fetch_project(output: Path, subject: str) -> tuple[dict | None, dict]:
    key = digest(subject.encode())
    directory = output / "projects" / key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "receipt.json"
    receipt = json.loads(path.read_bytes()) if path.exists() else {}
    if receipt.get("status") == "not_found":
        return None, receipt
    if stored_source_matches(directory, receipt):
        return json.loads((directory / receipt["path"]).read_bytes()), receipt
    url = f"https://pypi.org/pypi/{subject.removeprefix('pypi:')}/json"
    try:
        with HTTPClient(timeout=30, max_retries=2) as http:
            payload, receipt = pypi_json(http, url, directory, "project")
    except HTTPClientError as error:
        if error.status_code != 404:
            raise
        receipt = {"status": "not_found", "url": url}
        write_json_atomic(path, receipt)
        return None, receipt
    write_json_atomic(path, receipt)
    return payload, receipt


def checked_release_census(
    subject: str, history: list[dict], payload: dict, end: str, restored_artifacts=frozenset()
):
    """Require agreement of the full version set, first uploads and artifact digests."""
    if package_id(payload["info"]["name"]) != subject:
        raise ValueError("project identity differs from the frozen target")
    mirror = {row["version"]: row for row in history if row["uploaded_at"] <= end}
    observed, verified = {}, []
    for version, files in payload["releases"].items():
        if not files:
            continue
        first = min(files, key=lambda item: provider_time(item["upload_time_iso_8601"]))
        timestamp = provider_time(first["upload_time_iso_8601"])
        if rounded_up_time(timestamp) > end:
            continue
        observed[version] = first
        row = mirror.get(version)
        if row is None:
            continue
        matches = [item for item in files if item["digests"]["sha256"] == row["sha256"]]
        if not any(
            format_utc_timestamp(provider_time(item["upload_time_iso_8601"])) == row["uploaded_at"]
            and provider_time(item["upload_time_iso_8601"]) == timestamp
            for item in matches
        ):
            continue
        verified.append({**row, "available_at": rounded_up_time(timestamp)})
    verified_versions = {row["version"] for row in verified}
    for version, row in mirror.items():
        if version in verified_versions or row["sha256"] not in restored_artifacts:
            continue
        current = payload["releases"].get(version, [])
        # An earlier surviving publisher upload contradicts the proposed first release.
        if current and min(
            provider_time(item["upload_time_iso_8601"]) for item in current
        ) < provider_time(row["uploaded_at"]):
            continue
        # The historical mirror stores milliseconds. A removed file has no surviving
        # higher precision PyPI timestamp, so certify availability one tick later.
        available = format_utc_timestamp(
            provider_time(row["uploaded_at"]) + timedelta(milliseconds=1)
        )
        if available > end:
            continue
        observed[version] = row
        verified.append({**row, "available_at": available})
    complete = bool(verified) and set(observed) == set(mirror) == {
        row["version"] for row in verified
    }
    return sorted(verified, key=lambda row: (row["uploaded_at"], row["version"])), complete


def build_target(
    candidate,
    package,
    history,
    history_receipt,
    project,
    project_source,
    checked_at,
    outcome=None,
    census_dir: Path | None = None,
    restored_artifacts=frozenset(),
):
    subject, cutoff = candidate["package_id"], candidate["cutoff_at"]
    horizon = add_calendar_months(cutoff, 12)
    before, full_before = checked_release_census(
        subject, history, project, cutoff, restored_artifacts
    )
    through_horizon, full_horizon = checked_release_census(
        subject, history, project, horizon, restored_artifacts
    )
    full_before = full_before and before[0]["uploaded_at"] == package["first_upload_at"]
    after = [row for row in through_horizon if cutoff < row["uploaded_at"] <= horizon]

    def release(row):
        return {
            "schema_version": "0.1.0",
            "record_type": "package_release",
            "package_id": subject,
            "repo_id": None,
            "version": row["version"],
            "uploaded_at": row["uploaded_at"],
            "requires_python": row["requires_python"] or None,
            "dependencies": None,
            "is_yanked": None,
            "yanked_reason": None,
            "yanked_status_at": None,
            "provenance": provenance(
                "pypi_bigquery" if row["sha256"] in restored_artifacts else "pypi_json",
                history_receipt if row["sha256"] in restored_artifacts else project_source,
                row["available_at"],
                row["available_at"],
                row["sha256"],
            ),
        }

    pypi, archive = candidate["pypi_source"], candidate["archive_source"]
    pypi_at = rounded_up_time(provider_time(pypi["artifact_uploaded_at"]))
    archive_at = rounded_up_time(provider_time(archive["revision"]["timeCreated"]))
    identity_at = max(pypi_at, archive_at)
    mapping_sources = [
        source(
            "mapping-pypi",
            "pypi_json",
            pypi,
            pypi_at,
            pypi_at,
            revision=candidate["distribution_sha256"],
        ),
        source(
            "mapping-archive",
            "gh_archive",
            archive,
            candidate["archive_event"]["event_at"],
            archive_at,
            candidate["archive_event"]["record_sha256"],
            archive["revision"]["generation"],
        ),
    ]
    observed_at = project_source["retrieved_at"]
    observed_source = source(
        "project-observation", "pypi_json", project_source, observed_at, observed_at
    )
    observed_source["temporal_basis"] = "observation"
    coverage, coverage_sources, core_sources = [], {}, []
    if full_before:
        census_body = canonical_json(before)
        sha = digest(census_body)
        if census_dir is not None:
            atomic_bytes(census_dir / f"{sha}.json", census_body, immutable=True)
        available = max(row["available_at"] for row in before)
        census = {
            **history_receipt,
            "retrieved_at": max(history_receipt["retrieved_at"], project_source["retrieved_at"]),
        }
        core_sources = [
            source(
                "release-census",
                "pypi_bigquery",
                census,
                available,
                available,
                sha,
                history_receipt["content_sha256"],
                history_receipt["query_sha256"],
            )
        ]
        coverage_sources = {"releases": ["release-census"]}
        coverage.append(
            {
                "data_group": "releases",
                "status": "complete",
                "records_observed": len(before),
                "time_range": {"start_at": "1990-01-01T00:00:00.000Z", "end_at": cutoff},
                "missing_reason": None,
            }
        )
    else:
        coverage.append(
            {
                "data_group": "releases",
                "status": "unavailable",
                "records_observed": 0,
                "time_range": None,
                "missing_reason": "historical_unavailable",
            }
        )
    for group in ("commits", "contributors", "issues", "pull_requests"):
        coverage.append(
            {
                "data_group": group,
                "status": "unavailable",
                "records_observed": 0,
                "time_range": None,
                "missing_reason": "not_collected",
            }
        )
    record = {
        "snapshot": {
            "schema_version": "0.1.0",
            "record_type": "snapshot_build_request",
            "request_id": raw_id(digest(f"model-a-core:{subject}:{cutoff}".encode()))[4:],
            "run_id": raw_id(digest(b"d09-model-a-release-core-v1"))[4:],
            "package_id": subject,
            "repository": {
                "repo_id": candidate["repo_id"],
                "github_repo_id": parse_repo_id(candidate["repo_id"]),
                "github_node_id": None,
                "repo_full_name": candidate["repo_url"][19:],
                "repo_url": candidate["repo_url"],
                "known_urls": [],
            },
            "cutoff_at": cutoff,
            "horizon_months": 12,
            "releases": [release(row) for row in before],
            "activities": [],
            "coverage": coverage,
        },
        "mapping": {
            "repo_id": candidate["repo_id"],
            "repo_group_id": candidate["repo_id"],
            "mapping_revision": candidate["distribution_sha256"]
            + ":"
            + candidate["archive_event"]["record_sha256"],
            "scope": f"{subject} declared repository; package ownership not inferred from URL agreement",
            "scope_quality": "repository_only",
            "valid_from": identity_at,
            "valid_to": format_utc_timestamp(provider_time(cutoff) + timedelta(milliseconds=1)),
            "available_at": identity_at,
            "source_ids": [row["source_id"] for row in mapping_sources],
        },
        "verifier": "reposeer:release-census-v1",
        "reviewed_at": checked_at,
        "sources": [*mapping_sources, observed_source, *core_sources],
        "core_coverage_sources": coverage_sources,
        "official_search_sources": ["project-observation"],
        "official_evidence": [],
        "official_events": [],
        "followup": None,
        "horizon_releases": [release(row) for row in after],
        "horizon_activities": [],
    }
    if outcome is not None:
        if outcome.repo_id != candidate["repo_id"] or outcome.reviewed_at > checked_at:
            raise ValueError("outcome review has a different repo identity or a future review time")
        if outcome.followup is not None and not full_horizon:
            raise ValueError("approved follow-up lacks a matching complete release census")
        record.update(
            verifier=outcome.verifier + "+reposeer:release-census-v1",
            official_search_sources=outcome.official_search_sources,
            official_evidence=[row.model_dump() for row in outcome.official_evidence],
            official_events=[row.model_dump() for row in outcome.official_events],
            followup=outcome.followup.model_dump() if outcome.followup else None,
        )
        record["sources"].extend(row.model_dump() for row in outcome.sources)
    reviewed = ReviewedD09Target.model_validate(record)
    return reviewed, {"cutoff_release_census": full_before, "horizon_release_census": full_horizon}


def load_outcomes(body: bytes) -> dict[tuple[str, str | None], OutcomeReview]:
    outcomes = {}
    for row in parse_rows(body):
        review = OutcomeReview.model_validate(row)
        for retained in review.sources:
            source_path = Path(review.source_paths[retained.source_id])
            if file_digest(source_path) != retained.content_sha256:
                raise ValueError("outcome source checksum mismatch")
        for evidence in review.official_evidence:
            for citation in evidence.citations:
                text = Path(review.source_paths[citation.source_id]).read_text(encoding="utf-8")
                if text[citation.start_offset : citation.end_offset] != citation.quote:
                    raise ValueError("official outcome citation does not match its source revision")
        key = (review.package_id, review.cutoff_at)
        if key in outcomes:
            raise ValueError("duplicate outcome review")
        outcomes[key] = review
    return outcomes


def run(args):
    outcome_body = args.outcomes.read_bytes() if args.outcomes else b""
    outcomes = load_outcomes(outcome_body)
    approved_only = getattr(args, "approved_only", False)
    if approved_only and (not outcomes or any(cutoff is None for _, cutoff in outcomes)):
        raise ValueError("--approved-only requires at least one outcome with an exact cutoff")
    approved_subjects = {subject for subject, _ in outcomes}
    output = args.output_dir
    cache_root = args.project_cache_dir or output
    output.mkdir(parents=True, exist_ok=True)
    batches, _ = completed_batches(args.run_dir)
    manifest = {str(path.resolve()): file_digest(path / "cohort.json") for path, _ in batches}
    frozen = output / "cohort-inputs.json"
    if frozen.exists():
        original = json.loads(frozen.read_bytes())
        if any(manifest.get(key) != value for key, value in original.items()):
            raise ValueError("a frozen input batch changed or is no longer complete")
        manifest = original
        batches = [(path, receipt) for path, receipt in batches if str(path.resolve()) in manifest]
    else:
        write_json_atomic(frozen, manifest)
    packages, candidates, histories, receipts, package_batches = {}, {}, {}, {}, {}
    repair_material = {}
    release_material = defaultdict(dict)
    for directory, _ in batches:
        cohort = json.loads((directory / "cohort.json").read_bytes())
        by_id = {row["package_id"]: row for row in cohort["packages"]}
        packages.update(by_id)
        package_batches.update({subject: directory for subject in by_id})
        for row in parse_rows((directory / "verification/mapping_evidence.jsonl").read_bytes()):
            key = (row["package_id"], row["cutoff_at"])
            if row["status"] == "mapping_sources_matched" and (not approved_only or key in outcomes):
                candidates[key] = row
        if approved_only and not approved_subjects.intersection(by_id):
            continue
        with HTTPClient(timeout=45, max_retries=2) as http:
            for shard in sorted(directory.glob("shard-*")):
                shard_rows = parse_rows((shard / "review.jsonl").read_bytes())
                subjects = {row["package_id"] for row in shard_rows}
                if approved_only and not approved_subjects.intersection(subjects):
                    continue
                for row in shard_rows:
                    retained = row.get("metadata_repair_source")
                    if retained is not None:
                        if retained["content_sha256"] != row[
                            "distribution_sha256"
                        ] or not stored_source_matches(shard, retained):
                            raise ValueError("repaired distribution metadata is missing or corrupt")
                        repair_material[str((shard / retained["path"]).resolve())] = retained[
                            "content_sha256"
                        ]
                histories.update(
                    release_history(http, shard, [by_id[subject] for subject in sorted(subjects)])
                )
                for path in sorted((shard / "sources").glob("distributions*.json")):
                    receipt = json.loads(path.read_bytes())
                    if not stored_source_matches(shard, receipt):
                        raise ValueError("release mirror source is missing or corrupt")
                    for row in parse_rows((shard / receipt["path"]).read_bytes()):
                        subject = package_id(row["name"])
                        receipts.setdefault(subject, receipt)
                        release_material[subject][str((shard / receipt["path"]).resolve())] = (
                            receipt["content_sha256"]
                        )
    if approved_only and (missing := outcomes.keys() - candidates.keys()):
        raise ValueError(f"{len(missing)} approved cutoffs lack a verified mapping: {sorted(missing)[:5]}")
    selected = {row["package_id"] for row in candidates.values()}
    if args.package:
        selected.intersection_update(
            package_id(name.removeprefix("pypi:")) for name in args.package
        )
    selected = sorted(selected)
    already = {
        subject
        for subject in selected
        if (cache_root / "projects" / digest(subject.encode()) / "receipt.json").exists()
    }
    pending = [subject for subject in selected if subject not in already]
    budgeted = pending[: args.max_packages] if args.max_packages else pending
    checked_projects = already | set(budgeted)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for subject, result in zip(
            budgeted, pool.map(lambda item: fetch_project(cache_root, item), budgeted), strict=True
        ):
            print(
                f"Checked PyPI project {subject} ({'available' if result[0] else 'not found'})",
                flush=True,
            )
    checked_at = format_utc_timestamp(datetime.now(UTC))
    body_rows, findings = [], []
    applied_outcomes = 0
    project_cache, material_sources, restored = {}, dict(repair_material), {}
    material_sources[str(frozen.resolve())] = file_digest(frozen)
    repair_budget = args.repair_artifact_budget
    source_checks = set()
    for key, candidate in sorted(candidates.items()):
        if key[0] not in checked_projects:
            continue
        if key[0] not in project_cache:
            project_cache[key[0]] = fetch_project(cache_root, key[0])
        project, receipt = project_cache[key[0]]
        if project is None:
            continue
        if key[0] not in restored:
            client = HTTPClient(timeout=45, max_retries=1) if repair_budget else nullcontext()
            with client as http:
                proofs, materials, attempted = recover_artifacts(
                    http,
                    cache_root / "restored" / digest(key[0].encode())[:16],
                    key[0],
                    histories[key[0]],
                    project,
                    repair_budget,
                )
            restored[key[0]] = proofs
            material_sources.update(materials)
            repair_budget -= attempted
        batch = package_batches[key[0]]
        for retained in (candidate["pypi_source"], candidate["archive_source"]):
            proof = batch / "verification" / retained["path"]
            check_key = (str(proof.resolve()), retained["content_sha256"])
            if check_key not in source_checks:
                if not stored_source_matches(batch / "verification", retained):
                    raise ValueError("mapping source is missing or corrupt")
                source_checks.add(check_key)
                material_sources[check_key[0]] = check_key[1]
        project_path = cache_root / "projects" / digest(key[0].encode()) / receipt["path"]
        material_sources[str(project_path.resolve())] = receipt["content_sha256"]
        material_sources.update(release_material[key[0]])
        outcome = outcomes.get(key, outcomes.get((key[0], None)))
        review, finding = build_target(
            candidate,
            packages[key[0]],
            histories[key[0]],
            receipts[key[0]],
            project,
            receipt,
            checked_at,
            outcome,
            output / "censuses",
            restored[key[0]],
        )
        applied_outcomes += outcome is not None
        body_rows.append(review.model_dump())
        findings.append({"package_id": key[0], "cutoff_at": key[1], **finding})
    body = jsonl(body_rows)
    atomic_bytes(output / "reviews.jsonl", body, immutable=False)
    results = []
    artifact_hashes = {}
    totals = {"strict_rows": 0, "strict_class_counts": {"0": 0, "1": 0}}
    for directory, _ in batches:
        subjects = {
            row["package_id"]
            for row in json.loads((directory / "cohort.json").read_bytes())["packages"]
        }
        part = jsonl(row for row in body_rows if row["snapshot"]["package_id"] in subjects)
        if approved_only and not part:
            continue
        destination = output / "reviewed" / directory.name / digest(part)
        if not (destination / "summary.json").exists():
            write_reviewed_run(directory, destination, part or None, checked_at)
        built = json.loads((destination / "summary.json").read_bytes())
        totals["strict_rows"] += built["strict_rows"]
        for label in ("0", "1"):
            totals["strict_class_counts"][label] += built["strict_class_counts"][label]
        results.append(str(destination.resolve()))
        for path in destination.iterdir():
            if path.is_file():
                artifact_hashes[str(path.resolve())] = file_digest(path)
    if approved_only and totals["strict_rows"] != len(outcomes):
        raise ValueError(
            f"{totals['strict_rows']}/{len(outcomes)} approved cutoffs produced strict rows. "
            "Check the reviewed eligibility reports and rerun the same preparation."
        )
    summary = {
        "status": "project_budget_reached" if len(pending) > len(budgeted) else "prepared",
        "purpose": "retrospective_analysis",
        "reviewed_at": checked_at,
        "selected_projects": len(selected),
        "project_sources_checked": len(checked_projects),
        "snapshot_inputs": len(body_rows),
        "complete_release_censuses": sum(row["cutoff_release_census"] for row in findings),
        "outcome_approvals": len(outcomes),
        "outcome_reviews_applied": applied_outcomes,
        "restored_original_release_files": sum(len(proofs) for proofs in restored.values()),
        **totals,
        "reviewed_directories": results,
        "artifact_hashes": artifact_hashes,
        "reviews_sha256": digest(body),
        "limitations": [
            "GitHub activity groups remain nullable",
            "No cessation or negative label is inferred from missing activity or keyword leads",
        ],
    }
    atomic_bytes(output / "census-audit.jsonl", jsonl(findings), immutable=False)
    for census_path in (output / "censuses").glob("*.json"):
        material_sources[str(census_path.resolve())] = file_digest(census_path)
    if args.outcomes:
        material_sources[str(args.outcomes.resolve())] = digest(outcome_body)
        for outcome in outcomes.values():
            material_sources.update(
                {
                    outcome.source_paths[source.source_id]: source.content_sha256
                    for source in outcome.sources
                }
            )
    for path, expected in material_sources.items():
        if file_digest(Path(path)) != expected:
            raise ValueError("a source changed while preparing the dataset")
    write_json_atomic(output / "source-manifest.json", material_sources)
    summary["source_manifest_sha256"] = file_digest(output / "source-manifest.json")
    write_json_atomic(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/d09/drive-full-v1"))
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/d09/model-a-retrospective-v1")
    )
    parser.add_argument("--outcomes", type=Path)
    parser.add_argument(
        "--approved-only",
        action="store_true",
        help="Prepare exact package-cutoffs from --outcomes and require every approval to qualify",
    )
    parser.add_argument(
        "--project-cache-dir",
        type=Path,
        help="Reuse checked project responses from another preparation run",
    )
    parser.add_argument(
        "--repair-artifact-budget",
        type=int,
        default=0,
        help="Maximum original release files to retrieve when the current index no longer lists the first upload",
    )
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument(
        "--max-packages", type=int, default=30, help="New project responses this pass; 0 means all"
    )
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.approved_only and (args.outcomes is None or args.package):
        parser.error("--approved-only uses the cutoff list from --outcomes")
    if args.max_packages < 0 or args.repair_artifact_budget < 0 or not 1 <= args.workers <= 8:
        parser.error("expected nonnegative project budget and 1–8 workers")
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output_dir):
            summary = run(args)
    except (OSError, ValueError, KeyError, TypeError, HTTPClientError) as error:
        parser.exit(1, f"Model A preparation stopped: {error}\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
