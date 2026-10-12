"""T-014 raw activity persistence for one verified repository and group."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from reposeer.clients.github import GitHubClient
from reposeer.collection.context import CollectionContext
from reposeer.collection.persistence import write_json_atomic
from reposeer.collection.run_manager import RunManager
from reposeer.collectors.github import activity
from reposeer.collectors.github.activity import ActivityGroup, GitHubActivityContractCollector
from reposeer.contracts import (
    SCHEMA_VERSION,
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    GitHubRepository,
    RawRecord,
    validate_collection_pair,
    validate_record,
)
from reposeer.contracts.identity import parse_repo_id
from reposeer.exceptions import CollectionError, StorageError
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.raw.jsonl import JsonlStorage


class ActivityPersistenceError(CollectionError):
    """A valid staged page remains recoverable after a storage/checkpoint failure."""


ACTIVITY_GROUPS: tuple[ActivityGroup, ...] = (
    "commits", "issues", "pull_requests", "contributors", "releases",
)
_INITIAL_STATE: dict[str, str | int] = {
    "status": "pending", "cursor": "", "pages_fetched": 0, "records_emitted": 0,
    "records_stored": 0, "records_rejected": 0, "error_count": 0,
    "pagination_complete": 0, "last_error_code": "", "last_request_id": "",
    "missing_reason": "",
}
_CURSOR = re.compile(r"p:([1-9][0-9]*):i:(0|[1-9][0-9]?)")


def item_prefix(target_index: int, group: ActivityGroup) -> str:
    return f"activity:item:{target_index}:{group}:"


def initial_item_state(target_index: int, group: ActivityGroup) -> dict[str, str | int]:
    prefix = item_prefix(target_index, group)
    return {prefix + key: value for key, value in _INITIAL_STATE.items()}


def read_item_state(
    context: CollectionContext, target_index: int, group: ActivityGroup
) -> dict[str, Any]:
    prefix = item_prefix(target_index, group)
    state = {key: context.checkpoint_state.get(prefix + key) for key in _INITIAL_STATE}
    for key, initial in _INITIAL_STATE.items():
        if type(state[key]) is not type(initial):
            raise CollectionError("Invalid activity checkpoint fields")
        if type(initial) is int and state[key] < 0:
            raise CollectionError("Invalid activity checkpoint counters")
    if (
        state["status"] not in {
            "pending", "complete", "partial", "retryable_failure", "failed", "excluded",
        }
        or state["pagination_complete"] not in (0, 1)
        or (state["cursor"] and _CURSOR.fullmatch(state["cursor"]) is None)
        or state["records_stored"] > state["records_emitted"]
        or state["error_count"] < state["records_rejected"]
        or state["records_emitted"] + state["records_rejected"] > 100 * state["pages_fetched"]
        or (state["last_request_id"] and re.fullmatch(
            r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}",
            state["last_request_id"],
        ) is None)
        or (not state["pages_fetched"] and state["cursor"])
        or (state["pagination_complete"] and state["cursor"])
        or (state["status"] == "excluded" and any(
            state[key] for key in (
                "cursor", "pages_fetched", "records_emitted", "records_stored", "records_rejected",
                "error_count", "pagination_complete", "last_request_id", "last_error_code",
            )
        ))
        or (state["status"] in {"retryable_failure", "failed"} and not state["error_count"])
        or (state["status"] == "complete" and (not state["pages_fetched"] or state["missing_reason"]))
        or (state["status"] == "complete" and (
            not state["pagination_complete"] or state["error_count"] or state["records_rejected"]
        ))
    ):
        raise CollectionError("Inconsistent activity checkpoint state")
    return state


def _serialized(record: RawRecord) -> str:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))


def _digest(record: RawRecord) -> str:
    return hashlib.sha256(_serialized(record).encode("utf-8")).hexdigest()


def _logical_key(record: RawRecord) -> tuple[str, str | None, str] | None:
    if record.source_record_id is None:
        return None
    return record.run_id, record.repo_id, record.source_record_id


def _validate_activity_raw(record: RawRecord, group: ActivityGroup) -> None:
    """Check group identity using the existing Phase 2 extraction functions."""
    repo_id = record.repo_id
    if (
        repo_id is None or record.source != "github_rest"
        or record.source_record_type != "repository_activity" or record.package_id is not None
    ):
        raise ValueError("Invalid activity raw scope")
    parse_repo_id(repo_id)
    if group == "contributors":
        entity = activity._extract_contributor(record.payload, repo_id, record.retrieved_at)
        source_id = ("contributor:" + entity.contributor_github_id
                     if entity.contributor_github_id is not None else None)
        event_at = None
    elif group == "commits":
        entity = activity._extract_commit(record.payload, repo_id)
        source_id, event_at = "commit:" + entity.commit_sha, entity.committed_at
    elif group == "issues":
        if "pull_request" in record.payload:
            raise ValueError("PR cannot be stored as an issue")
        entity = activity._extract_issue(record.payload, repo_id)
        source_id, event_at = "issue:" + entity.issue_id, entity.created_at
    elif group == "pull_requests":
        entity = activity._extract_pull_request(record.payload, repo_id)
        source_id, event_at = "pull_request:" + entity.pr_id, entity.created_at
    else:
        entity = activity._extract_release(record.payload, repo_id)
        source_id = "release:" + entity.release_id
        event_at = entity.published_at if not entity.is_draft else None
    endpoint = "pulls" if group == "pull_requests" else group
    parts = urlsplit(record.source_url)
    if (
        record.source_record_id != source_id or record.event_at != event_at
        or record.available_at is not None or parts.query or parts.fragment
        or not parts.path.endswith("/" + endpoint)
    ):
        raise ValueError("Invalid activity raw identity, endpoint or event mapping")


@dataclass(slots=True)
class _RawIndex:
    run_id: str
    # Digests and temporal summaries, never a second copy of provider payloads.
    identities: dict[str, str] = field(default_factory=dict)
    logical: set[tuple[str, str | None, str]] = field(default_factory=set)
    native_ids: dict[tuple[str, str | None, str], set[str]] = field(default_factory=dict)
    repository_ids: dict[str | None, set[str]] = field(default_factory=dict)
    observations: dict[str, tuple[str, str | None, str | None, str | None]] = field(
        default_factory=dict
    )

    def add(self, record: RawRecord) -> None:
        digest = _digest(record)
        previous = self.identities.get(record.raw_record_id)
        if previous is not None and previous != digest:
            raise ValueError("Conflicting raw UUID")
        self.identities[record.raw_record_id] = digest
        if record.run_id != self.run_id:
            return
        key = _logical_key(record)
        if key is not None:
            self.native_ids.setdefault(key, set()).add(record.raw_record_id)
            self.logical.add(key)
        self.repository_ids.setdefault(record.repo_id, set()).add(record.raw_record_id)
        self.observations[record.raw_record_id] = (
            record.run_id, record.repo_id, record.event_at, record.available_at,
        )


class ActivityService:
    """Append valid native raw observations; persistence errors never return success."""

    def __init__(
        self,
        client: GitHubClient | None = None,
        *,
        storage_paths: StoragePaths | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client if client is not None else GitHubClient()
        self._owns_client = client is None
        self.paths = storage_paths if storage_paths is not None else paths
        self.clock = clock
        self.raw_storage = JsonlStorage()
        self._indexes: dict[ActivityGroup, _RawIndex] = {}

    def collect_and_store(
        self,
        repository: GitHubRepository,
        group: ActivityGroup,
        *,
        max_pages: int = 10,
        max_records: int = 1000,
        cursor: str | None = None,
        run_id: str | None = None,
    ) -> CollectionResult:
        repository = validate_record(repository)
        if not isinstance(repository, GitHubRepository):
            raise TypeError("Expected canonical GitHubRepository")
        request = CollectionRequest(
            schema_version=SCHEMA_VERSION,
            record_type="collection_request",
            request_id=str(uuid4()),
            run_id=str(uuid4()) if run_id is None else run_id,
            collector_name=f"github_activity_{group}",
            source="github_rest",
            subjects=[
                CollectionSubject(
                    repo_id=repository.identity.repo_id,
                    repo_full_name=repository.identity.repo_full_name,
                    package_id=None,
                    url=None,
                )
            ],
            record_kinds=["repository_activity"],
            time_range=None,
            cutoff_at=None,
            cursor=cursor,
            max_pages=max_pages,
            max_records=max_records,
        )
        collector = GitHubActivityContractCollector(group, repository, self.client, clock=self.clock)
        result = collector.collect(request)
        validate_collection_pair(request, result)
        if result.records:
            try:
                destination = self.paths.raw_path("github", group)
                for record in result.records:
                    self.raw_storage.append_contract_record(destination, record)
            except (OSError, StorageError) as error:
                raise CollectionError("Unable to persist GitHub activity raw records") from error
        return result

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
            self._owns_client = False

    def reset_recovery_indexes(self) -> None:
        """Rebuild streaming indexes once for a new batch execution/resume."""
        self._indexes.clear()

    def _index(self, group: ActivityGroup, run_id: str) -> _RawIndex:
        if group not in ACTIVITY_GROUPS:
            raise CollectionError("Invalid activity group")
        if group in self._indexes and self._indexes[group].run_id == run_id:
            return self._indexes[group]
        path = self.paths.raw_dir / "github" / f"{group}.jsonl"
        index = _RawIndex(run_id)
        try:
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if not line.endswith("\n"):
                            raise CollectionError(
                                f"Incomplete final activity JSONL line; manual repair required: {path}"
                            )
                        record = validate_record(line)
                        if not isinstance(record, RawRecord):
                            raise ValueError("Expected canonical RawRecord")
                        _validate_activity_raw(record, group)
                        index.add(record)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise CollectionError(f"Invalid or unreadable activity raw JSONL: {path}") from error
        self._indexes[group] = index
        return index

    def journal_path(
        self, context: CollectionContext, repository: GitHubRepository, group: ActivityGroup
    ) -> Path:
        if group not in ACTIVITY_GROUPS or not re.fullmatch(
            r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}",
            context.run_id,
        ):
            raise CollectionError("Invalid activity journal scope")
        numeric = parse_repo_id(repository.identity.repo_id)
        return self.paths.metadata_dir / "collection_runs" / (
            f"activity_{context.run_id}_{numeric}_{group}_pending.json"
        )

    @staticmethod
    def _next_state(
        before: dict[str, Any], result: CollectionResult, stored: int,
        max_pages: int, max_records: int,
    ) -> dict[str, Any]:
        after = dict(before)
        metadata = result.metadata
        for counter_name, increment in (
            ("pages_fetched", metadata.pages_fetched),
            ("records_emitted", metadata.records_emitted),
            ("records_rejected", metadata.records_rejected),
            ("records_stored", stored), ("error_count", len(result.errors)),
        ):
            after[counter_name] += increment
        after["last_request_id"] = result.request_id
        if metadata.pages_fetched:
            after["cursor"] = metadata.next_cursor or ""
        after["pagination_complete"] = int(metadata.pagination_complete)
        if result.errors:
            after["last_error_code"] = result.errors[-1].code
        if metadata.pagination_complete:
            after["status"] = (
                "partial" if after["error_count"] or after["records_rejected"] else "complete"
            )
            after["missing_reason"] = (
                "native_rejections" if after["records_rejected"] else
                "collection_errors" if after["error_count"] else ""
            )
        elif not metadata.pages_fetched and result.errors:
            after["status"] = (
                "retryable_failure" if any(error.retryable for error in result.errors) else "failed"
            )
            after["missing_reason"] = "unavailable"
        elif result.errors and not metadata.records_rejected:
            after["status"] = "failed"
            after["missing_reason"] = "unavailable"
        elif after["pages_fetched"] >= max_pages or after["records_emitted"] >= max_records:
            after["status"] = "partial"
            after["missing_reason"] = "budget_exhausted"
        else:
            after["status"] = "pending"
            after["missing_reason"] = "not_collected"
        return after

    def _load_journal(
        self, path: Path, repository: GitHubRepository, group: ActivityGroup,
        context: CollectionContext, target_index: int,
    ) -> tuple[CollectionRequest, CollectionResult, dict[str, Any], list[RawRecord]]:
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
            expected_keys = {
                "journal_version", "run_id", "repo_id", "group", "target_index",
                "start_cursor", "request", "result", "state", "before_state", "append_raw_ids",
            }
            if (
                not isinstance(journal, dict) or set(journal) != expected_keys
                or type(journal["journal_version"]) is not int or journal["journal_version"] != 1
                or journal["run_id"] != context.run_id
                or journal["repo_id"] != repository.identity.repo_id
                or journal["group"] != group or type(journal["target_index"]) is not int
                or journal["target_index"] != target_index or journal["state"] != "pending"
            ):
                raise ValueError("Journal identity mismatch")
            request = validate_record(journal["request"])
            result = validate_record(journal["result"])
            if not isinstance(request, CollectionRequest) or not isinstance(result, CollectionResult):
                raise ValueError("Invalid journal contracts")
            validate_collection_pair(request, result)
            before = journal["before_state"]
            if not isinstance(before, dict) or set(before) != set(_INITIAL_STATE):
                raise ValueError("Invalid journal checkpoint snapshot")
            test_context = CollectionContext(run_id=context.run_id, started_at=context.started_at, checkpoint_state={
                item_prefix(target_index, group) + key: value for key, value in before.items()
            })
            read_item_state(test_context, target_index, group)
            manifest = json.loads(str(context.checkpoint_state["activity:manifest"]))
            if (
                request.run_id != context.run_id or request.source != "github_rest"
                or request.collector_name != f"github_activity_{group}"
                or request.record_kinds != ["repository_activity"] or request.time_range is not None
                or request.cutoff_at is not None or request.max_pages != 1
                or request.max_records != manifest["max_records_per_group"] - before["records_emitted"]
                or before["pages_fetched"] >= manifest["max_pages_per_group"]
                or before["status"] not in {"pending", "retryable_failure"}
                or journal["start_cursor"] != (before["cursor"] or None)
                or request.cursor != journal["start_cursor"]
                or len(request.subjects) != 1
                or request.subjects[0].repo_id != repository.identity.repo_id
                or request.subjects[0].repo_full_name != repository.identity.repo_full_name
                or request.subjects[0].package_id is not None or request.subjects[0].url is not None
                or len(result.metadata.coverage) != 1
                or result.metadata.coverage[0].data_group != group
            ):
                raise ValueError("Journal request configuration mismatch")
            metadata = result.metadata
            expected_coverage = "unavailable" if result.status == "failed" else result.status
            if (
                metadata.coverage[0].status != expected_coverage
                or metadata.coverage[0].records_observed != len(result.records)
                or (metadata.pagination_complete and metadata.pages_fetched != 1)
                or (not metadata.pages_fetched and (result.records or metadata.records_rejected))
            ):
                raise ValueError("Inconsistent journal page/coverage counters")
            start = _CURSOR.fullmatch(request.cursor or "p:1:i:0")
            if start is None:
                raise ValueError("Invalid journal start cursor")
            page, offset = int(start[1]), int(start[2])
            if metadata.pages_fetched and not metadata.pagination_complete:
                following = _CURSOR.fullmatch(metadata.next_cursor or "")
                if following is None:
                    raise ValueError("Incomplete page needs a native continuation cursor")
                next_page, next_offset = int(following[1]), int(following[2])
                if next_page == page:
                    consumed = next_offset - offset
                elif (next_page, next_offset) == (page + 1, 0):
                    consumed = 100 - offset
                else:
                    raise ValueError("Journal cursor escaped its bounded native page")
                if consumed < metadata.records_emitted + metadata.records_rejected:
                    raise ValueError("Journal cursor did not account for processed native items")
            elif not metadata.pages_fetched and metadata.next_cursor not in {
                None, request.cursor or "p:1:i:0",
            }:
                raise ValueError("Failed page changed its native continuation cursor")
            if metadata.records_emitted + metadata.records_rejected > 100 - offset:
                raise ValueError("Journal page counters exceed the native page remainder")
            endpoint = "pulls" if group == "pull_requests" else group
            for record in result.records:
                _validate_activity_raw(record, group)
                if (
                    record.repo_id != repository.identity.repo_id
                    or not urlsplit(record.source_url).path.casefold().endswith(
                        f"/repos/{repository.identity.repo_full_name}/{endpoint}".casefold()
                    )
                ):
                    raise ValueError("Journal raw repository mismatch")
            selected_ids = journal["append_raw_ids"]
            records = {record.raw_record_id: record for record in result.records}
            if (
                not isinstance(selected_ids, list) or any(type(value) is not str for value in selected_ids)
                or len(selected_ids) != len(set(selected_ids)) or not set(selected_ids) <= records.keys()
            ):
                raise ValueError("Invalid journal append identities")
            selected = [records[raw_id] for raw_id in selected_ids]
            logical = [_logical_key(record) for record in selected if record.source_record_id is not None]
            if len(logical) != len(set(logical)):
                raise ValueError("Duplicate staged native identity")
            index = self._index(group, context.run_id)
            for record in result.records:
                existing = index.identities.get(record.raw_record_id)
                if existing is not None and existing != _digest(record):
                    raw_path = self.paths.raw_dir / "github" / f"{group}.jsonl"
                    raise CollectionError(f"Conflicting persisted raw UUID in activity JSONL: {raw_path}")
                if record.raw_record_id not in selected_ids:
                    key = _logical_key(record)
                    if key is None or (key not in index.logical and key not in logical):
                        raise ValueError("Journal discarded an observation without duplicate evidence")
            return request, result, before, selected
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
            raise CollectionError(f"Invalid or conflicting activity pending journal: {path}") from error

    def recover_pending(
        self, repository: GitHubRepository, group: ActivityGroup, *,
        context: CollectionContext, run_manager: RunManager, target_index: int,
    ) -> bool:
        """Reconcile an original staged step; never create replacement raw UUIDs."""
        path = self.journal_path(context, repository, group)
        if not path.exists():
            return False
        request, result, before, selected = self._load_journal(
            path, repository, group, context, target_index,
        )
        manifest = json.loads(str(context.checkpoint_state["activity:manifest"]))
        after = self._next_state(
            before, result, len(selected), manifest["max_pages_per_group"],
            manifest["max_records_per_group"],
        )
        current = read_item_state(context, target_index, group)
        index = self._index(group, context.run_id)
        committed = current == after and current["last_request_id"] == request.request_id
        if not committed and current != before:
            raise CollectionError(f"Pending journal conflicts with durable checkpoint: {path}")
        missing = [record for record in selected if record.raw_record_id not in index.identities]
        if committed and missing:
            raise CollectionError(f"Committed activity records are missing from JSONL: {path}")
        # A selected UUID must not compete with another UUID for its logical native identity.
        selected_ids = {record.raw_record_id for record in selected}
        persisted_ids = index.repository_ids.get(repository.identity.repo_id, set())
        baseline_count = len(persisted_ids) - sum(raw_id in persisted_ids for raw_id in selected_ids)
        if baseline_count != before["records_stored"]:
            raise CollectionError(f"Pending journal baseline conflicts with stored activity JSONL: {path}")
        for record in selected:
            key = _logical_key(record)
            if key is not None and index.native_ids.get(key, set()) - selected_ids:
                raise CollectionError(f"Conflicting staged native identity in JSONL: {path}")
        try:
            if not committed:
                if selected:
                    destination = self.paths.raw_path("github", group)
                    appended = self.raw_storage.append_contract_records_durable(destination, missing)
                    if type(appended) is not int or appended != len(missing):
                        raise StorageError("Durable activity append returned an inconsistent count")
                    for record in missing:
                        index.add(record)
                old_state = dict(context.checkpoint_state)
                old_counts = context.collected_count, context.error_count
                try:
                    for key, value in after.items():
                        context.set_checkpoint(item_prefix(target_index, group) + key, value)
                    context.record_success(len(selected))
                    context.record_error(len(result.errors))
                    run_manager.save_checkpoint(context)
                except CollectionError as error:
                    context.checkpoint_state = old_state
                    context.collected_count, context.error_count = old_counts
                    raise ActivityPersistenceError(
                        f"Unable to checkpoint activity page; pending journal retained: {path}"
                    ) from error
            path.unlink()
        except (OSError, StorageError) as error:
            self._indexes.pop(group, None)
            raise ActivityPersistenceError(f"Unable to commit activity page; pending journal retained: {path}") from error
        return True

    def collect_page_and_store(
        self, repository: GitHubRepository, group: ActivityGroup, *,
        context: CollectionContext, run_manager: RunManager, target_index: int,
        cursor: str | None, remaining_record_budget: int,
    ) -> CollectionResult:
        """Fetch one bounded Phase 2 step and commit its journal before continuing."""
        repository = validate_record(repository)
        if not isinstance(repository, GitHubRepository):
            raise CollectionError("Expected canonical GitHubRepository")
        path = self.journal_path(context, repository, group)
        if path.exists():
            raise CollectionError("Recover the pending activity journal before collecting another page")
        before = read_item_state(context, target_index, group)
        manifest = json.loads(str(context.checkpoint_state["activity:manifest"]))
        if (
            before["status"] not in {"pending", "retryable_failure"}
            or cursor != (before["cursor"] or None)
            or remaining_record_budget != manifest["max_records_per_group"] - before["records_emitted"]
            or before["pages_fetched"] >= manifest["max_pages_per_group"]
        ):
            raise CollectionError("Activity page does not match durable progress or remaining budgets")
        request = CollectionRequest(
            schema_version=SCHEMA_VERSION, record_type="collection_request",
            request_id=str(uuid4()), run_id=context.run_id, collector_name=f"github_activity_{group}",
            source="github_rest", subjects=[CollectionSubject(
                repo_id=repository.identity.repo_id, repo_full_name=repository.identity.repo_full_name,
                package_id=None, url=None,
            )], record_kinds=["repository_activity"], time_range=None, cutoff_at=None,
            cursor=cursor, max_pages=1, max_records=remaining_record_budget,
        )
        index = self._index(group, context.run_id)
        collector = GitHubActivityContractCollector(group, repository, self.client, clock=self.clock)
        result = collector.collect(request)
        validate_collection_pair(request, result)
        logical = set()
        selected = []
        for record in result.records:
            if record.raw_record_id in index.identities:
                raise CollectionError("A new activity collection step reused an existing raw UUID")
            key = _logical_key(record)
            if key is None or (key not in index.logical and key not in logical):
                selected.append(record.raw_record_id)
                if key is not None:
                    logical.add(key)
        journal = {
            "journal_version": 1, "run_id": context.run_id,
            "repo_id": repository.identity.repo_id, "group": group, "target_index": target_index,
            "start_cursor": cursor, "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"), "state": "pending",
            "before_state": before, "append_raw_ids": selected,
        }
        try:
            write_json_atomic(path, journal)
        except (OSError, ValueError, TypeError) as error:
            raise ActivityPersistenceError(f"Unable to stage activity pending journal: {path}") from error
        self.recover_pending(
            repository, group, context=context, run_manager=run_manager, target_index=target_index,
        )
        return result

    def stored_metrics(
        self, repository: GitHubRepository, group: ActivityGroup, *,
        context: CollectionContext, target_index: int,
    ) -> dict[str, Any]:
        """Report only validated, committed observations; exclude an uncommitted journal."""
        index = self._index(group, context.run_id)
        excluded_ids: set[str] = set()
        path = self.journal_path(context, repository, group)
        if path.exists():
            _, _, before, selected = self._load_journal(path, repository, group, context, target_index)
            if read_item_state(context, target_index, group) == before:
                excluded_ids = {record.raw_record_id for record in selected}
        observed = [
            index.observations[raw_id][2:]
            for raw_id in index.repository_ids.get(repository.identity.repo_id, set())
            if raw_id not in excluded_ids
        ]
        events = [event_at for event_at, _ in observed if event_at is not None]
        if len(observed) != read_item_state(context, target_index, group)["records_stored"]:
            raise CollectionError(f"Activity stored count conflicts with JSONL: {group}")
        return {
            "min_event_at": min(events) if events else None,
            "max_event_at": max(events) if events else None,
            "null_event_at_count": sum(event_at is None for event_at, _ in observed),
            "null_available_at_count": sum(available_at is None for _, available_at in observed),
        }
