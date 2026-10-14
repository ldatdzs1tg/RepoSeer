"""Offline T-014 Phase 3 orchestration and injected crash recovery regressions."""

import json
from collections.abc import Callable, Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from reposeer.cli import collect_activity as cli
from reposeer.cli.app import app
from reposeer.clients.github import GitHubClient
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.run_manager import RunManager
from reposeer.config import settings
from reposeer.contracts import CollectionRequest, CollectionResult, RawRecord, validate_record
from reposeer.exceptions import CollectionError
from reposeer.services import activity_batch_service as batch_module
from reposeer.services import activity_service as activity_module
from reposeer.services.activity_batch_service import ActivityBatchService
from reposeer.services.activity_service import ACTIVITY_GROUPS
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2024, 2, 29, 12, tzinfo=UTC)
SLUG = "Synthetic/Demo"
SECRET = "synthetic-secret-provider-body"


def native(group: str, index: int = 0) -> dict[str, Any]:
    if group == "commits":
        return {"sha": f"{index + 1:040x}", "commit": {}, "parents": []}
    if group == "contributors":
        return {"id": index + 101, "type": "User", "contributions": 1}
    if group == "releases":
        return {"id": index + 201, "tag_name": f"v{index}", "draft": True, "prerelease": False}
    payload = {
        "id": index + 301, "number": index + 1, "title": "Synthetic activity",
        "state": "open", "created_at": "2024-01-01T00:00:00Z",
    }
    if group == "pull_requests":
        payload["merged"] = False
    return payload


def metadata(slug: str, numeric_id: int = 900000001, **changes: Any) -> dict[str, Any]:
    payload = json.loads(
        (ROOT / "examples/contracts/github_raw_record.json").read_text(encoding="utf-8")
    )["payload"]
    payload.update(full_name=slug, id=numeric_id, **changes)
    return payload


@pytest.fixture
def environment(tmp_path, monkeypatch) -> Iterator[Callable]:
    monkeypatch.setattr(settings.collection, "cache_enabled", False)
    sessions = []

    def make(
        pages: dict[tuple[str, str, int], Any] | None = None,
        repositories: dict[str, Any] | None = None,
        before_request: Callable[[httpx.Request], None] | None = None,
    ):
        calls = []
        pages = {} if pages is None else pages
        repositories = {SLUG: metadata(SLUG)} if repositories is None else repositories

        def handler(request):
            if before_request is not None:
                before_request(request)
            calls.append(request)
            components = request.url.path.removeprefix("/repos/").split("/")
            slug = "/".join(components[:2])
            if len(components) == 2:
                value = repositories[slug]
            else:
                group = "pull_requests" if components[2] == "pulls" else components[2]
                value = pages.get((slug, group, int(request.url.params["page"])), [])
            if isinstance(value, httpx.Response):
                return value
            if isinstance(value, Exception):
                raise value
            return httpx.Response(200, json=deepcopy(value))

        http = HTTPClient(
            max_retries=1, transport=httpx.MockTransport(handler),
            clock=lambda: 0, sleep=lambda delay: None,
        )
        sessions.append(http)
        client = GitHubClient(
            token="synthetic-token", base_url="https://github.example", http_client=http,
        )
        service = ActivityBatchService(
            client, storage_paths=StoragePaths(tmp_path), clock=lambda: NOW,
        )
        return service, client, calls, pages

    yield make
    for session in sessions:
        session.close()


def restart(service, client) -> ActivityBatchService:
    return ActivityBatchService(client, storage_paths=service.paths, clock=lambda: NOW)


def checkpoint(service, run_id):
    context = service.run_manager.resume_run(run_id)
    assert context is not None
    return context


def state(service, run_id, group="commits", index=0):
    prefix = f"activity:item:{index}:{group}:"
    return {
        key.removeprefix(prefix): value
        for key, value in checkpoint(service, run_id).checkpoint_state.items()
        if key.startswith(prefix)
    }


def raws(service, group="commits") -> list[RawRecord]:
    return JsonlStorage().read_contract_records(service.paths.raw_dir / "github" / f"{group}.jsonl")


def journals(service, run_id):
    return list(service.run_manager.checkpoints_dir.glob(f"activity_{run_id}_*_pending.json"))


def assert_unique(records):
    assert len(records) == len({record.raw_record_id for record in records})
    logical = [
        (record.run_id, record.repo_id, record.source_record_id)
        for record in records if record.source_record_id is not None
    ]
    assert len(logical) == len(set(logical))
    assert all(isinstance(validate_record(record), RawRecord) for record in records)


