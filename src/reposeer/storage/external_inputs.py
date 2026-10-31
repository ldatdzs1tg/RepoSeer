"""Read verified external source/supplement observations without coordination or writes."""

import re
from pathlib import Path

from reposeer.collection.provider_preflight import (
    EXTERNAL_SOURCES,
    TERMINAL_PROVIDER_STATES,
    ProviderState,
)
from reposeer.contracts import RawRecord, validate_record
from reposeer.contracts.identity import canonical_article_url
from reposeer.pipelines.external_enrichment import discovery_status
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.storage import artifact_integrity as c
from reposeer.storage.raw.jsonl import JsonlStorage

POLICY = "external-v2"
OPTIONS = {key: value for key, value in c.policy_options("distributed-v2").items()
           if key not in {"max_pages", "max_records"}}


def source_facts(source: Path):
    from reposeer.pipelines.external_enrichment import source_scopes

    source = c.checked_mount_path(source.absolute())
    c.require(source.parents[3].name == "submissions", "invalid_source_submission_path")
    drive_root = source.parents[4]
    worker = source.parents[2].name
    c.require(re.fullmatch(r"C[1-7]", worker), "invalid_worker")
    expected = c.read_json(c.safe_path(source, "COLLECTION_INPUT.json"))
    run_id = c.uid(source.parent.parent.name)
    repository_id = source.parent.name
    c.require(re.fullmatch(r"[a-f0-9]{64}", repository_id), "invalid_source_repository_id")
    c.require(re.fullmatch(r"part_[0-9]{3,}", source.name), "invalid_source_part")
    index = int(source.name[5:])
    c.require(type(expected.get("schema_version")) is int and expected["schema_version"] == 1
            and expected["worker_id"] == worker and expected["sampling_run_id"] == run_id
            and expected["repository_id"] == repository_id and type(expected["part_index"]) is int
            and expected["part_index"] == index and index > 0
            and c.digest(expected["repo_full_name"].encode("utf-8")) == repository_id,
            "source_directory_identity_mismatch")
    c.require(expected["package_ids"] == sorted(set(expected["package_ids"]))
            and 1 <= len(expected["package_ids"]) <= 100, "invalid_source_package_ids")
    c.require(expected["collection"] in [c.policy_options(name) for name in ("legacy-initial", "distributed-v2")],
            "invalid_collection_policy")
    marker = c.verify_drive_part(source, expected)
    manifest = c.read_json(c.safe_path(source, f"metadata/collection_runs/integration_{marker['integration_run_id']}.json"))
    scopes = source_scopes(source, manifest)
    identities = [scope.resolve() for scope in scopes]
    c.require(sorted(item["package_id"] for item in identities) == expected["package_ids"],
            "source_identity_mismatch")
    enrichment = dict(schema_version=1, policy_id="external-v2", worker_id=worker,
        sampling_run_id=run_id, source_repository_id=repository_id, source_part_index=index,
        source_submission_relative_path=source.relative_to(drive_root).as_posix(),
        source_integration_run_id=marker["integration_run_id"],
        source_ready_sha256=c.fingerprint(source / "READY.json")[0],
        source_checksums_sha256=c.fingerprint(source / "CHECKSUMS.sha256")[0],
        package_ids=expected["package_ids"], verified_repo_id=identities[0]["repo_id"],
        verified_mapping_evidence_refs=sorted({entry["raw_record_id"] for item in identities
                                              for entry in item["evidence"]}),
        external_collection_options=OPTIONS)
    return enrichment, scopes


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
        if v2 and len({raw.raw_record_id for raw in candidates if raw.payload["discovery_provider"] == name}) >= 30:
            value = ProviderState.BUDGET
        if value in TERMINAL_PROVIDER_STATES and any(job["candidate_ids"] for job in provider_jobs):
            own_urls = {raw.payload["candidate_url"] for raw in candidates if raw.payload["discovery_provider"] == name}
            failed_urls = {group["url"] for entry in manifest["stages"]
                           if entry["stage"] == "external_crawl" and entry.get("target") == package
                           for group in entry.get("details", {}).get("candidate_groups", []) if group.get("errors")}
            missing_urls = {canonical_article_url(url) for url in own_urls} - urls
            if own_urls & failed_urls:
                value = ProviderState.RETRY
            elif missing_urls and value != ProviderState.BUDGET:
                value = ProviderState.PENDING
        if value == ProviderState.BLOCKED and all(job.get("missing_reason") == "preflight_deferred" for job in provider_jobs):
            value = ProviderState.PENDING
        status[name] = value
        if v2 and provider_jobs and value not in TERMINAL_PROVIDER_STATES:
            run_ids = {entry["run_ids"][0] for entry in discoveries}
            if len(run_ids) == 1:
                path = c.safe_path(source, f"metadata/collection_runs/discovery_state_{next(iter(run_ids))}.json")
                if path.exists():
                    seeds[name] = path
    projection_paths = [c.safe_path(source, f"metadata/collection_runs/discovery_state_{run_id}.json")
                        for run_id in {entry["run_ids"][0] for entry in discoveries}]
    return dict(status=status, candidates=candidates, documents=documents if keep_documents else [], document_urls={canonical_article_url(raw.source_url) for raw in documents}, attempted_urls=urls,
                seeds=seeds, jobs=jobs, retry_urls=retry_urls, projection_paths=[path for path in projection_paths if path.exists()])


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


