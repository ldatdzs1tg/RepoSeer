"""Shared mounted-submission supplement planner, receipt reader and coverage view."""

import csv
import io
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from tools.coordination import _common as c

from reposeer.collection.provider_preflight import (
    EXTERNAL_SOURCES,
    TERMINAL_PROVIDER_STATES,
    ProviderState,
    availability_state,
)
from reposeer.contracts import RawRecord, validate_record
from reposeer.contracts.identity import canonical_article_url
from reposeer.exceptions import CollectionError, StorageError
from reposeer.pipelines.external_crawl import _group_candidates, _select_candidate_groups
from reposeer.pipelines.external_enrichment import ExternalEnrichmentPipeline, discovery_status
from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage

POLICY = "external-v2"
AUTO_ENRICH_MAX_PACKAGES = 1
OPTIONS = {key: value for key, value in c.policy_options("distributed-v2").items()
           if key not in {"max_pages", "max_records"}}


def _worker():
    from tools.coordination import worker
    return worker


def scan_parts(submissions: Path) -> tuple[list[Path], list[dict]]:
    parts, errors = [], []
    for run in sorted(submissions.iterdir()):
        if c.desktop_metadata(run.name):
            continue
        try:
            c.checked_mount_path(run)
            c.uid(run.name)
            c.require(run.is_dir(), "invalid_sampling_run_directory")
            for repo in sorted(run.iterdir()):
                c.checked_mount_path(repo)
                c.require(repo.is_dir() and len(repo.name) == 64 and all(ch in "0123456789abcdef" for ch in repo.name), "invalid_repository_directory")
                for part in sorted(repo.iterdir()):
                    c.checked_mount_path(part)
                    if part.name == "supplements" or c.desktop_metadata(part.name):
                        continue
                    c.require(part.is_dir() and part.name.startswith("part_") and part.name[5:].isdecimal(), "invalid_source_part")
                    parts.append(part)
        except (OSError, ValueError, TypeError, KeyError, CollectionError, StorageError) as error:
            errors.append(dict(source=run.name, error_code=str(error) if isinstance(error, c.CoordinationError) else type(error).__name__))
    return parts, errors


def supplement_root(source: Path) -> Path:
    return c.safe_path(source.parent, f"supplements/{source.name}/{POLICY}")


def provider_input(source_facts: dict, scope, provider: str, options: dict) -> dict:
    key = scope.pypi.package_id
    return {**source_facts, "package_ids": [key], "package_id": key, "provider": provider,
            "verified_mapping_evidence_refs": sorted(entry["raw_record_id"] for entry in scope.resolve()["evidence"]),
            "external_collection_options": options}


def verify_receipt(root: Path, expected: dict, *, marker: dict | None = None) -> dict:
    root = c.checked_mount_path(root)
    marker = c.read_json(c.safe_path(root, "READY.json")) if marker is None else marker
    saved = c.read_json(c.safe_path(root, "ENRICHMENT_INPUT.json"))
    c.require(type(marker.get("schema_version")) is int and type(saved.get("schema_version")) is int, "invalid_enrichment_schema")
    for value in (marker, saved):
        c.require(all(value.get(key) == item for key, item in expected.items()), "source_fingerprint_mismatch")
    c.timestamp(saved["created_at_utc"])
    c.timestamp(marker["completed_at_utc"])
    c.uid(marker["enrichment_run_id"])
    c.require(marker["collection_status"] in TERMINAL_PROVIDER_STATES, "enrichment_not_successful")
    checksums = c.safe_path(root, "CHECKSUMS.sha256")
    declared = c.read_checksums(checksums)
    c.require(c.fingerprint(checksums)[0] == marker["checksums_sha256"], "checksum_list_mismatch")
    c.require(declared == {name: value for name, value in c.files(root, declared=declared).items()
                           if name not in {"CHECKSUMS.sha256", "READY.json"}}, "bundle_checksum_mismatch")
    report = c.read_json(c.safe_path(root, "reports/external_enrichment.json"))
    c.require(marker["report"] == report and report["status"] == marker["collection_status"]
              and report["provider"] == expected["provider"]
              and report["enrichment_run_id"] == marker["enrichment_run_id"], "enrichment_report_mismatch")
    from reposeer.collectors.external.crawler.validation import validate_external_document_record
    records = JsonlStorage().read_contract_records(c.safe_path(root, "raw/external/documents.jsonl"))
    candidates = JsonlStorage().read_contract_records(c.safe_path(root, "raw/external/discovery.jsonl"))
    candidate_ids = {raw.raw_record_id for raw in candidates}
    c.require(report["candidates_discovered"] == len({raw.payload["candidate_id"] for raw in candidates})
              and all(raw.payload.get("discovery_provider") == expected["provider"]
                      and raw.payload.get("search_target_package_id") == expected["package_id"] for raw in candidates),
              "enrichment_candidate_mismatch")
    c.require(set(report["document_associations"]) == set(report["document_raw_ids"])
              and all(set(ids) <= candidate_ids for ids in report["document_associations"].values()), "enrichment_lineage_mismatch")
    c.require(len(report["attempted_urls"]) <= 20, "enrichment_budget_exceeded")
    for raw in records:
        validate_external_document_record(raw)
    c.require(report["document_raw_ids"] == sorted(raw.raw_record_id for raw in records), "enrichment_counts_mismatch")
    c.require(report["documents_extracted"] == sum(raw.payload["content_status"] == "full" for raw in records), "enrichment_counts_mismatch")
    return marker


