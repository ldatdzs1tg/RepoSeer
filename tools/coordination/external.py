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
from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.contracts.identity import canonical_article_url
from reposeer.exceptions import CollectionError, StorageError
from reposeer.pipelines.external_crawl import _group_candidates, _select_candidate_groups
from reposeer.pipelines.external_enrichment import ExternalEnrichmentPipeline, discovery_status
from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.storage.external_inputs import (  # noqa: F401 - compatibility re-exports
    OPTIONS,
    POLICY,
    _online,
    initial_evidence,
    package_evidence,
    provider_input,
    resolve_verified_external_inputs,
    supplement_root,
    verify_receipt,
)
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage

AUTO_ENRICH_MAX_PACKAGES = 1


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


def write_state(path: Path, expected: dict, status: str, previous: dict | None, *, receipt=None, retry_after=None, success_at=None) -> dict:
    c.require((c.read_json(path) if path.exists() else None) == previous, "concurrent_provider_state_change")
    state = dict(**expected, status=status, attempt_count=(previous or {}).get("attempt_count", 0) + int(status == ProviderState.RUNNING),
                 last_attempt_at=c.format_utc_timestamp(c.now()) if status == ProviderState.RUNNING else (previous or {}).get("last_attempt_at"),
                 last_success_at=success_at if status in TERMINAL_PROVIDER_STATES else (previous or {}).get("last_success_at"),
                 retry_after=retry_after, receipt_relative_path=receipt)
    c.write_json(path, state, immutable=False)
    return state


def package_plan(source: Path, facts: dict, scope, local_base: Path | None) -> dict:
    plan = package_evidence(source, facts, scope)
    used = plan["budget_used"]
    for name, unit in plan["providers"].items():
        local = c.safe_path(local_base, f"{facts['worker_id']}/{facts['sampling_run_id']}/{facts['source_repository_id']}/{source.name}/{c.digest(plan['package_id'].encode())}/{name}")
        unit.update(local=local, local_receipt=None, checkpoint=None)
        c.require((local / "ENRICHMENT_INPUT.json").exists() or not c.files(unit["destination"]) or unit["receipt"],
                  "mounted_partial_enrichment_requires_local_workspace")
        if (local / "ENRICHMENT_INPUT.json").exists():
            saved = c.read_json(local / "ENRICHMENT_INPUT.json")
            c.require(all(saved.get(key) == value for key, value in unit["expected"].items()), "source_fingerprint_mismatch")
        if (local / "READY.json").exists():
            unit["local_receipt"] = verify_receipt(local, unit["expected"])
            used.update(canonical_article_url(url) for url in unit["local_receipt"]["report"]["attempted_urls"])
        checkpoint = local / "metadata/enrichment_state.json"
        if checkpoint.exists():
            state = c.read_json(checkpoint)
            c.require(state.get("identity") == VerifiedTargetContext.from_scope(scope).model_dump(mode="json")
                      and state.get("options") == unit["expected"]["external_collection_options"], "enrichment_checkpoint_conflict")
            unit["checkpoint"] = state
            attempted = set()
            for manifest_name in state.get("crawl_runs", []):
                manifest = c.read_json(c.safe_path(local, "metadata/collection_runs/" + Path(manifest_name).name))
                attempted.update(canonical_article_url(group["url"]) for group in manifest["candidate_groups"])
            c.require(attempted == {canonical_article_url(url) for url in state.get("attempted_urls", [])}, "capture_checkpoint_mismatch")
            used.update(attempted)
            if state.get("phase") in {"crawling", "processing", "requires_inspection"} and not unit["receipt"]:
                unit["status"] = ProviderState.INSPECT
    c.require(len(used) <= 20, "package_candidate_budget_exceeded")
    plan["candidate_budget_exhausted"] = len(used) == 20
    documents = _package_documents(plan)
    known = {canonical_article_url(url) for raw in documents.values()
             for url in (raw.source_url, raw.payload.get("canonical_url"), raw.payload.get("fetched_url")) if url}
    for name, unit in plan["providers"].items():
        own = [raw for raw in plan["initial"]["candidates"] if raw.payload["discovery_provider"] == name]
        own += JsonlStorage().read_contract_records(unit["local"] / "raw/external/discovery.jsonl")
        own_urls = {canonical_article_url(raw.payload["candidate_url"]) for raw in own}
        retries = set(plan["initial"]["retry_urls"]) | set((unit["checkpoint"] or {}).get("retry_urls", []))
        unit["retry_urls"] = {canonical_article_url(url) for url in retries} & own_urls & used - known
        phase = (unit["checkpoint"] or {}).get("phase")
        discovery = (unit["checkpoint"] or {}).get("discovery_status")
        unit["work"] = (None if unit["receipt"] else "publish" if unit["local_receipt"] or phase == "complete"
            else None if unit["status"] in TERMINAL_PROVIDER_STATES | {ProviderState.INSPECT}
            else "retry" if plan["candidate_budget_exhausted"] and unit["retry_urls"]
            else "finalize" if discovery in TERMINAL_PROVIDER_STATES and (not own_urls - known or discovery == ProviderState.BUDGET)
            else None if plan["candidate_budget_exhausted"] else "network")
    return plan