def test_initial_manifest_and_completed_resume_are_durable(environment, monkeypatch):
    service, client, calls, _ = environment()
    writes = []
    save = service.run_manager.save_checkpoint

    def capture(context):
        writes.append(deepcopy(context.checkpoint_state))
        return save(context)

    monkeypatch.setattr(service.run_manager, "save_checkpoint", capture)
    announced = []
    service.on_run_started = lambda run_id, path: announced.append((run_id, path, len(calls)))
    result = service.start([SLUG, "https://github.com/synthetic/demo/"], groups=ACTIVITY_GROUPS)
    manifest = json.loads(writes[0]["activity:manifest"])
    assert manifest == {
        "manifest_version": 1, "schema_version": "0.1.0", "targets": [SLUG],
        "groups": list(ACTIVITY_GROUPS), "max_pages_per_group": 5, "max_records_per_group": 500,
    }
    assert result.requested_count == 1 and result.status == "complete"
    assert announced == [(result.run_id, result.checkpoint_path, 0)]
    assert len(calls) == 6
    assert sum(not request.url.query for request in calls) == 1
    assert result.raw_stored_count == 0
    count = len(calls)
    resumed = restart(service, client).resume(result.run_id)
    assert len(calls) == count and resumed.groups == result.groups
    assert resumed.status == "complete" and not resumed.resumable
    assert writes[0]["activity:item:0:commits:status"] == "pending"


@pytest.mark.parametrize(
    "targets,groups,pages,records",
    [(["bad-target"], ["commits"], 5, 500), ([SLUG], ["unknown"], 5, 500),
     ([SLUG], ["commits", "commits"], 5, 500), ([SLUG], [], 5, 500),
     ([SLUG], ["commits"], 0, 500), ([SLUG], ["commits"], 5, True)],
)
def test_invalid_configuration_creates_no_run(environment, targets, groups, pages, records):
    service, _, calls, _ = environment()
    with pytest.raises(ValueError):
        service.start(targets, groups=groups, max_pages_per_group=pages, max_records_per_group=records)
    assert calls == [] and not list(service.run_manager.checkpoints_dir.glob("run_*.json"))


def test_run_manager_initial_state_validation_and_no_overwrite(tmp_path):
    manager = RunManager(tmp_path / "runs")
    run_id = str(uuid4())
    context = manager.start_run(run_id, initial_checkpoint_state={"activity:manifest": "{}"})
    assert manager.resume_run(run_id) == context
    before = (tmp_path / "runs" / f"run_{run_id}.json").read_bytes()
    with pytest.raises(CollectionError, match="already exists"):
        manager.start_run(run_id)
    assert (tmp_path / "runs" / f"run_{run_id}.json").read_bytes() == before
    invalid = str(uuid4())
    with pytest.raises(ValueError):
        manager.start_run(invalid, initial_checkpoint_state={"bad": True})
    assert not (tmp_path / "runs" / f"run_{invalid}.json").exists()
    assert manager.start_run("legacy").run_id == "legacy"


@pytest.mark.parametrize("run_id", ["../bad", "", "00000000-0000-1000-8000-000000000001", None])
def test_resume_invalid_uuid_never_fetches(environment, run_id):
    service, _, calls, _ = environment()
    with pytest.raises(ValueError):
        service.resume(run_id)
    assert not calls


def test_resume_missing_run(environment):
    service, _, calls, _ = environment()
    with pytest.raises(CollectionError, match="Unknown activity run"):
        service.resume(str(uuid4()))
    assert not calls


@pytest.mark.parametrize(
    "changes", [{"private": True}, {"fork": True}, {"is_template": True},
                {"mirror_url": "https://example.org/mirror"}, {"visibility": "internal"}],
)
def test_exclusion_is_permanent_and_does_not_block_eligible_repo(environment, changes):
    other = "Synthetic/Other"
    service, client, calls, _ = environment(repositories={
        SLUG: metadata(SLUG, **changes), other: metadata(other, 900000002, archived=True),
    })
    result = service.start([SLUG, other], groups=["commits", "issues"])
    assert result.status == "partial" and result.excluded_count == result.processed_count == 1
    assert [item.status for item in result.items] == ["excluded", "processed"]
    assert len(calls) == 4
    restart(service, client).resume(result.run_id)
    assert len(calls) == 4