def seal(root: Path, expected: dict, report: dict) -> dict:
    inventory = c.files(root)
    lines = "".join(f"{sha} {size} {name}\n" for name, (sha, size) in inventory.items()
                    if name not in {"CHECKSUMS.sha256", "READY.json"})
    c.atomic_bytes(root / "CHECKSUMS.sha256", lines.encode())
    marker = dict(**expected, enrichment_run_id=report["enrichment_run_id"], report=report,
                  collection_status=report["status"], checksums_sha256=c.digest(lines.encode()),
                  completed_at_utc=c.format_utc_timestamp(c.now()))
    verify_receipt(root, expected, marker=marker)
    c.write_json(root / "READY.json", marker)
    return marker


@contextmanager
def writer_lock(path: Path):
    path = c.checked_mount_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = str(uuid4()).encode()
    try:
        with path.open("xb") as stream:
            stream.write(token)
            stream.flush()
    except FileExistsError:
        raise c.CoordinationError("concurrent_writer_requires_inspection") from None
    try:
        yield
    finally:
        c.require(path.read_bytes() == token, "writer_lock_changed")
        path.unlink()


def _raw_records(path: Path):
    if not path.exists():
        return
    with path.open("rb") as stream:
        while line := stream.readline(32 * 1024**2 + 1):
            c.require(len(line) <= 32 * 1024**2, "external_raw_line_size_limit")
            raw = validate_record(line)
            c.require(isinstance(raw, RawRecord), "noncanonical_external_raw")
            yield raw


def initial_evidence(source: Path, package: str, *, keep_documents: bool = True) -> dict:
    """Published source manifests establish only the scope actually attempted."""
    marker = c.read_json(source / "READY.json")
    manifest = c.read_json(source / f"metadata/collection_runs/integration_{marker['integration_run_id']}.json")
    discoveries = [entry for entry in manifest["stages"] if entry["stage"] == "external_discovery"
                   and entry.get("target") == package and "discovery" in entry.get("details", {})]
    jobs = [job for entry in discoveries for job in entry["details"]["discovery"]["per_job_stats"]]
    candidates = [raw for raw in _raw_records(c.safe_path(source, "raw/external/discovery.jsonl"))
                  if raw.payload.get("search_target_package_id") == package]
    raw_ids = {raw.raw_record_id for raw in candidates}
    documents = [raw for raw in _raw_records(c.safe_path(source, "raw/external/documents.jsonl"))
                 if raw.package_id == package or raw.payload.get("candidate_raw_record_id") in raw_ids
                 or raw.source_url in {candidate.payload["candidate_url"] for candidate in candidates}]
    from reposeer.collectors.external.crawler.validation import validate_external_document_record
    for raw in documents:
        validate_external_document_record(raw)
    urls = {canonical_article_url(raw.source_url) for raw in documents}
    retry_urls = set()
    for entry in manifest["stages"]:
        if entry["stage"] == "external_crawl" and entry.get("target") == package:
            urls.update(canonical_article_url(group["url"]) for group in entry.get("details", {}).get("candidate_groups", []))
            retry_urls.update(canonical_article_url(group["url"]) for group in entry.get("details", {}).get("candidate_groups", [])
                              if group.get("errors") and all(error.get("retryable") for error in group["errors"]))
    v2 = c.read_json(source / "COLLECTION_INPUT.json")["collection"] == c.policy_options("distributed-v2") and any(
        entry["details"]["discovery"].get("max_records") == 150 and
        entry["details"]["discovery"].get("max_pages_per_job") == 3 and
        set(entry["details"].get("requested_sources", [])) == set(EXTERNAL_SOURCES) for entry in discoveries)
    status = {}
    seeds = {}
    for name in EXTERNAL_SOURCES:
        provider_jobs = [job for job in jobs if job["provider"] == name]
        value = discovery_status(provider_jobs) if v2 and provider_jobs else ProviderState.PENDING
        if len(urls) >= 20 or v2 and len({raw.raw_record_id for raw in candidates if raw.payload["discovery_provider"] == name}) >= 30:
            value = ProviderState.BUDGET
        elif value in TERMINAL_PROVIDER_STATES and any(job["candidate_ids"] for job in provider_jobs):
            own_urls = {raw.payload["candidate_url"] for raw in candidates if raw.payload["discovery_provider"] == name}
            failed_urls = {group["url"] for entry in manifest["stages"]
                           if entry["stage"] == "external_crawl" and entry.get("target") == package
                           for group in entry.get("details", {}).get("candidate_groups", []) if group.get("errors")}
            if own_urls & failed_urls:
                value = ProviderState.PENDING
        status[name] = value if value in TERMINAL_PROVIDER_STATES else ProviderState.PENDING
        if v2 and any(job["pages_fetched"] for job in provider_jobs) and value not in TERMINAL_PROVIDER_STATES:
            run_ids = {entry["run_ids"][0] for entry in discoveries}
            if len(run_ids) == 1:
                path = c.safe_path(source, f"metadata/collection_runs/discovery_state_{next(iter(run_ids))}.json")
                if path.exists():
                    seeds[name] = path
    projection_paths = [c.safe_path(source, f"metadata/collection_runs/discovery_state_{run_id}.json")
                        for run_id in {entry["run_ids"][0] for entry in discoveries}]
    return dict(status=status, candidates=candidates, documents=documents if keep_documents else [], document_urls={canonical_article_url(raw.source_url) for raw in documents}, attempted_urls=urls,
                seeds=seeds, retry_urls=retry_urls, projection_paths=[path for path in projection_paths if path.exists()])


