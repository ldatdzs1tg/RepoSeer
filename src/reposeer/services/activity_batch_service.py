"""Sequential T-014 activity batches using T-012 checkpoints and page journals."""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from reposeer.clients.github import GitHubClient
from reposeer.collection.context import CollectionContext
from reposeer.collection.persistence import write_json_atomic
from reposeer.collection.run_manager import RunManager
from reposeer.collectors.github.activity import ActivityGroup
from reposeer.contracts import SCHEMA_VERSION, GitHubRepository, validate_record
from reposeer.contracts.identity import checked_timestamp, format_utc_timestamp
from reposeer.exceptions import CollectionError, StorageError
from reposeer.services.activity_service import (
    ACTIVITY_GROUPS,
    ActivityPersistenceError,
    ActivityService,
    initial_item_state,
    item_prefix,
    read_item_state,
)
from reposeer.services.repository_service import RepositoryService, parse_repository_target
from reposeer.storage.paths import StoragePaths, paths

LIMITATIONS = (
    "GitHub REST list pagination is not a frozen historical snapshot.",
    "Complete pagination does not prove complete historical reconstruction; min_event_at is not a coverage start.",
    "Current issue, PR and contributor states are not states at every historical cutoff.",
    "Native contributor aggregates are not historical contributor counts.",
    "available_at=null remains unknown and does not establish cutoff-safe features.",
    "Replay deduplication is scoped to one logical run; anonymous identities and moving pages limit coverage guarantees.",
    "GitHub Releases are not PyPI package releases.",
    "One active writer per run is supported; valid raw files and journals are prerequisites for recovery.",
)


@dataclass(frozen=True, slots=True)
class ActivityBatchItem:
    target: str
    status: Literal["processed", "excluded", "duplicate", "failed"]
    repo_id: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ActivityBatchResult:
    run_id: str
    status: Literal["complete", "partial", "failed"]
    requested_count: int
    processed_count: int
    excluded_count: int
    duplicate_count: int
    failed_count: int
    raw_stored_count: int
    resumable: bool
    checkpoint_path: Path
    report_path: Path
    items: tuple[ActivityBatchItem, ...]
    groups: tuple[dict[str, Any], ...]