def test_metadata_failure_isolated_and_numeric_identity_deduplicated(environment):
    bad, alias = "Synthetic/Bad", "Synthetic/Alias"
    service, _, calls, _ = environment(repositories={
        bad: httpx.Response(404, text=SECRET), SLUG: metadata(SLUG), alias: metadata(alias),
    })
    result = service.start([bad, SLUG, alias], groups=["commits"])
    assert result.status == "partial" and result.failed_count == result.duplicate_count == 1
    assert result.processed_count == 1 and len(calls) == 4
    assert [item.status for item in result.items] == ["failed", "processed", "duplicate"]
    assert SECRET not in repr(result) + result.report_path.read_text(encoding="utf-8")


def test_all_excluded_is_failed(environment):
    service, _, calls, _ = environment(repositories={SLUG: metadata(SLUG, private=True)})
    result = service.start([SLUG], groups=["commits"])
    assert result.status == "failed" and result.excluded_count == 1
    assert not result.resumable and len(calls) == 1


def test_page_is_persisted_and_checkpointed_before_next_fetch(environment):
    observed = []
    holder = {}

    def before_request(request):
        if request.url.params.get("page") == "2":
            service = holder["service"]
            run_path = next(service.run_manager.checkpoints_dir.glob("run_*.json"))
            run_id = json.loads(run_path.read_text(encoding="utf-8"))["run_id"]
            progress = state(service, run_id)
            observed.append((len(raws(service)), progress["cursor"], progress["records_stored"]))
            assert not journals(service, run_id)

    service, _, _, _ = environment({
        (SLUG, "commits", 1): [native("commits", i) for i in range(100)],
        (SLUG, "commits", 2): [native("commits", 100)],
    }, before_request=before_request)
    holder["service"] = service
    result = service.start([SLUG], groups=["commits"])
    assert observed == [(100, "p:2:i:0", 100)]
    assert result.status == "complete" and result.raw_stored_count == 101
    assert result.groups[0]["pages_fetched"] == 2


@pytest.mark.parametrize("page_budget,record_budget,cursor,count", [(1, 500, "p:2:i:0", 100), (5, 2, "p:1:i:2", 2)])
def test_budget_partial_is_terminal_across_resume(environment, page_budget, record_budget, cursor, count):
    service, client, calls, _ = environment({
        (SLUG, "commits", 1): [native("commits", i) for i in range(100)],
    })
    result = service.start(
        [SLUG], groups=["commits"], max_pages_per_group=page_budget, max_records_per_group=record_budget,
    )
    assert result.status == "partial" and result.groups[0]["missing_reason"] == "budget_exhausted"
    assert result.groups[0]["next_cursor"] == cursor and result.raw_stored_count == count
    assert not result.resumable
    before = len(calls)
    resumed = restart(service, client).resume(result.run_id)
    assert len(calls) == before and resumed.groups == result.groups


def test_retry_preserves_original_budgets_cursor_and_verified_context(environment):
    service, client, calls, pages = environment({
        (SLUG, "commits", 1): [native("commits", i) for i in range(100)],
        (SLUG, "commits", 2): httpx.Response(503, text=SECRET),
    })
    result = service.start([SLUG], groups=["commits"], max_pages_per_group=2, max_records_per_group=101)
    progress = state(service, result.run_id)
    assert progress["pages_fetched"] == 1 and progress["cursor"] == "p:2:i:0"
    assert progress["records_stored"] == 100 and progress["error_count"] == 1
    assert result.groups[0]["status"] == "retryable_failure" and result.resumable
    pages[(SLUG, "commits", 2)] = [native("commits", 100), native("commits", 101)]
    resumed = restart(service, client).resume(result.run_id)
    assert resumed.status == "partial" and resumed.raw_stored_count == 101
    assert state(service, result.run_id)["pages_fetched"] == 2
    assert resumed.groups[0]["next_cursor"] == "p:2:i:1"
    assert sum(not request.url.query for request in calls) == 1
    assert len(calls) == 4 and not resumed.resumable


def test_issues_pr_filter_and_rejections_do_not_corrupt_cursor(environment):
    page = [{"pull_request": None}] * 98 + [native("issues", 0), {"id": 99}]
    service, _, calls, _ = environment({
        (SLUG, "issues", 1): page, (SLUG, "issues", 2): [native("issues", 1)],
    })
    result = service.start([SLUG], groups=["issues"])
    assert result.raw_stored_count == 2 and result.status == "partial"
    assert result.groups[0]["pagination_complete"] and result.groups[0]["records_rejected"] == 1
    assert result.groups[0]["missing_reason"] == "native_rejections"
    assert len(calls) == 3