def _online(path: Path, expected: dict) -> dict | None:
    if not path.exists():
        return None
    state = c.read_json(path)
    c.require(all(state.get(key) == expected[key] for key in (
        "schema_version", "policy_id", "worker_id", "sampling_run_id", "source_repository_id",
        "source_part_index", "package_id", "provider", "source_ready_sha256", "source_checksums_sha256")),
        "source_fingerprint_mismatch")
    c.require(type(state.get("schema_version")) is int, "invalid_provider_state")
    c.require(state.get("status") in set(ProviderState), "invalid_provider_state")
    c.require(type(state.get("attempt_count")) is int and state["attempt_count"] >= 0, "invalid_provider_state")
    for key in ("last_attempt_at", "last_success_at", "retry_after"):
        if state.get(key) is not None:
            c.timestamp(state[key])
    return state


def write_state(path: Path, expected: dict, status: str, previous: dict | None, *, receipt=None, retry_after=None, success_at=None) -> dict:
    c.require((c.read_json(path) if path.exists() else None) == previous, "concurrent_provider_state_change")
    state = dict(**expected, status=status, attempt_count=(previous or {}).get("attempt_count", 0) + int(status == ProviderState.RUNNING),
                 last_attempt_at=c.format_utc_timestamp(c.now()) if status == ProviderState.RUNNING else (previous or {}).get("last_attempt_at"),
                 last_success_at=success_at if status in TERMINAL_PROVIDER_STATES else (previous or {}).get("last_success_at"),
                 retry_after=retry_after, receipt_relative_path=receipt)
    c.write_json(path, state, immutable=False)
    return state


def package_plan(source: Path, facts: dict, scope, local_base: Path | None) -> dict:
    key = scope.pypi.package_id
    package_sha = c.digest(key.encode())
    root = supplement_root(source)
    initial = initial_evidence(source, key, keep_documents=local_base is None)
    providers = {}
    for name in EXTERNAL_SOURCES:
        options = dict(OPTIONS)
        count = len({raw.raw_record_id for raw in initial["candidates"] if raw.payload.get("discovery_provider") == name})
        if name not in initial["seeds"] and 0 < count < 30:
            options["discovery_max_records"] = (30 - count) * 5
        expected = provider_input(facts, scope, name, options)
        destination = c.safe_path(root, f"packages/{package_sha}/{name}")
        local = c.safe_path(local_base, f"{facts['worker_id']}/{facts['sampling_run_id']}/{facts['source_repository_id']}/{source.name}/{package_sha}/{name}") if local_base is not None else None
        state_path = c.safe_path(root, f"state/{package_sha}/{name}.json")
        state = _online(state_path, expected)
        if local is not None:
            c.require((local / "ENRICHMENT_INPUT.json").exists() or not c.files(destination) or (destination / "READY.json").exists(),
                      "mounted_partial_enrichment_requires_local_workspace")
        local_receipt = verify_receipt(local, expected) if local is not None and (local / "READY.json").exists() else None
        receipt = verify_receipt(destination, expected) if (destination / "READY.json").exists() else None
        status = receipt["collection_status"] if receipt else initial["status"][name]
        if not receipt and status not in TERMINAL_PROVIDER_STATES and state and state["status"] not in TERMINAL_PROVIDER_STATES:
            status = state["status"]
        providers[name] = dict(status=status, expected=expected, destination=destination, local=local,
                               state_path=state_path, online=state, receipt=receipt, local_receipt=local_receipt)
    used = set(initial["attempted_urls"])
    for unit in providers.values():
        receipt = unit["receipt"] or unit["local_receipt"]
        if receipt:
            used.update(receipt["report"]["attempted_urls"])
    c.require(len(used) <= 20, "package_candidate_budget_exceeded")
    return dict(source=source, facts=facts, scope=scope, package_id=key, root=root,
                initial=initial, providers=providers, budget_used=used)