def _package_documents(plan) -> dict:
    documents = {}
    inputs = [initial_evidence(plan["source"], plan["package_id"])["documents"]]
    for unit in plan["providers"].values():
        roots = [unit["destination"]] if unit["receipt"] else []
        if unit.get("local") and (unit.get("local_receipt") or unit.get("checkpoint") and
                unit["checkpoint"].get("phase") not in {"crawling", "processing", "requires_inspection"}):
            roots.append(unit["local"])
        inputs.extend(JsonlStorage().read_contract_records(root / "raw/external/documents.jsonl") for root in roots)
    for records in inputs:
        for raw in records:
            validate_external_document_record(raw)
            previous = documents.get(raw.raw_record_id)
            c.require(previous is None or previous == raw, "conflicting_raw_record_id")
            documents[raw.raw_record_id] = raw
    return documents


def _document_metrics(summary, plans, existing_ids):
    records, full, relevance_seen = {}, set(), set()
    for plan in plans:
        documents = _package_documents(plan)
        for raw in documents.values():
            fingerprint = c.digest(c.canonical(raw.model_dump(mode="json")))
            previous = records.get(raw.raw_record_id)
            c.require(previous is None or previous == fingerprint, "conflicting_raw_record_id")
            records[raw.raw_record_id] = fingerprint
            if raw.payload["content_status"] == "full":
                full.add(raw.raw_record_id)
        fresh = [raw for key, raw in documents.items() if (key, plan["package_id"]) not in relevance_seen]
        relevance_seen.update((raw.raw_record_id, plan["package_id"]) for raw in fresh)
        if fresh:
            contexts = [VerifiedTargetContext.from_scope(plan["scope"])]
            for prefix, rows in (("", fresh), ("new_", [raw for raw in fresh if raw.raw_record_id not in existing_ids])):
                if rows:
                    normalized = NormalizationPipeline().process(rows, targets=contexts)
                    for decision, count in normalized.processing_summary.relevance_decisions.items():
                        key = prefix + decision + "_documents"
                        summary[key] = summary.get(key, 0) + count
    summary.update(documents_newly_extracted=len(full - existing_ids), documents_extracted=len(full - existing_ids),
        documents_reused=len(set(records) & existing_ids), documents_total_available=len(records), documents_total_extracted=len(full))