def test_moving_pages_deduplicate_known_identities_only_within_run(environment):
    service, _, _, _ = environment({
        (SLUG, "commits", 1): [native("commits", i) for i in range(100)],
        (SLUG, "commits", 2): [native("commits", 99), native("commits", 100)],
    })
    first = service.start([SLUG], groups=["commits"])
    assert first.groups[0]["records_emitted"] == 102 and first.raw_stored_count == 101
    second = service.start([SLUG], groups=["commits"])
    assert second.raw_stored_count == 101 and len(raws(service)) == 202
    assert_unique(raws(service))


@pytest.mark.parametrize(
    "failure_point", ["before_append", "mid_append", "checkpoint", "checkpoint_atomic", "delete", "journal_write"],
)
def test_restart_replays_crash_without_double_append_or_counter_increment(
    environment, monkeypatch, failure_point,
):
    service, client, calls, _ = environment({
        (SLUG, "commits", 1): [native("commits", i) for i in range(3)],
    })
    append = service.activity_service.raw_storage.append_contract_records_durable
    unlink = Path.unlink
    staged_ids = []
    with monkeypatch.context() as patch:
        if failure_point in {"before_append", "mid_append"}:
            def fail_append(path, records):
                records = list(records)
                staged_ids.extend(record.raw_record_id for record in records)
                if failure_point == "mid_append":
                    append(path, records[:1])
                raise OSError(SECRET)
            patch.setattr(service.activity_service.raw_storage, "append_contract_records_durable", fail_append)
        elif failure_point == "checkpoint":
            save = service.run_manager.save_checkpoint

            def fail_save(context):
                if context.checkpoint_state.get("activity:item:0:commits:pages_fetched", 0):
                    raise CollectionError("Injected checkpoint failure")
                return save(context)
            patch.setattr(service.run_manager, "save_checkpoint", fail_save)
        elif failure_point == "checkpoint_atomic":
            from reposeer.collection import run_manager as manager_module

            atomic = manager_module.write_json_atomic

            def fail_atomic(path, data):
                if data["checkpoint_state"].get("activity:item:0:commits:pages_fetched", 0):
                    raise OSError(SECRET)
                return atomic(path, data)
            patch.setattr(manager_module, "write_json_atomic", fail_atomic)
        elif failure_point == "delete":
            def fail_unlink(path, *args, **kwargs):
                if path.name.endswith("_pending.json"):
                    raise OSError(SECRET)
                return unlink(path, *args, **kwargs)
            patch.setattr(Path, "unlink", fail_unlink)
        else:
            patch.setattr(activity_module, "write_json_atomic", lambda path, data: (_ for _ in ()).throw(OSError(SECRET)))
        result = service.start([SLUG], groups=["commits"])
    progress = state(service, result.run_id)
    pending = journals(service, result.run_id)
    if failure_point == "delete":
        assert progress["records_stored"] == 3 and progress["pages_fetched"] == 1
        assert result.raw_stored_count == 3
    else:
        assert progress["records_stored"] == progress["pages_fetched"] == 0
        assert progress["cursor"] == "" and result.raw_stored_count == 0
    if failure_point == "journal_write":
        assert not pending
    else:
        assert len(pending) == 1
        journal = json.loads(pending[0].read_text(encoding="utf-8"))
        staged_ids = [record["raw_record_id"] for record in journal["result"]["records"]]
        assert isinstance(validate_record(journal["request"]), CollectionRequest)
        assert isinstance(validate_record(journal["result"]), CollectionResult)
    recovered = restart(service, client).resume(result.run_id)
    records = raws(service)
    assert recovered.status == "complete" and recovered.raw_stored_count == 3
    assert state(service, result.run_id)["pages_fetched"] == 1
    assert state(service, result.run_id)["records_emitted"] == 3
    assert not journals(service, result.run_id)
    assert_unique(records)
    if staged_ids:
        assert [record.raw_record_id for record in records] == staged_ids
    assert len(calls) == (3 if failure_point == "journal_write" else 2)
    assert SECRET not in repr(result) + repr(recovered)


def test_anonymous_replay_preserves_uuid_and_distinct_identical_observations(environment, monkeypatch):
    payload = {"type": "Anonymous", "contributions": 1, "email": "synthetic@example.org"}
    service, client, calls, _ = environment({(SLUG, "contributors", 1): [payload, payload]})
    with monkeypatch.context() as patch:
        patch.setattr(service.activity_service.raw_storage, "append_contract_records_durable",
                      lambda path, records: (_ for _ in ()).throw(OSError(SECRET)))
        result = service.start([SLUG], groups=["contributors"])
    journal = json.loads(journals(service, result.run_id)[0].read_text(encoding="utf-8"))
    original = [record["raw_record_id"] for record in journal["result"]["records"]]
    recovered = restart(service, client).resume(result.run_id)
    records = raws(service, "contributors")
    assert recovered.raw_stored_count == 2 and len(calls) == 2
    assert [record.raw_record_id for record in records] == original
    assert records[0].payload == records[1].payload == payload
    assert all(record.source_record_id is record.event_at is None for record in records)
    assert_unique(records)