def package_evidence(source: Path, facts: dict, scope, *, keep_documents=False) -> dict:
    key = scope.pypi.package_id
    package_sha = c.digest(key.encode())
    root = supplement_root(source)
    initial = initial_evidence(source, key, keep_documents=keep_documents)
    providers = {}
    used = set(initial["attempted_urls"])
    for name in EXTERNAL_SOURCES:
        options = dict(OPTIONS)
        count = len({raw.raw_record_id for raw in initial["candidates"] if raw.payload.get("discovery_provider") == name})
        if name not in initial["seeds"] and 0 < count < 30:
            options["discovery_max_records"] = (30 - count) * 5
        expected = provider_input(facts, scope, name, options)
        destination = c.safe_path(root, f"packages/{package_sha}/{name}")
        state_path = c.safe_path(root, f"state/{package_sha}/{name}.json")
        state = _online(state_path, expected)
        receipt = verify_receipt(destination, expected) if (destination / "READY.json").exists() else None
        status = receipt["collection_status"] if receipt else initial["status"][name]
        if not receipt and status not in TERMINAL_PROVIDER_STATES and state and state["status"] not in TERMINAL_PROVIDER_STATES:
            status = state["status"]
        providers[name] = dict(status=status, expected=expected, destination=destination,
            state_path=state_path, online=state, receipt=receipt)
        if receipt:
            used.update(canonical_article_url(url) for url in receipt["report"]["attempted_urls"])
    c.require(len(used) <= 20, "package_candidate_budget_exceeded")
    return dict(source=source, facts=facts, scope=scope, package_id=key, root=root,
                initial=initial, providers=providers, budget_used=used,
                candidate_budget_exhausted=len(used) == 20)


def resolve_verified_external_inputs(source: Path, policy_id: str = POLICY,
                                     package_id: str | None = None, cutoff: str | None = None) -> dict:
    """Read sealed observations and coverage without rewriting or collapsing provenance."""
    c.require(policy_id == POLICY, "invalid_external_policy")
    source = c.checked_mount_path(source.absolute())
    facts, scopes = source_facts(source)
    if package_id is not None:
        c.subject(package_id)
        c.require(any(scope.pypi.package_id == package_id for scope in scopes), "package_not_in_source")
    paths, records, references, coverage, budgets = set(), {}, [], {}, {}
    cutoff_at = c.timestamp(cutoff) if cutoff is not None else None
    for scope in scopes:
        if package_id is not None and scope.pypi.package_id != package_id:
            continue
        plan = package_evidence(source, facts, scope, keep_documents=True)
        key = scope.pypi.package_id
        coverage[key] = {name: unit["status"] for name, unit in plan["providers"].items()}
        budgets[key] = plan["candidate_budget_exhausted"]
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
                coverage=coverage, candidate_budget_exhausted=budgets, targets=[VerifiedTargetContext.from_scope(scope) for scope in scopes if package_id is None or scope.pypi.package_id == package_id],
                historical_validity="observation_only", source_fingerprints=facts)