def resolve_verified_external_inputs(source: Path, policy_id: str = POLICY,
                                     package_id: str | None = None, cutoff: str | None = None) -> dict:
    """Read sealed observations and coverage without rewriting or collapsing provenance."""
    c.require(policy_id == POLICY, "invalid_external_policy")
    source = c.checked_mount_path(source.absolute())
    worker = c.worker_id(source.parents[2].name, "crawler")
    drive_root = source.parents[4]
    facts, scopes = _worker()._enrichment_source(source, drive_root, worker)
    if package_id is not None:
        c.subject(package_id)
        c.require(any(scope.pypi.package_id == package_id for scope in scopes), "package_not_in_source")
    paths, records, references, coverage = set(), {}, [], {}
    cutoff_at = c.timestamp(cutoff) if cutoff is not None else None
    for scope in scopes:
        if package_id is not None and scope.pypi.package_id != package_id:
            continue
        plan = package_plan(source, facts, scope, None)
        key = scope.pypi.package_id
        coverage[key] = {name: unit["status"] for name, unit in plan["providers"].items()}
        inputs = [(source / "raw/external/documents.jsonl", plan["initial"]["documents"], None)]
        for name, unit in plan["providers"].items():
            if unit["receipt"]:
                path = unit["destination"] / "raw/external/documents.jsonl"
                inputs.append((path, JsonlStorage().read_contract_records(path), unit["receipt"]))
                references.append(dict(package_id=key, provider=name, receipt=str(unit["destination"] / "READY.json"),
                    source_ready_sha256=facts["source_ready_sha256"],
                    document_associations=unit["receipt"]["report"].get("document_associations", {})))
        for path, observations, _ in inputs:
            if path.exists():
                paths.add(path)
            for raw in observations:
                if cutoff_at is not None and (c.timestamp(raw.retrieved_at) > cutoff_at or
                        raw.available_at is not None and c.timestamp(raw.available_at) > cutoff_at):
                    continue
                previous = records.get(raw.raw_record_id)
                c.require(previous is None or previous == raw, "conflicting_raw_record_id")
                records[raw.raw_record_id] = raw
    canonical_index, native_index = {}, {}
    for raw in records.values():
        canonical_index.setdefault(canonical_article_url(raw.payload.get("canonical_url") or raw.source_url), []).append(raw.raw_record_id)
        for ref in raw.payload.get("discovery_references", []):
            if ref.get("discovery_source_item_id"):
                key = ref["discovery_provider"] + ":" + ref["discovery_source_item_id"]
                native_index.setdefault(key, set()).add(raw.raw_record_id)
    return dict(document_paths=sorted(paths), records=list(records.values()), provenance=references,
                canonical_url_index=canonical_index, native_identity_index={key: sorted(ids) for key, ids in native_index.items()},
                coverage=coverage, targets=[VerifiedTargetContext.from_scope(scope) for scope in scopes if package_id is None or scope.pypi.package_id == package_id],
                historical_validity="observation_only", source_fingerprints=facts)