@pytest.mark.parametrize("damage", ["journal_identity", "journal_cursor", "result_cursor", "raw_conflict", "raw_invalid", "raw_tail"])
def test_recovery_fails_closed_on_corruption_without_fetching(environment, monkeypatch, damage):
    service, client, calls, _ = environment({(SLUG, "commits", 1): [native("commits")]})
    with monkeypatch.context() as patch:
        patch.setattr(service.activity_service.raw_storage, "append_contract_records_durable",
                      lambda path, records: (_ for _ in ()).throw(OSError(SECRET)))
        result = service.start([SLUG], groups=["commits"])
    pending = journals(service, result.run_id)[0]
    journal = json.loads(pending.read_text(encoding="utf-8"))
    path = service.paths.raw_dir / "github" / "commits.jsonl"
    if damage.startswith("journal") or damage == "result_cursor":
        if damage == "journal_identity":
            journal["repo_id"] = "github:repository:999"
        elif damage == "journal_cursor":
            journal["start_cursor"] = "p:9:i:0"
        else:
            journal["result"]["metadata"]["pagination_complete"] = False
            journal["result"]["metadata"]["next_cursor"] = "p:9:i:0"
            journal["result"]["status"] = "partial"
            journal["result"]["metadata"]["coverage"][0]["status"] = "partial"
            journal["result"]["metadata"]["coverage"][0]["missing_reason"] = "not_collected"
        pending.write_text(json.dumps(journal), encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        if damage == "raw_conflict":
            original = journal["result"]["records"][0]
            altered = deepcopy(original)
            altered["payload"]["provider_extra"] = "altered"
            path.write_text(json.dumps(original) + "\n" + json.dumps(altered) + "\n", encoding="utf-8")
        elif damage == "raw_invalid":
            path.write_text("{}\n", encoding="utf-8")
        else:
            path.write_text('{"partial":', encoding="utf-8")
    before_bytes = path.read_bytes() if path.exists() else None
    before_calls = len(calls)
    with pytest.raises(CollectionError) as error:
        restart(service, client).resume(result.run_id)
    assert len(calls) == before_calls and state(service, result.run_id)["pages_fetched"] == 0
    assert pending.exists() and SECRET not in str(error.value)
    if before_bytes is not None:
        assert path.read_bytes() == before_bytes
        assert str(path) in str(error.value)


@pytest.mark.parametrize("damage", ["manifest", "context", "cursor", "counter", "orphan_journal"])
def test_invalid_durable_state_fails_before_new_requests(environment, damage):
    service, client, calls, _ = environment()
    result = service.start([SLUG], groups=["commits"])
    context = checkpoint(service, result.run_id)
    if damage == "manifest":
        manifest = json.loads(str(context.checkpoint_state["activity:manifest"]))
        manifest["max_pages_per_group"] = 10
        context.checkpoint_state["activity:manifest"] = json.dumps(manifest)
    elif damage == "context":
        repository = json.loads(str(context.checkpoint_state["activity:repo:0:context"]))
        repository["identity"]["repo_full_name"] = "Other/Repo"
        context.checkpoint_state["activity:repo:0:context"] = json.dumps(repository)
    elif damage == "cursor":
        context.checkpoint_state["activity:item:0:commits:cursor"] = "p:0:i:0"
    elif damage == "counter":
        context.checkpoint_state["activity:item:0:commits:pages_fetched"] = 999
    else:
        path = service.run_manager.checkpoints_dir / f"activity_{result.run_id}_999_commits_pending.json"
        path.write_text("{}", encoding="utf-8")
    service.run_manager.save_checkpoint(context)
    before_calls = len(calls)
    with pytest.raises(CollectionError):
        restart(service, client).resume(result.run_id)
    assert len(calls) == before_calls


def test_durable_append_validates_before_writing_and_syncs(tmp_path, monkeypatch):
    from reposeer.storage.raw import jsonl as jsonl_module

    record = validate_record((ROOT / "examples/contracts/github_raw_record.json").read_text(encoding="utf-8"))
    storage = JsonlStorage()
    path = tmp_path / "records.jsonl"
    synced = []
    fsync = jsonl_module.os.fsync

    def capture(fd):
        synced.append(path.read_bytes())
        fsync(fd)

    monkeypatch.setattr(jsonl_module.os, "fsync", capture)
    assert storage.append_contract_records_durable(path, [record]) == 1
    assert synced and storage.read_contract_records(path) == [record]
    before = path.read_bytes()
    with pytest.raises(TypeError):
        storage.append_contract_records_durable(path, [record, {}])
    assert path.read_bytes() == before


def test_report_schema_counts_nulls_and_regeneration_after_write_failure(environment, monkeypatch):
    service, client, calls, _ = environment({
        (SLUG, "commits", 1): [native("commits")],
        (SLUG, "contributors", 1): [native("contributors")],
        (SLUG, "issues", 1): [native("issues")],
    })
    announced = []
    service.on_run_started = lambda run_id, path: announced.append(run_id)
    with monkeypatch.context() as patch:
        patch.setattr(batch_module, "write_json_atomic", lambda path, data: (_ for _ in ()).throw(OSError(SECRET)))
        with pytest.raises(CollectionError, match="persist activity batch report"):
            service.start([SLUG], groups=["commits", "contributors", "issues"])
    run_id = announced[0]
    before_calls = len(calls)
    recovered = restart(service, client).resume(run_id)
    assert len(calls) == before_calls and recovered.raw_stored_count == 3
    report = json.loads(recovered.report_path.read_text(encoding="utf-8"))
    assert set(report) == {
        "report_version", "run_id", "schema_version", "status", "started_at", "finished_at",
        "targets_requested", "repositories_processed", "repositories_excluded", "repositories_duplicate",
        "repositories_failed", "total_raw_records_stored", "groups", "limitations",
    }
    assert report["report_version"] == 1 and report["run_id"] == run_id
    assert report["total_raw_records_stored"] == sum(group["records_stored"] for group in report["groups"])
    assert all(group["null_available_at_count"] == 1 for group in report["groups"])
    assert [group["null_event_at_count"] for group in report["groups"]] == [1, 1, 0]
    assert report["groups"][2]["min_event_at"] == "2024-01-01T00:00:00.000Z"
    assert len(report["limitations"]) >= 7


def test_shared_client_ownership(environment, monkeypatch):
    service, client, _, _ = environment()
    service.close()
    service.close()
    assert not client.http._sync_client.is_closed
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    monkeypatch.setattr(batch_module, "GitHubClient", lambda: client)
    owned = ActivityBatchService(storage_paths=service.paths, clock=lambda: NOW)
    owned.close()
    owned.close()
    assert closed == [True]


@pytest.mark.parametrize("groups", ["all", "commits,issues"])
def test_cli_start_resume_and_original_collect_remain_available(environment, tmp_path, monkeypatch, groups):
    service, client, calls, _ = environment()
    services = []

    def factory(**options):
        instance = restart(service, client)
        instance.on_run_started = options.get("on_run_started")
        services.append(instance)
        return instance

    monkeypatch.setattr(cli, "ActivityBatchService", factory)
    path = tmp_path / "repos.txt"
    path.write_text("  # Synthetic UTF-8 input \u2014 offline\n\n" + SLUG + "\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["collect-activity", "start", "--repos-file", str(path), "--groups", groups])
    assert result.exit_code == 0, result.output
    assert "Run ID:" in result.output and "Report:" in result.output and "Resumable: no" in result.output
    run_path = next(service.run_manager.checkpoints_dir.glob("run_*.json"))
    run_id = json.loads(run_path.read_text(encoding="utf-8"))["run_id"]
    before = len(calls)
    resumed = runner.invoke(app, ["collect-activity", "resume", "--run-id", run_id])
    assert resumed.exit_code == 0 and len(calls) == before
    assert len(services) == 2
    help_result = runner.invoke(app, ["collect", "--help"])
    assert help_result.exit_code == 0 and "--repos-file" in help_result.output
    assert "--source" in help_result.output
    assert SECRET not in result.output + resumed.output


@pytest.mark.parametrize("option,value", [("--groups", "commits,"), ("--groups", "unknown"), ("--groups", "commits,commits"), ("--max-pages", "0"), ("--max-records", "1000001")])
def test_cli_invalid_configuration_is_exit_two_before_service_creation(tmp_path, monkeypatch, option, value):
    path = tmp_path / "repos.txt"
    path.write_text(SLUG + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ActivityBatchService", lambda **options: pytest.fail("Service must not be constructed"))
    result = CliRunner().invoke(app, ["collect-activity", "start", "--repos-file", str(path), option, value])
    assert result.exit_code == 2


@pytest.mark.parametrize("contents", ["", "bad-target", "https://user:synthetic-secret-provider-body@github.com/a/b"])
def test_cli_invalid_targets_and_empty_files_are_safe(tmp_path, monkeypatch, contents):
    path = tmp_path / "repos.txt"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(cli, "ActivityBatchService", lambda **options: pytest.fail("Unexpected construction"))
    result = CliRunner().invoke(app, ["collect-activity", "start", "--repos-file", str(path)])
    assert result.exit_code == 2 and SECRET not in result.output


@pytest.mark.parametrize("mode", ["partial", "failed"])
def test_cli_exit_one_for_incomplete_runs(environment, tmp_path, monkeypatch, mode):
    pages = {(SLUG, "commits", 1): (
        [native("commits", i) for i in range(100)] if mode == "partial" else httpx.Response(401, text=SECRET)
    )}
    service, client, _, _ = environment(pages)
    monkeypatch.setattr(cli, "ActivityBatchService", lambda **options: restart(service, client))
    path = tmp_path / "repos.txt"
    path.write_text(SLUG, encoding="utf-8")
    result = CliRunner().invoke(app, ["collect-activity", "start", "--repos-file", str(path),
                                    "--groups", "commits", "--max-pages", "1"])
    assert result.exit_code == 1 and f"Status: {mode}" in result.output
    assert SECRET not in result.output



def test_all_five_groups_persist_verified_context_and_original_payload(environment):
    pages = {(SLUG, group, 1): [native(group)] for group in ACTIVITY_GROUPS}
    service, _, calls, _ = environment(pages)
    result = service.start([SLUG], groups=ACTIVITY_GROUPS)
    assert result.status == "complete" and result.raw_stored_count == 5 and len(calls) == 6
    repository = validate_record(str(checkpoint(service, result.run_id).checkpoint_state["activity:repo:0:context"]))
    for group in ACTIVITY_GROUPS:
        records = raws(service, group)
        assert len(records) == 1 and records[0].payload == native(group)
        assert records[0].run_id == result.run_id and records[0].repo_id == repository.identity.repo_id
        assert records[0].available_at is None
    assert {entry["group"] for entry in result.groups} == set(ACTIVITY_GROUPS)


def test_group_storage_failure_isolated_and_resume_skips_completed_sibling(environment, monkeypatch):
    service, client, calls, _ = environment({
        (SLUG, "commits", 1): [native("commits")],
        (SLUG, "issues", 1): [native("issues")],
    })
    append = service.activity_service.raw_storage.append_contract_records_durable

    def fail_commits(path, records):
        if path.stem == "commits":
            raise OSError(SECRET)
        return append(path, records)

    with monkeypatch.context() as patch:
        patch.setattr(service.activity_service.raw_storage, "append_contract_records_durable", fail_commits)
        result = service.start([SLUG], groups=["commits", "issues"])
    assert result.status == "partial" and result.raw_stored_count == 1 and result.resumable
    assert result.groups[0]["status"] == "retryable_failure"
    assert result.groups[1]["status"] == "complete"
    before_calls = len(calls)
    recovered = restart(service, client).resume(result.run_id)
    assert recovered.status == "complete" and recovered.raw_stored_count == 2
    assert len(calls) == before_calls and not journals(service, result.run_id)


def test_metadata_interruption_before_verification_checkpoint_retries_only_metadata(environment, monkeypatch):
    service, client, calls, _ = environment()
    announced = []
    service.on_run_started = lambda run_id, path: announced.append(run_id)
    save = service.run_manager.save_checkpoint

    def fail_verification(context):
        if "activity:repo:0:context" in context.checkpoint_state:
            raise CollectionError("Injected verification checkpoint interruption")
        return save(context)

    with monkeypatch.context() as patch:
        patch.setattr(service.run_manager, "save_checkpoint", fail_verification)
        with pytest.raises(CollectionError):
            service.start([SLUG], groups=["commits"])
    assert len(calls) == 1
    context = checkpoint(service, announced[0])
    assert "activity:repo:0:context" not in context.checkpoint_state
    recovered = restart(service, client).resume(announced[0])
    assert recovered.status == "complete" and len(calls) == 3
    assert sum(not request.url.query for request in calls) == 2


@pytest.mark.parametrize("command", ["missing_file", "bad_run_id", "unknown_run_id"])
def test_cli_missing_file_and_resume_errors(environment, tmp_path, monkeypatch, command):
    service, client, calls, _ = environment()
    monkeypatch.setattr(cli, "ActivityBatchService", lambda **options: restart(service, client))
    if command == "missing_file":
        args = ["start", "--repos-file", str(tmp_path / "absent.txt")]
        expected = 2
    else:
        args = ["resume", "--run-id", "bad" if command == "bad_run_id" else str(uuid4())]
        expected = 2 if command == "bad_run_id" else 1
    result = CliRunner().invoke(app, ["collect-activity", *args])
    assert result.exit_code == expected and not calls



def test_raw_recovery_index_is_reused_across_pages(environment, monkeypatch):
    service, _, _, _ = environment({
        (SLUG, "commits", 1): [native("commits", i) for i in range(100)],
        (SLUG, "commits", 2): [native("commits", i) for i in range(100, 200)],
    })
    service.start([SLUG], groups=["commits"])
    destination = service.paths.raw_dir / "github" / "commits.jsonl"
    reads = []
    original_open = Path.open

    def capture(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == destination and mode == "r":
            reads.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", capture)
    result = service.start([SLUG], groups=["commits"])
    assert result.status == "complete" and result.raw_stored_count == 200
    assert reads == [destination]



def test_resume_isolates_recoverable_journal_storage_failure(environment, monkeypatch):
    service, client, calls, pages = environment({
        (SLUG, "commits", 1): [native("commits")],
        (SLUG, "issues", 1): httpx.Response(503, text=SECRET),
    })
    with monkeypatch.context() as patch:
        patch.setattr(service.activity_service.raw_storage, "append_contract_records_durable",
                      lambda path, records: (_ for _ in ()).throw(OSError(SECRET)))
        first = service.start([SLUG], groups=["commits", "issues"])
    pages[(SLUG, "issues", 1)] = [native("issues")]
    resumed_service = restart(service, client)
    append = resumed_service.activity_service.raw_storage.append_contract_records_durable

    def fail_commits(path, records):
        if path.stem == "commits":
            raise OSError(SECRET)
        return append(path, records)

    with monkeypatch.context() as patch:
        patch.setattr(resumed_service.activity_service.raw_storage, "append_contract_records_durable", fail_commits)
        second = resumed_service.resume(first.run_id)
    assert second.status == "partial" and second.raw_stored_count == 1
    assert second.groups[0]["status"] == "retryable_failure"
    assert second.groups[1]["status"] == "partial" and second.groups[1]["pagination_complete"]
    assert len(calls) == 4 and journals(service, first.run_id)
    third = restart(service, client).resume(first.run_id)
    assert third.raw_stored_count == 2 and len(calls) == 4
    assert not journals(service, first.run_id)



def test_rate_limit_timeout_and_storage_interruption_resume_to_strict_raw_replay(environment, monkeypatch):
    service, client, calls, pages = environment({
        (SLUG, "commits", 1): [native("commits", i) for i in range(100)],
        (SLUG, "commits", 2): httpx.Response(429, headers={"Retry-After": "0"}),
    })
    first = service.start([SLUG], groups=["commits"], max_pages_per_group=3)
    assert first.resumable and first.raw_stored_count == 100
    assert state(service, first.run_id)["cursor"] == "p:2:i:0"
    pages[(SLUG, "commits", 2)] = httpx.ReadTimeout("Injected timeout")
    timed_out = restart(service, client).resume(first.run_id)
    assert timed_out.resumable and timed_out.raw_stored_count == 100
    assert state(service, first.run_id)["error_count"] == 2
    pages[(SLUG, "commits", 2)] = [native("commits", 100)]
    interrupted = restart(service, client)
    append = interrupted.activity_service.raw_storage.append_contract_records_durable

    def interrupt(path, records):
        append(path, list(records)[:1])
        raise OSError("Injected after durable raw append")

    with monkeypatch.context() as patch:
        patch.setattr(interrupted.activity_service.raw_storage, "append_contract_records_durable", interrupt)
        pending = interrupted.resume(first.run_id)
    assert pending.resumable and journals(service, first.run_id)
    assert state(service, first.run_id)["records_stored"] == 100
    before = len(calls)
    final = restart(service, client).resume(first.run_id)
    assert len(calls) == before and not journals(service, first.run_id)
    assert final.raw_stored_count == 101 and not final.resumable
    assert final.status == "partial" and final.groups[0]["pagination_complete"]
    assert final.groups[0]["missing_reason"] == "collection_errors"
    replay = raws(service)
    assert_unique(replay)
    assert len(replay) == 101 and all(record.available_at is None for record in replay)
    assert all(record.run_id == first.run_id for record in replay)