def schedule(parts: list[Path], drive_root: Path, worker: str, *, data_root=None, dry_run=False,
             limit: int | None = None, preflight=None, checker=None, pipeline_factory=None, deferred=None) -> dict:
    """Filter executable provider work before applying the automatic package limit."""
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
    summary = dict(status="planned" if dry_run else "completed", worker_id=worker, policy_id=POLICY,
        packages_planned=0, packages_processed=0, pending_packages=len(pending), packages_incomplete=len(pending),
        packages_actionable=0, packages_requiring_inspection=0, packages_budget_blocked=0,
        packages_temporarily_deferred=0, packages_availability_unverified=0,
        providers_completed=0, providers_budget_completed=0, providers_deferred=0, providers_failed=0,
        documents_extracted=0, documents_newly_extracted=0, documents_reused=0, documents_total_available=0,
        documents_total_extracted=0, relevant_documents=0, irrelevant_documents=0, unresolved_documents=0,
        new_relevant_documents=0, new_irrelevant_documents=0, new_unresolved_documents=0,
        source_parts=len(parts), completed=0, no_candidates=0, skipped_existing=len(plans)-len(pending),
        partial=0, failed=len(errors), failures=errors, providers={})
    potential = [plan for plan in plans if any(unit["work"] for unit in plan["providers"].values())]
    needed = sorted({name for plan in potential for name, unit in plan["providers"].items()
                     if unit["work"] in {"network", "retry"} and name not in (deferred or {})})
    blocked = dict(deferred or {})
    available, availability = set(), {}
    if needed and not dry_run:
        preflight = preflight or (checker or check_providers)(include_github=False, sources=needed)
        summary["preflight"] = preflight
        if not preflight.get("configuration_valid", preflight.get("ready", False)):
            summary.update(status="configuration_incomplete", failed=summary["failed"]+1, plan=[], providers_planned=0)
            return summary
        available = set(preflight.get("available_external_sources", EXTERNAL_SOURCES if preflight.get("ready") else []))
        availability = preflight.get("provider_status", {})
        blocked.update({name: availability_state(availability.get(name, {}).get("status", "provider_unreachable"))
                        for name in needed if name not in available})
    actionable = []
    for plan in pending:
        units = plan["providers"]
        plan["runnable"] = {name for name, unit in units.items() if unit["work"] in {"publish", "finalize"}
            or unit["work"] in {"network", "retry"} and name in available and name not in blocked}
        unverified = dry_run and any(unit["work"] in {"network", "retry"} for unit in units.values())
        if plan["runnable"]:
            actionable.append(plan)
        elif unverified:
            summary["packages_availability_unverified"] += 1
        elif any(unit["work"] for unit in units.values()):
            summary["packages_temporarily_deferred"] += 1
        elif all(unit["status"] in TERMINAL_PROVIDER_STATES | {ProviderState.INSPECT} for unit in units.values()):
            summary["packages_requiring_inspection"] += 1
        elif plan["candidate_budget_exhausted"]:
            summary["packages_budget_blocked"] += 1
    summary["packages_actionable"] = len(actionable)
    preview = potential if dry_run else actionable
    def priority(plan):
        runnable = [unit for name, unit in plan["providers"].items() if unit["work"] and (dry_run or name in plan["runnable"])]
        return (not any(unit["status"] == ProviderState.PENDING for unit in runnable),
            any(unit["receipt"] for unit in plan["providers"].values()), -len(runnable), plan["package_id"], str(plan["source"]))

    preview.sort(key=priority)
    selected = preview if limit is None else preview[:limit]
    summary.update(packages_planned=len(selected), providers_planned=sum(
        len(plan["runnable"]) if not dry_run else sum(bool(unit["work"]) for unit in plan["providers"].values()) for plan in selected),
        availability="availability_unverified" if dry_run and needed else "verified" if needed else "not_required",
        plan=[dict(package_id=plan["package_id"], sampling_run_id=plan["facts"]["sampling_run_id"], source_part=plan["source"].name,
            candidate_budget_exhausted=plan["candidate_budget_exhausted"],
            providers={name: unit["status"] for name, unit in plan["providers"].items()}) for plan in selected])
    if not dry_run:
        existing_ids = {key for plan in plans for key in _package_documents(plan)}
        affected = [plan for plan in pending if plan in selected or any(name in blocked and unit["work"] in {"network", "retry"}
                    for name, unit in plan["providers"].items())]
        with ExitStack() as locks:
            for root in sorted({plan["root"] for plan in affected}):
                locks.enter_context(writer_lock(c.safe_path(root, "state/writer.lock")))
            for plan in affected:
                for name, unit in plan["providers"].items():
                    if name in blocked and unit["work"] in {"network", "retry"}:
                        unit["status"] = blocked[name]
                        unit["online"] = write_state(unit["state_path"], unit["expected"], unit["status"], unit["online"],
                            retry_after=availability.get(name, {}).get("retry_after"))
            for plan in selected:
                try:
                    _execute_package(plan, summary, available, blocked, availability, pipeline_factory)
                except (OSError, ValueError, TypeError, KeyError, CollectionError, StorageError) as error:
                    summary["failed"] += 1
                    summary["failures"].append(dict(package_id=plan["package_id"], error_code=str(error) if isinstance(error, c.CoordinationError) else type(error).__name__))
                finally:
                    plan["initial"]["documents"] = []
        _document_metrics(summary, plans, existing_ids)
        summary["failed"] += sum(plan not in selected and any(unit["status"] in {ProviderState.INSPECT, ProviderState.BLOCKED}
            for unit in plan["providers"].values()) for plan in pending)
    summary["pending_packages"] = sum(any(unit["status"] not in TERMINAL_PROVIDER_STATES for unit in plan["providers"].values()) for plan in plans)
    summary["candidate_budget_exhausted"] = any(plan["candidate_budget_exhausted"] for plan in plans)
    summary["overall_external_status"] = ("candidate_budget_exhausted" if summary["pending_packages"] and summary["candidate_budget_exhausted"]
        else "partial" if summary["pending_packages"] else "budget_completed" if any(unit["status"] == ProviderState.BUDGET
            for plan in plans for unit in plan["providers"].values()) else "complete")
    if errors or summary["failed"]:
        summary["status"] = "failed"
    elif not dry_run:
        summary["status"] = "partial" if summary["pending_packages"] else "completed"
    return summary