def schedule(parts: list[Path], drive_root: Path, worker: str, *, data_root=None, dry_run=False,
             limit: int | None = None, preflight=None, checker=None, pipeline_factory=None, deferred=None) -> dict:
    """The sole planner for manual and automatic enrichment. Provider quota is invocation-scoped."""
    from reposeer.collection.provider_preflight import check_providers
    w = _worker()
    base = c.local_drive_workspace("supplements", data_root, drive_root)
    plans, errors = [], []
    for source in parts:
        try:
            facts, scopes = w._enrichment_source(source, drive_root, worker)
            plans.extend(package_plan(source, facts, scope, base) for scope in scopes)
        except (OSError, ValueError, TypeError, KeyError, CollectionError, StorageError) as error:
            errors.append(dict(source=source.name, error_code=str(error) if isinstance(error, c.CoordinationError) else type(error).__name__))
    pending = [plan for plan in plans if any(unit["status"] not in TERMINAL_PROVIDER_STATES for unit in plan["providers"].values())]
    pending.sort(key=lambda plan: (any(unit["receipt"] for unit in plan["providers"].values()),
        -sum(unit["status"] not in TERMINAL_PROVIDER_STATES for unit in plan["providers"].values()), plan["package_id"]))
    selected = pending if limit is None else pending[:limit]
    summary = dict(status="planned" if dry_run else "completed", worker_id=worker, policy_id=POLICY,
        packages_planned=len(selected), packages_processed=0, pending_packages=len(pending),
        providers_completed=0, providers_budget_completed=0, providers_deferred=0, providers_failed=0,
        plan=[dict(package_id=plan["package_id"], sampling_run_id=plan["facts"]["sampling_run_id"],
                   source_part=plan["source"].name, providers={name: unit["status"] for name, unit in plan["providers"].items()}) for plan in selected],
        documents_extracted=0, relevant_documents=0, irrelevant_documents=0, unresolved_documents=0,
        source_parts=len(parts), completed=0, no_candidates=0, skipped_existing=len(plans) - len(pending),
        partial=0, failed=len(errors), failures=errors, providers={})
    if dry_run or not selected:
        summary["providers_planned"] = sum(unit["status"] not in TERMINAL_PROVIDER_STATES for plan in selected for unit in plan["providers"].values())
        if errors:
            summary["status"] = "failed"
        return summary
    def needs_network(unit, plan):
        if len(plan["budget_used"]) >= 20:
            return False
        if (unit["local"] / "READY.json").exists():
            return False
        checkpoint = unit["local"] / "metadata/enrichment_state.json"
        if checkpoint.exists():
            state = c.read_json(checkpoint)
            if state.get("phase") == "complete":
                return False
            if state.get("discovery_status") in TERMINAL_PROVIDER_STATES:
                known = set(plan["initial"]["document_urls"])
                for other in plan["providers"].values():
                    receipt_root = other["destination"] if other["receipt"] else other["local"] if other["local_receipt"] else None
                    if receipt_root is not None:
                        known.update(canonical_article_url(raw.source_url) for raw in JsonlStorage().read_contract_records(receipt_root / "raw/external/documents.jsonl"))
                return any(canonical_article_url(raw.payload["candidate_url"]) not in known
                           for raw in JsonlStorage().read_contract_records(unit["local"] / "raw/external/discovery.jsonl"))
        return True

    needed = sorted({name for plan in selected for name, unit in plan["providers"].items()
                     if unit["status"] not in TERMINAL_PROVIDER_STATES | {ProviderState.INSPECT}
                     and name not in (deferred or {}) and needs_network(unit, plan)})
    if needed:
        preflight = preflight or (checker or check_providers)(include_github=False, sources=needed)
        summary["preflight"] = preflight
        if not preflight.get("configuration_valid", preflight.get("ready", False)):
            summary.update(status="configuration_incomplete", failed=summary["failed"] + 1)
            return summary
    available = set(EXTERNAL_SOURCES) if not needed else set((preflight or {}).get("available_external_sources", EXTERNAL_SOURCES if (preflight or {}).get("ready") else []))
    availability = (preflight or {}).get("provider_status", {})
    blocked = {**(deferred or {}), **{name: availability_state(availability.get(name, {}).get("status", "provider_unreachable")) for name in needed if name not in available}}
    with ExitStack() as locks:
        for root in sorted({plan["root"] for plan in selected}):
            locks.enter_context(writer_lock(c.safe_path(root, "state/writer.lock")))
        for plan in selected:
            try:
                _execute_package(plan, summary, available, blocked, availability, pipeline_factory)
            except (OSError, ValueError, TypeError, KeyError, CollectionError, StorageError) as error:
                summary["failed"] += 1
                summary["failures"].append(dict(package_id=plan["package_id"], error_code=str(error) if isinstance(error, c.CoordinationError) else type(error).__name__))
            finally:
                plan["initial"]["documents"] = []
    summary["pending_packages"] = len(pending) - summary["completed"] - summary["no_candidates"]
    all_states = [unit["status"] for plan in plans for unit in plan["providers"].values()]
    summary["overall_external_status"] = ("partial" if any(value not in TERMINAL_PROVIDER_STATES for value in all_states)
        else "budget_completed" if ProviderState.BUDGET in all_states else "complete")
    summary["status"] = "failed" if summary["failed"] else "partial" if summary["partial"] else "completed"
    return summary