def validate_run_id(run_id: str) -> str:
    try:
        parsed = UUID(run_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Run ID must be a canonical UUIDv4") from error
    if parsed.version != 4 or str(parsed) != run_id:
        raise ValueError("Run ID must be a canonical UUIDv4")
    return run_id


def validate_groups(groups: Sequence[str]) -> tuple[ActivityGroup, ...]:
    if isinstance(groups, str | bytes) or not groups:
        raise ValueError("Select at least one activity group")
    if tuple(groups) == ("all",):
        return ACTIVITY_GROUPS
    if any(type(group) is not str or group not in ACTIVITY_GROUPS for group in groups):
        raise ValueError("Unknown or empty activity group")
    if len(groups) != len(set(groups)):
        raise ValueError("Activity groups must be unique")
    return tuple(groups)


def validate_targets(targets: Sequence[str]) -> tuple[str, ...]:
    if isinstance(targets, str | bytes) or not 1 <= len(targets) <= 1000:
        raise ValueError("Provide 1 to 1,000 repository targets")
    result = []
    seen = set()
    for target in targets:
        try:
            slug = parse_repository_target(target)
        except (ValueError, TypeError) as error:
            raise ValueError("Expected owner/repo or an HTTPS GitHub repository URL") from error
        if slug.casefold() not in seen:
            seen.add(slug.casefold())
            result.append(slug)
    return tuple(result)


def validate_budgets(max_pages: int, max_records: int) -> None:
    if type(max_pages) is not int or not 1 <= max_pages <= 10000:
        raise ValueError("Page budget must be an integer from 1 to 10,000")
    if type(max_records) is not int or not 1 <= max_records <= 1000000:
        raise ValueError("Record budget must be an integer from 1 to 1,000,000")


class ActivityBatchService:
    """One shared client, immutable manifest, sequential bounded collection steps."""

    def __init__(
        self, client: GitHubClient | None = None, *,
        storage_paths: StoragePaths | None = None,
        clock: Callable[[], datetime] | None = None,
        run_manager: RunManager | None = None,
        on_run_started: Callable[[str, Path], None] | None = None,
    ) -> None:
        self.paths = storage_paths if storage_paths is not None else paths
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)
        self.run_manager = run_manager if run_manager is not None else RunManager(
            self.paths.metadata_dir / "collection_runs"
        )
        if self.run_manager.checkpoints_dir != self.paths.metadata_dir / "collection_runs":
            raise ValueError("RunManager must use this data directory's collection_runs directory")
        self.client = client if client is not None else GitHubClient()
        self._owns_client = client is None
        self.repository_service = RepositoryService(self.client, storage_paths=self.paths)
        self.repository_service.collector.clock = self.clock
        self.activity_service = ActivityService(
            self.client, storage_paths=self.paths, clock=self.clock,
        )
        self.on_run_started = on_run_started

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
            self._owns_client = False

    def start(
        self, targets: Sequence[str], *, groups: Sequence[ActivityGroup],
        max_pages_per_group: int = 5, max_records_per_group: int = 500,
    ) -> ActivityBatchResult:
        targets = validate_targets(targets)
        groups = validate_groups(groups)
        validate_budgets(max_pages_per_group, max_records_per_group)
        manifest = json.dumps({
            "manifest_version": 1, "schema_version": SCHEMA_VERSION,
            "targets": list(targets), "groups": list(groups),
            "max_pages_per_group": max_pages_per_group,
            "max_records_per_group": max_records_per_group,
        }, sort_keys=True, separators=(",", ":"))
        initial: dict[str, str | int] = {
            "activity:manifest": manifest,
            "activity:manifest_sha256": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
            "activity:started_at": format_utc_timestamp(self.clock()),
        }
        for index, _ in enumerate(targets):
            initial[f"activity:repo:{index}:status"] = "pending"
            initial[f"activity:repo:{index}:error_code"] = ""
            for group in groups:
                initial.update(initial_item_state(index, group))
        context = self.run_manager.start_run(initial_checkpoint_state=initial)
        validate_run_id(context.run_id)
        if self.on_run_started is not None:
            self.on_run_started(context.run_id, self._checkpoint_path(context.run_id))
        return self._execute(context)

    def resume(self, run_id: str) -> ActivityBatchResult:
        run_id = validate_run_id(run_id)
        context = self.run_manager.resume_run(run_id)
        if context is None:
            raise CollectionError(f"Unknown activity run: {run_id}")
        return self._execute(context)

    def _checkpoint_path(self, run_id: str) -> Path:
        return self.run_manager.checkpoints_dir / f"run_{run_id}.json"

    @staticmethod
    def _manifest(context: CollectionContext) -> dict[str, Any]:
        try:
            value = context.checkpoint_state["activity:manifest"]
            if type(value) is not str or hashlib.sha256(value.encode("utf-8")).hexdigest() != context.checkpoint_state["activity:manifest_sha256"]:
                raise ValueError("Manifest fingerprint mismatch")
            manifest = json.loads(value)
            fields = {
                "manifest_version", "schema_version", "targets", "groups",
                "max_pages_per_group", "max_records_per_group",
            }
            if (
                not isinstance(manifest, dict) or set(manifest) != fields
                or type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 1
                or manifest["schema_version"] != SCHEMA_VERSION
                or type(manifest["targets"]) is not list or type(manifest["groups"]) is not list
                or list(validate_targets(manifest["targets"])) != manifest["targets"]
                or list(validate_groups(manifest["groups"])) != manifest["groups"]
            ):
                raise ValueError("Invalid manifest structure")
            validate_budgets(manifest["max_pages_per_group"], manifest["max_records_per_group"])
            checked_timestamp(str(context.checkpoint_state["activity:started_at"]))
            return manifest
        except (ValueError, TypeError, KeyError) as error:
            raise CollectionError("Invalid or conflicting immutable activity run manifest") from error

    @staticmethod
    def _eligible(repository: GitHubRepository) -> bool:
        return (
            repository.visibility == "public" and not repository.is_fork
            and not repository.is_template and repository.mirror_url is None
        )

    def _restore_repositories(
        self, context: CollectionContext, manifest: dict[str, Any],
    ) -> dict[int, GitHubRepository]:
        restored = {}
        seen_ids = set()
        for index, target in enumerate(manifest["targets"]):
            prefix = f"activity:repo:{index}:"
            status = context.checkpoint_state.get(prefix + "status")
            if (
                type(context.checkpoint_state.get(prefix + "error_code")) is not str
                or status not in {"pending", "processed", "excluded", "duplicate", "failed"}
            ):
                raise CollectionError("Invalid repository verification checkpoint status")
            stored = context.checkpoint_state.get(prefix + "context")
            if status in {"processed", "duplicate"} and stored is None:
                raise CollectionError("Verified repository context is missing")
            if stored is not None:
                try:
                    if type(stored) is not str:
                        raise ValueError("Expected canonical repository JSON")
                    repository = validate_record(stored)
                    if (
                        not isinstance(repository, GitHubRepository)
                        or repository.identity.repo_full_name.casefold() != target.casefold()
                        or not self._eligible(repository)
                        or repository.provenance.source != "github_rest"
                        or status not in {"processed", "duplicate"}
                    ):
                        raise ValueError("Verified repository context mismatch")
                    parts = urlsplit(repository.provenance.source_url)
                    if parts.query or parts.fragment or not parts.path.casefold().endswith(
                        "/repos/" + target.casefold()
                    ):
                        raise ValueError("Repository provenance does not match manifest target")
                    if status == "processed" and repository.identity.repo_id in seen_ids:
                        raise ValueError("Duplicate repository marked processed")
                except (ValueError, TypeError) as error:
                    raise CollectionError("Invalid or inconsistent stored repository context") from error
                restored[index] = repository
                if status == "processed":
                    seen_ids.add(repository.identity.repo_id)
            for group in manifest["groups"]:
                state = read_item_state(context, index, group)
                if (
                    state["pages_fetched"] > manifest["max_pages_per_group"]
                    or state["records_emitted"] > manifest["max_records_per_group"]
                    or (stored is None and (state["pages_fetched"] or state["records_emitted"]))
                    or (status in {"excluded", "duplicate"} and state["status"] != "excluded")
                    or (state["status"] == "pending" and (
                        state["pages_fetched"] >= manifest["max_pages_per_group"]
                        or state["records_emitted"] >= manifest["max_records_per_group"]
                    ))
                ):
                    raise CollectionError("Activity checkpoint exceeds immutable budgets or lacks context")
        for index, repository in restored.items():
            if (
                context.checkpoint_state[f"activity:repo:{index}:status"] == "duplicate"
                and repository.identity.repo_id not in seen_ids
            ):
                raise CollectionError("Duplicate repository lacks a verified processed identity")
        stored_count = error_count = 0
        for index, _ in enumerate(manifest["targets"]):
            for group in manifest["groups"]:
                state = read_item_state(context, index, group)
                stored_count += state["records_stored"]
                error_count += state["error_count"]
        if context.collected_count != stored_count or context.error_count != error_count:
            raise CollectionError("Run counters conflict with durable activity group counters")
        return restored

    @staticmethod
    def _metadata_excluded(error: CollectionError) -> bool:
        # Public T-013 API communicates verified exclusions via fixed diagnostics.
        message = str(error)
        if message == "Repository is outside the public metadata scope":
            return True
        prefix = "Repository excluded from processed output: "
        if not message.startswith(prefix):
            return False
        reasons = message.removeprefix(prefix).split(", ")
        return all(
            reason in {"fork_repository", "template_repository", "mirror_repository"}
            for reason in reasons
        )

    def _save_target(
        self, context: CollectionContext, index: int, status: str, code: str,
        groups: Sequence[ActivityGroup], repository: GitHubRepository | None = None,
    ) -> None:
        before = dict(context.checkpoint_state)
        prefix = f"activity:repo:{index}:"
        context.set_checkpoint(prefix + "status", status)
        context.set_checkpoint(prefix + "error_code", code)
        if repository is not None:
            context.set_checkpoint(prefix + "context", repository.model_dump_json())
        if status in {"excluded", "duplicate"}:
            for group in groups:
                context.set_checkpoint(item_prefix(index, group) + "status", "excluded")
                context.set_checkpoint(item_prefix(index, group) + "missing_reason", status)
        try:
            self.run_manager.save_checkpoint(context)
        except CollectionError:
            context.checkpoint_state = before
            raise

    def _execute(self, context: CollectionContext) -> ActivityBatchResult:
        manifest = self._manifest(context)
        repositories = self._restore_repositories(context, manifest)
        self.activity_service.reset_recovery_indexes()
        # Validate every run journal before any new API call, including orphan/conflicting files.
        expected = {}
        for index, repository in repositories.items():
            if context.checkpoint_state[f"activity:repo:{index}:status"] == "processed":
                for group in manifest["groups"]:
                    expected[self.activity_service.journal_path(context, repository, group)] = (index, repository, group)
        pending = list(self.run_manager.checkpoints_dir.glob(f"activity_{context.run_id}_*_pending.json"))
        if any(path not in expected for path in pending):
            raise CollectionError("Unexpected activity pending journal for this run")
        blocked = set()
        for path in pending:
            index, repository, group = expected[path]
            try:
                self.activity_service.recover_pending(
                    repository, group, context=context, run_manager=self.run_manager,
                    target_index=index,
                )
            except ActivityPersistenceError:
                blocked.add((index, group))
        seen_ids = {
            repository.identity.repo_id for index, repository in repositories.items()
            if context.checkpoint_state[f"activity:repo:{index}:status"] == "processed"
        }
        for index, target in enumerate(manifest["targets"]):
            prefix = f"activity:repo:{index}:"
            target_status = context.checkpoint_state[prefix + "status"]
            if target_status in {"excluded", "duplicate"}:
                continue
            repository = repositories.get(index)
            if repository is None:
                try:
                    owner, repo = target.split("/")
                    repository = self.repository_service.collect_and_store(owner, repo)
                    repository = validate_record(repository)
                    if not isinstance(repository, GitHubRepository) or repository.identity.repo_full_name.casefold() != target.casefold():
                        raise ValueError("Invalid verified repository identity")
                except (CollectionError, StorageError, OSError, ValueError, TypeError) as error:
                    excluded = isinstance(error, CollectionError) and self._metadata_excluded(error)
                    self._save_target(
                        context, index, "excluded" if excluded else "failed",
                        "excluded" if excluded else "metadata_error", manifest["groups"],
                    )
                    continue
                if not self._eligible(repository):
                    self._save_target(context, index, "excluded", "excluded", manifest["groups"])
                    continue
                duplicate = repository.identity.repo_id in seen_ids
                self._save_target(
                    context, index, "duplicate" if duplicate else "processed", "",
                    manifest["groups"], repository,
                )
                repositories[index] = repository
                if duplicate:
                    continue
                seen_ids.add(repository.identity.repo_id)
            for group in manifest["groups"]:
                if (index, group) in blocked:
                    continue
                while True:
                    state = read_item_state(context, index, group)
                    if state["status"] in {"complete", "partial", "failed", "excluded"}:
                        break
                    if state["pages_fetched"] >= manifest["max_pages_per_group"] or state["records_emitted"] >= manifest["max_records_per_group"]:
                        raise CollectionError("Exhausted activity budget has inconsistent checkpoint status")
                    try:
                        self.activity_service.collect_page_and_store(
                            repository, group, context=context, run_manager=self.run_manager,
                            target_index=index, cursor=state["cursor"] or None,
                            remaining_record_budget=manifest["max_records_per_group"] - state["records_emitted"],
                        )
                    except ActivityPersistenceError:
                        # Reload counters after any persistence interruption before reporting.
                        durable = self.run_manager.resume_run(context.run_id)
                        if durable is None:
                            raise CollectionError("Activity checkpoint disappeared") from None
                        context = durable
                        break
                    if read_item_state(context, index, group)["status"] == "retryable_failure":
                        break
        return self._report(context, manifest, repositories)

    def _report(
        self, context: CollectionContext, manifest: dict[str, Any],
        repositories: dict[int, GitHubRepository],
    ) -> ActivityBatchResult:
        items = []
        entries = []
        eligible_entries = []
        resumable = False
        pending = bool(list(self.run_manager.checkpoints_dir.glob(
            f"activity_{context.run_id}_*_pending.json"
        )))
        for index, target in enumerate(manifest["targets"]):
            prefix = f"activity:repo:{index}:"
            target_status = str(context.checkpoint_state[prefix + "status"])
            repository = repositories.get(index)
            code = str(context.checkpoint_state[prefix + "error_code"])
            items.append(ActivityBatchItem(
                target, target_status, repository.identity.repo_id if repository else None, code or None,
            ))
            if target_status in {"pending", "failed"}:
                resumable = True
            for group in manifest["groups"]:
                state = read_item_state(context, index, group)
                status = state["status"]
                missing_reason = state["missing_reason"] or None
                metrics = {
                    "min_event_at": None, "max_event_at": None,
                    "null_event_at_count": 0, "null_available_at_count": 0,
                }
                if target_status == "processed" and repository is not None:
                    metrics = self.activity_service.stored_metrics(
                        repository, group, context=context, target_index=index,
                    )
                    if status == "pending":
                        status, missing_reason = "retryable_failure", "persistence_interrupted"
                    if status == "retryable_failure":
                        resumable = True
                elif target_status == "failed":
                    status, missing_reason = "failed", "metadata_unavailable"
                else:
                    status, missing_reason = "excluded", target_status
                entry = {
                    "repo_id": repository.identity.repo_id if repository else None,
                    "repo_full_name": repository.identity.repo_full_name if repository else target,
                    "group": group, "status": status,
                    **{field: state[field] for field in (
                        "pages_fetched", "records_emitted", "records_stored", "records_rejected", "error_count",
                    )},
                    "pagination_complete": bool(state["pagination_complete"]),
                    "next_cursor": state["cursor"] or None,
                    "missing_reason": missing_reason, **metrics,
                }
                entries.append(entry)
                if target_status != "duplicate":
                    eligible_entries.append(entry)
        processed = sum(item.status == "processed" for item in items)
        excluded = sum(item.status == "excluded" for item in items)
        duplicate = sum(item.status == "duplicate" for item in items)
        failed = sum(item.status == "failed" for item in items)
        stored = sum(entry["records_stored"] for entry in eligible_entries)
        all_complete = bool(eligible_entries) and all(
            entry["status"] == "complete" for entry in eligible_entries
        ) and not pending
        useful = stored > 0 or any(entry["status"] == "complete" for entry in eligible_entries)
        status = "complete" if all_complete else "partial" if useful else "failed"
        report = {
            "report_version": 1, "run_id": context.run_id, "schema_version": SCHEMA_VERSION,
            "status": status, "started_at": context.checkpoint_state["activity:started_at"],
            "finished_at": format_utc_timestamp(self.clock()),
            "targets_requested": len(manifest["targets"]),
            "repositories_processed": processed, "repositories_excluded": excluded,
            "repositories_duplicate": duplicate, "repositories_failed": failed,
            "total_raw_records_stored": stored, "groups": entries,
            "limitations": list(LIMITATIONS),
        }
        path = self.run_manager.checkpoints_dir / f"activity_{context.run_id}_report.json"
        try:
            write_json_atomic(path, report)
        except (OSError, ValueError, TypeError) as error:
            raise CollectionError(f"Unable to persist activity batch report: {path}") from error
        return ActivityBatchResult(
            context.run_id, status, len(manifest["targets"]), processed, excluded, duplicate,
            failed, stored, resumable or pending, self._checkpoint_path(context.run_id), path,
            tuple(items), tuple(entries),
        )