def _execute_package(plan, summary, available, blocked, availability, pipeline_factory):
    w, storage = _worker(), JsonlStorage()
    scope = plan["scope"]
    plan["initial"] = initial_evidence(plan["source"], plan["package_id"])
    documents = _package_documents(plan)
    used = set(plan["budget_used"])
    candidates = list(plan["initial"]["candidates"])
    pipelines, states, projection_paths = {}, {}, list(plan["initial"]["projection_paths"])
    for unit in plan["providers"].values():
        if unit["receipt"]:
            candidates.extend(storage.read_contract_records(unit["destination"] / "raw/external/discovery.jsonl"))
    c.require(len(used) <= 20, "package_candidate_budget_exceeded")
    for name, unit in plan["providers"].items():
        if not unit["work"] or unit["work"] in {"network", "retry"} and name not in plan["runnable"]:
            continue
        if (unit["local"] / "READY.json").exists():
            verify_receipt(unit["local"], unit["expected"])
            continue
        if name in blocked and unit["work"] in {"network", "retry"}:
            unit["status"] = blocked[name]
            unit["online"] = write_state(unit["state_path"], unit["expected"], unit["status"], unit["online"],
                                         retry_after=availability.get(name, {}).get("retry_after"))
            continue
        c.require(name in available or unit["work"] in {"publish", "finalize"}, "provider_not_available")
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
            if len(used) >= 20 or unit["work"] in {"publish", "finalize"}:
                state = pipeline.prepare(scope, unit["expected"]["external_collection_options"])
                jobs = state.get("jobs", [job for job in plan["initial"]["jobs"] if job["provider"] == name])
                state.update(jobs=jobs, discovery_status=state.get("discovery_status", discovery_status(jobs)))
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
                                            error_code=str(error) if str(error) in {"discovery_checkpoint_scope_mismatch", "enrichment_checkpoint_conflict"}
                                            else "requires_inspection", error_type=type(error).__name__))
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
    active_urls = {canonical_article_url(raw.payload["candidate_url"]) for raw in candidates if raw.payload["discovery_provider"] in pipelines}
    selected = _select_candidate_groups([group for group in groups if canonical_article_url(group.primary.payload["candidate_url"]) in active_urls
        and canonical_article_url(group.primary.payload["candidate_url"]) not in used
        and canonical_article_url(group.primary.payload["candidate_url"]) not in known_by_url])[:remaining]
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
                unit["checkpoint"] = c.read_json(unit["local"] / "metadata/enrichment_state.json")
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
                    error_code=str(error) if str(error) in {"discovery_checkpoint_scope_mismatch", "enrichment_checkpoint_conflict"}
                                            else "requires_inspection", error_type=type(error).__name__))
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
    for unit in plan["providers"].values():
        report = unit["receipt"]["report"] if unit["receipt"] else unit.get("checkpoint") or {}
        used.update(canonical_article_url(url) for url in report.get("attempted_urls", []))
    c.require(len(used) <= 20, "package_candidate_budget_exceeded")
    plan.update(budget_used=used, candidate_budget_exhausted=len(used) == 20)
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
                        overall_external_status=(("budget_completed" if ProviderState.BUDGET in statuses.values() else "complete") if complete == 5
                            else "candidate_budget_exhausted" if data["candidate_budget_exhausted"][key] else "partial"), completed_provider_count=complete,
                        pending_provider_count=5-complete, last_attempt_at=max(attempts, default=""), last_success_at=max(successes, default="")))
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (row["sampling_run_id"], row["worker_id"], row["source_part"], row["package_id"])))
        c.atomic_bytes(c.safe_path(root, "screening/external_coverage.csv"), stream.getvalue().encode(), immutable=False)
    return dict(status="completed", rows=len(rows), output=c.mounted_display_path(root / "screening/external_coverage.csv"))