def _execute_package(plan, summary, available, blocked, availability, pipeline_factory):
    w, storage = _worker(), JsonlStorage()
    scope = plan["scope"]
    plan["initial"] = initial_evidence(plan["source"], plan["package_id"])
    documents = {raw.raw_record_id: raw for raw in plan["initial"]["documents"]}
    used = set(plan["initial"]["attempted_urls"])
    candidates = list(plan["initial"]["candidates"])
    pipelines, states, projection_paths = {}, {}, list(plan["initial"]["projection_paths"])
    for unit in plan["providers"].values():
        if unit["receipt"]:
            report = unit["receipt"]["report"]
            used.update(report["attempted_urls"])
            for raw in storage.read_contract_records(unit["destination"] / "raw/external/documents.jsonl"):
                previous = documents.get(raw.raw_record_id)
                c.require(previous is None or previous == raw, "conflicting_raw_record_id")
                documents[raw.raw_record_id] = raw
            candidates.extend(storage.read_contract_records(unit["destination"] / "raw/external/discovery.jsonl"))
        if (unit["local"] / "metadata/enrichment_state.json").exists():
            saved = c.read_json(unit["local"] / "metadata/enrichment_state.json")
            attempted = set()
            for manifest_name in saved.get("crawl_runs", []):
                manifest_path = c.safe_path(unit["local"], "metadata/collection_runs/" + Path(manifest_name).name)
                manifest = c.read_json(manifest_path)
                attempted.update(group["url"] for group in manifest["candidate_groups"])
            c.require(attempted == set(saved.get("attempted_urls", [])), "capture_checkpoint_mismatch")
            used.update(attempted)
            for raw in storage.read_contract_records(unit["local"] / "raw/external/documents.jsonl"):
                previous = documents.get(raw.raw_record_id)
                c.require(previous is None or previous == raw, "conflicting_raw_record_id")
                documents[raw.raw_record_id] = raw
    c.require(len(used) <= 20, "package_candidate_budget_exceeded")
    for name, unit in plan["providers"].items():
        if unit["status"] in TERMINAL_PROVIDER_STATES or unit["status"] == ProviderState.INSPECT:
            continue
        if (unit["local"] / "READY.json").exists():
            verify_receipt(unit["local"], unit["expected"])
            continue
        if name in blocked and len(used) < 20:
            unit["status"] = blocked[name]
            unit["online"] = write_state(unit["state_path"], unit["expected"], unit["status"], unit["online"],
                                         retry_after=availability.get(name, {}).get("retry_after"))
            continue
        c.require(name in available or len(used) >= 20, "provider_not_available")
        input_path = c.safe_path(unit["local"], "ENRICHMENT_INPUT.json")
        c.require(input_path.exists() or not c.files(unit["local"]), "untracked_enrichment_requires_inspection")
        if input_path.exists():
            saved = c.read_json(input_path)
            c.require(all(saved.get(key) == value for key, value in unit["expected"].items()), "source_fingerprint_mismatch")
        else:
            c.write_json(input_path, dict(**unit["expected"], created_at_utc=c.format_utc_timestamp(c.now())))
        unit["online"] = write_state(unit["state_path"], unit["expected"], ProviderState.RUNNING, unit["online"])
        pipeline = (pipeline_factory or ExternalEnrichmentPipeline)(StoragePaths(unit["local"]))
        pipelines[name] = pipeline
        try:
            if len(used) >= 20:
                state = pipeline.prepare(scope, unit["expected"]["external_collection_options"])
                state.update(jobs=state.get("jobs", []), discovery_status=ProviderState.BUDGET)
                storage.append_contract_records_durable(pipeline.paths.raw_path("external", "discovery"), [])
                pipeline.save(state)
                records = storage.read_contract_records(pipeline.paths.raw_dir / "external/discovery.jsonl")
            else:
                state, records = pipeline.discover(scope, name, unit["expected"]["external_collection_options"], seed=plan["initial"]["seeds"].get(name))
            states[name] = state
            candidates.extend(records)
            if state.get("projection_path"):
                projection_paths.append(Path(state["projection_path"]))
            unit["status"] = state["discovery_status"]
            if unit["status"] in {ProviderState.QUOTA, ProviderState.BLOCKED, ProviderState.RETRY}:
                blocked[name] = unit["status"]
        except (OSError, ValueError, TypeError, KeyError, CollectionError, StorageError) as error:
            unit["status"] = ProviderState.INSPECT
            pipelines.pop(name, None)
            unit["online"] = write_state(unit["state_path"], unit["expected"], unit["status"], unit["online"])
            summary["failures"].append(dict(package_id=plan["package_id"], provider=name,
                                            error_code="requires_inspection", error_type=type(error).__name__))
    groups, _, _ = _group_candidates(candidates)
    known_by_url, native_records = {}, {}
    for raw in documents.values():
        for url in {raw.source_url, raw.payload.get("fetched_url"), raw.payload.get("canonical_url")} - {None}:
            known_by_url.setdefault(canonical_article_url(url), []).append(raw)
        for ref in raw.payload.get("discovery_references", []):
            if ref.get("discovery_source_item_id"):
                native_records.setdefault((ref["discovery_provider"], ref["discovery_source_item_id"]), []).append(raw)
    for candidate in candidates:
        key = (candidate.payload["discovery_provider"], candidate.source_record_id)
        if key in native_records:
            known_by_url.setdefault(canonical_article_url(candidate.payload["candidate_url"]), []).extend(native_records[key])
    remaining = max(0, 20 - len(used))
    selected = _select_candidate_groups([group for group in groups if canonical_article_url(group.primary.payload["candidate_url"]) not in used and canonical_article_url(group.primary.payload["candidate_url"]) not in known_by_url])[:remaining]
    retry_urls = {url for state in states.values() for url in state.get("retry_urls", [])} | plan["initial"]["retry_urls"]
    selected_urls = {canonical_article_url(group.primary.payload["candidate_url"]) for group in selected}
    selected_urls.update(canonical_article_url(url) for url in retry_urls
                         if canonical_article_url(url) in used and canonical_article_url(url) not in known_by_url)
    attempted_this_call = set()
    for name, unit in plan["providers"].items():
        if unit["receipt"] or unit["status"] in TERMINAL_PROVIDER_STATES and name not in pipelines:
            if unit["online"] is None or unit["online"]["status"] != unit["status"]:
                unit["online"] = write_state(unit["state_path"], unit["expected"], unit["status"], unit["online"],
                    receipt=(str((unit["destination"] / "READY.json").relative_to(plan["source"].parents[4]).as_posix())
                             if unit["receipt"] else plan["facts"]["source_submission_relative_path"] + "/READY.json"),
                    success_at=(unit["receipt"] or c.read_json(plan["source"] / "READY.json"))["completed_at_utc"])
            continue
        if name in pipelines:
            own_path = unit["local"] / "raw/external/discovery.jsonl"
            own = storage.read_contract_records(own_path)
            own_ids = {raw.raw_record_id for raw in own}
            for raw in plan["initial"]["candidates"]:
                if raw.payload["discovery_provider"] == name and raw.raw_record_id not in own_ids:
                    storage.append_contract_record(own_path, raw)
                    own.append(raw)
                    own_ids.add(raw.raw_record_id)
            own_urls = {canonical_article_url(raw.payload["candidate_url"]) for raw in own}
            fetch = []
            for url in sorted(selected_urls & own_urls):
                if url not in known_by_url and url not in attempted_this_call:
                    fetch.extend(raw for raw in candidates if canonical_article_url(raw.payload["candidate_url"]) == url)
                    # Reserve this URL for this owner. Other providers reuse the validated returned observation.
            reused = [raw for url in own_urls for raw in known_by_url.get(url, [])]
            deferred_urls = (own_urls & attempted_this_call) - set(known_by_url)
            from reposeer.collectors.external.crawler.collector import (
                candidate_data,
                native_endpoint,
            )

            native_deferred = {canonical_article_url(raw.payload["candidate_url"]) for raw in fetch
                               if blocked.get(native_endpoint(candidate_data(raw))[1]) in {ProviderState.QUOTA, ProviderState.BLOCKED}}
            deferred_urls.update(native_deferred)
            fetch = [raw for raw in fetch if canonical_article_url(raw.payload["candidate_url"]) not in native_deferred]
            attempted_this_call.update(canonical_article_url(raw.payload["candidate_url"]) for raw in fetch)
            try:
                report = pipelines[name].capture(scope, name, unit["expected"]["external_collection_options"], fetch, reused,
                    projection_paths=projection_paths, deferred_urls=sorted(deferred_urls), document_associations={raw.raw_record_id: [candidate.raw_record_id for candidate in own
                        if raw in known_by_url.get(canonical_article_url(candidate.payload["candidate_url"]), [])] for raw in reused}, selection_limited=bool(own_urls - set(known_by_url) - selected_urls) and len(used | selected_urls) >= 20)
                for raw in storage.read_contract_records(unit["local"] / "raw/external/documents.jsonl"):
                    documents[raw.raw_record_id] = raw
                    known_by_url.setdefault(canonical_article_url(raw.source_url), []).append(raw)
                unit["status"] = report["status"]
                if unit["status"] in {ProviderState.QUOTA, ProviderState.RETRY, ProviderState.BLOCKED}:
                    blocked[name] = unit["status"]
                summary["providers"].setdefault(name, []).append(report["providers"].get(name, {}))
                refreshed, _ = w._enrichment_source(plan["source"], plan["source"].parents[4], plan["facts"]["worker_id"])
                c.require(refreshed == plan["facts"], "source_fingerprint_mismatch")
                if report["status"] in TERMINAL_PROVIDER_STATES:
                    seal(unit["local"], unit["expected"], report)
            except (OSError, ValueError, TypeError, KeyError, CollectionError, StorageError) as error:
                unit["status"] = ProviderState.INSPECT
                summary["failures"].append(dict(package_id=plan["package_id"], provider=name,
                    error_code="requires_inspection", error_type=type(error).__name__))
        if (unit["local"] / "READY.json").exists():
            receipt = w.publish_drive_part(unit["local"], unit["destination"], unit["expected"], verifier=verify_receipt)
            unit["status"] = receipt["collection_status"]
            unit["receipt"] = receipt
        if unit["status"] not in TERMINAL_PROVIDER_STATES and unit["status"] != ProviderState.INSPECT and name not in pipelines:
            continue
        unit["online"] = write_state(unit["state_path"], unit["expected"], unit["status"], unit["online"],
            receipt=str((unit["destination"] / "READY.json").relative_to(plan["source"].parents[4]).as_posix()) if unit["receipt"] else None,
            retry_after=availability.get(name, {}).get("retry_after"),
            success_at=unit["receipt"]["completed_at_utc"] if unit["receipt"] else None)
    summary["documents_extracted"] += sum(raw.payload["content_status"] == "full" for raw in documents.values())
    if documents:
        normalized = NormalizationPipeline().process(list(documents.values()), targets=[VerifiedTargetContext.from_scope(scope)])
        for decision, count in normalized.processing_summary.relevance_decisions.items():
            summary[decision + "_documents"] += count
    statuses = [unit["status"] for unit in plan["providers"].values()]
    summary["packages_processed"] += 1
    summary["providers_completed"] += sum(value in {ProviderState.COMPLETE, ProviderState.EMPTY} for value in statuses)
    summary["providers_budget_completed"] += statuses.count(ProviderState.BUDGET)
    summary["providers_deferred"] += sum(value in {ProviderState.QUOTA, ProviderState.RETRY} for value in statuses)
    summary["providers_failed"] += sum(value in {ProviderState.BLOCKED, ProviderState.INSPECT} for value in statuses)
    if all(value in TERMINAL_PROVIDER_STATES for value in statuses):
        summary["no_candidates" if all(value == ProviderState.EMPTY for value in statuses) else "completed"] += 1
    else:
        summary["partial"] += 1
        if any(value in {ProviderState.BLOCKED, ProviderState.INSPECT} for value in statuses):
            summary["failed"] += 1


def external_report(drive_root: Path) -> dict:
    """Single-writer derived CSV. Mutable state never proves provider completion."""
    root = c.checked_mount_path(drive_root)
    _, mapping = c.read_cumulative_mapping(root)
    fields = ("sampling_run_id", "worker_id", "package_id", "repo_full_name", "source_part", "policy_id",
              *(name + "_status" for name in EXTERNAL_SOURCES), "overall_external_status",
              "completed_provider_count", "pending_provider_count", "last_attempt_at", "last_success_at")
    rows = []
    with writer_lock(c.safe_path(root, "screening/external_coverage.lock")):
        for worker in c.DRIVE_WORKERS:
            submissions = c.safe_path(root, "submissions/" + worker)
            if not submissions.exists():
                continue
            parts, errors = scan_parts(submissions)
            c.require(not errors, "invalid_external_report_source")
            for source in parts:
                data = resolve_verified_external_inputs(source)
                facts = data["source_fingerprints"]
                for key, statuses in data["coverage"].items():
                    c.require(key in mapping, "external_package_not_in_master")
                    complete = sum(value in TERMINAL_PROVIDER_STATES for value in statuses.values())
                    attempts, successes = [], []
                    for name in EXTERNAL_SOURCES:
                        path = c.safe_path(supplement_root(source), f"state/{c.digest(key.encode())}/{name}.json")
                        if path.exists():
                            state = c.read_json(path)
                            attempts.extend([state["last_attempt_at"]] if state.get("last_attempt_at") else [])
                            successes.extend([state["last_success_at"]] if state.get("last_success_at") else [])
                    rows.append(dict(sampling_run_id=facts["sampling_run_id"], worker_id=worker, package_id=key,
                        repo_full_name=c.read_json(source / "COLLECTION_INPUT.json")["repo_full_name"],
                        source_part=source.name, policy_id=POLICY, **{name + "_status": statuses[name] for name in EXTERNAL_SOURCES},
                        overall_external_status=("budget_completed" if ProviderState.BUDGET in statuses.values() else "complete") if complete == 5 else "partial", completed_provider_count=complete,
                        pending_provider_count=5-complete, last_attempt_at=max(attempts, default=""), last_success_at=max(successes, default="")))
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (row["sampling_run_id"], row["worker_id"], row["source_part"], row["package_id"])))
        c.atomic_bytes(c.safe_path(root, "screening/external_coverage.csv"), stream.getvalue().encode(), immutable=False)
    return dict(status="completed", rows=len(rows), output=c.mounted_display_path(root / "screening/external_coverage.csv"))
