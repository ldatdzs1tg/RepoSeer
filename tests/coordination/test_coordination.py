"""Offline coordination contracts; production databases and Drive are never used."""

import copy
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from tests.integration import test_collection_pipeline as t020
from tools.coordination import _common as c
from tools.coordination import dispatcher as d
from tools.coordination import ingest as i
from tools.coordination import worker as w

from reposeer.contracts.identity import package_id
from reposeer.exceptions import HTTPClientError
from reposeer.pipelines.package_discovery import PackageDiscoveryOptions, PackageDiscoveryPipeline

environment = t020.environment


@pytest.fixture
def tmp_path(tmp_path_factory, monkeypatch):
    # Use a short --basetemp on Windows; route helper scratch files into pytest ownership.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.getbasetemp()))
    return tmp_path_factory.mktemp("rc")


class Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Registry:
    """Transactional metadata double; real locking SQL has a separate opt-in test."""

    def __init__(self):
        self.packages, self.jobs, self.repos, self.runs, self.artifacts = {}, {}, {}, {}, {}
        self.workers = {
            x: dict(
                worker_id=x,
                worker_role="discovery" if x == "D1" else "screening" if x == "D2" else "crawler",
                is_enabled=True,
            )
            for x in ["D1", "D2", *[f"C{n}" for n in range(1, 8)]]
        }
        self.readonly = False
        self.active_transactions = 0
        self.writer_active = False

    @contextmanager
    def connect(self, *, readonly=False):
        snapshot = copy.deepcopy(self.__dict__)
        self.readonly = readonly
        self.active_transactions += 1
        try:
            yield self
        except BaseException:
            self.__dict__.update(snapshot)
            raise
        finally:
            self.readonly = snapshot["readonly"]
            self.active_transactions = snapshot["active_transactions"]

    @contextmanager
    def writer_lock(self):
        c.require(not self.writer_active, "another_ingest_writer_is_active")
        self.writer_active = True
        try:
            yield
        finally:
            self.writer_active = False

    def execute(self, sql, args=()):
        sql = " ".join(sql.split())
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            assert not self.readonly
        if "pg_try_advisory_xact_lock" in sql:
            return Rows([dict(acquired=True)])
        if "pg_advisory_xact_lock" in sql:
            return Rows()
        if sql.startswith("SELECT"):
            if "FROM reposeer.workers" in sql:
                return Rows([self.workers[args[0]]])
            if "FROM reposeer.artifacts a JOIN" in sql:
                kind = "screening" if "c.run_kind='screening'" in sql else "integration"
                return Rows(
                    [
                        dict(
                            a,
                            **{
                                k: self.runs[a["run_id"]].get(k)
                                for k in ("job_id", "worker_id", "status")
                            },
                        )
                        for a in self.artifacts.values()
                        if a["artifact_kind"] == "bundle"
                        and a["status"] == "ingested"
                        and self.runs[a["run_id"]]["run_kind"] == kind
                        and (not args or a["storage_path"] == args[0])
                    ]
                )
            if "FROM reposeer.crawl_jobs j JOIN reposeer.crawl_runs" in sql:
                rows = [
                    dict(
                        a,
                        catalog_metadata=self.runs[a["run_id"]].get("catalog_metadata"),
                        repository_id=args[0],
                    )
                    for a in self.artifacts.values()
                    if a["artifact_kind"] == "bundle"
                    and a["status"] == "ingested"
                    and self.runs[a["run_id"]]["run_kind"] == "integration"
                    and self.jobs[self.runs[a["run_id"]]["job_id"]]["repository_id"] == args[0]
                    and (
                        (self.runs[a["run_id"]].get("catalog_metadata") or {}).get("github_repo_id")
                        is None
                        or str(self.runs[a["run_id"]]["catalog_metadata"]["github_repo_id"])
                        == args[1]
                    )
                ]
                rows.sort(
                    key=lambda a: (
                        self.runs[a["run_id"]].get("finished_at")
                        or c.timestamp("1970-01-01T00:00:00.000Z"),
                        a["run_id"],
                    ),
                    reverse=True,
                )
                return Rows(rows[args[3] : args[3] + args[2]])
            if "count(*) AS count FROM reposeer.crawl_runs" in sql:
                return Rows(
                    [
                        dict(
                            count=sum(
                                r["run_kind"] == "integration"
                                and (
                                    "catalog_metadata IS NULL" not in sql
                                    or r.get("catalog_metadata") is None
                                )
                                for r in self.runs.values()
                            )
                        )
                    ]
                )
            if "SELECT c.run_id,c.catalog_metadata" in sql:
                return Rows(
                    [
                        dict(run_id=k, catalog_metadata=r["catalog_metadata"])
                        for k, r in sorted(self.runs.items())
                        if r.get("catalog_metadata") is not None
                    ]
                )
            if "SELECT p.*,r.github_repo_id" in sql:
                return Rows(
                    [
                        dict(
                            p,
                            github_repo_id=self.repos[p["repository_id"]]["github_repo_id"],
                            repo_full_name=self.repos[p["repository_id"]]["repo_full_name"],
                        )
                        for p in sorted(self.packages.values(), key=lambda p: p["package_id"])
                        if p["mapping_status"] == "candidate"
                        and p.get("repository_id") in self.repos
                        and self.repos[p["repository_id"]].get("github_repo_id") is not None
                        and ("ANY" not in sql or p["package_id"] in args[0])
                    ][: args[-1]]
                )
            if "FROM reposeer.artifacts" in sql:
                return Rows([self.artifacts[args[0]]]) if args[0] in self.artifacts else Rows()
            if "FROM reposeer.crawl_runs" in sql:
                return Rows([self.runs[args[0]]]) if args[0] in self.runs else Rows()
            if "FROM reposeer.packages" in sql:
                if "repository_id=" in sql:
                    return Rows(
                        [p for p in self.packages.values() if p.get("repository_id") == args[0]][
                            :101
                        ]
                    )
                return Rows([self.packages[args[0]]]) if args[0] in self.packages else Rows()
            if "FROM reposeer.repositories" in sql:
                if "github_repo_id=" in sql:
                    return Rows(
                        [
                            r
                            for r in self.repos.values()
                            if r.get("github_repo_id") == args[0] and r["repository_id"] != args[1]
                        ]
                    )
                return Rows([self.repos[args[0]]])
            if "FROM reposeer.crawl_jobs" in sql:
                if "j.status IN" in sql:
                    return Rows(
                        sorted(
                            [
                                j
                                for j in self.jobs.values()
                                if j["job_kind"] == args[0]
                                and j["status"] in {"queued", "retry"}
                                and (
                                    j["scope_key"] == "initial"
                                    or j["job_kind"] == "collect_repository"
                                    and j["scope_key"] in c.COLLECTION_BUDGETS
                                )
                                and j["attempt_count"] < j["max_attempts"]
                                and (j.get("retry_after") is None or j["retry_after"] <= c.now())
                            ],
                            key=lambda j: (
                                -j.get("priority", 0),
                                list(c.COLLECTION_BUDGETS).index(j["scope_key"]),
                                j["job_id"],
                            ),
                        )[: args[1]]
                    )
                if "lease_expires_at<now()" in sql:
                    return Rows(
                        [
                            j
                            for j in self.jobs.values()
                            if j["status"] == "claimed" and j["lease_expires_at"] < c.now()
                        ]
                    )
                return Rows([self.jobs[args[0]]]) if args[0] in self.jobs else Rows()
        if sql.startswith("INSERT INTO reposeer.packages"):
            if args[0] in self.packages:
                return Rows()
            self.packages[args[0]] = dict(
                package_id=args[0], mapping_status="not_checked", repository_id=None
            )
            return Rows([self.packages[args[0]]])
        if sql.startswith("INSERT INTO reposeer.repositories"):
            repo = next(
                (r for r in self.repos.values() if r["repo_full_name"].lower() == args[0].lower()),
                None,
            )
            if repo is None:
                identity = len(self.repos) + 1
                repo = self.repos[identity] = dict(
                    repository_id=identity, repo_full_name=args[0], github_repo_id=None
                )
            return Rows([repo])
        if sql.startswith("INSERT INTO reposeer.crawl_jobs"):
            kind = "screen_package" if "screen_package" in sql else "collect_repository"
            key = "package_id" if kind == "screen_package" else "repository_id"
            scope = args[1] if "scope_key,priority" in sql else "initial"
            if any(
                j["job_kind"] == kind and j.get(key) == args[0] and j["scope_key"] == scope
                for j in self.jobs.values()
            ):
                return Rows()
            identity = str(uuid4())
            self.jobs[identity] = dict(
                job_id=identity,
                job_kind=kind,
                **{key: args[0]},
                status="queued",
                attempt_count=0,
                max_attempts=3,
                scope_key=scope,
                screen_assignment_id=None,
                priority=args[2] if "scope_key,priority" in sql else 0,
                repo_full_name=None
                if key == "package_id"
                else self.repos[args[0]]["repo_full_name"],
            )
            return Rows([self.jobs[identity]])
        if sql.startswith("INSERT INTO reposeer.crawl_runs"):
            if args[0] not in self.runs:
                if "'sampling'" in sql:
                    self.runs[args[0]] = dict(run_id=args[0], worker_id="D1", run_kind="sampling")
                elif "'screening'" in sql:
                    self.runs[args[0]] = dict(run_id=args[0], worker_id="D2", run_kind="screening")
                else:
                    self.runs[args[0]] = dict(
                        run_id=args[0],
                        worker_id=args[1],
                        job_id=args[2],
                        run_kind="integration",
                        status=args[3],
                        catalog_metadata=args[7].obj,
                    )
            return Rows()
        if sql.startswith("UPDATE reposeer.crawl_runs"):
            row = self.runs[args[1]]
            if row.get("catalog_metadata") is None:
                row["catalog_metadata"] = args[0].obj
            return Rows()
        if sql.startswith("INSERT INTO reposeer.artifacts"):
            self.artifacts[args[2]] = dict(
                run_id=args[0],
                artifact_kind=args[1],
                storage_path=args[2],
                sha256=args[3],
                byte_size=args[4],
                status="ingested",
                ingested_at=c.now(),
            )
            return Rows([self.artifacts[args[2]]])
        if sql.startswith("UPDATE reposeer.crawl_jobs"):
            job = self.jobs[args[-1]]
            if "status='claimed'" in sql:
                job.update(
                    status="claimed",
                    claimed_by=args[0],
                    claim_token=args[1],
                    lease_expires_at=args[2],
                    assignment_targets=args[3].obj,
                    assignment_sha256=args[4],
                    screen_assignment_id=args[5],
                    attempt_count=job["attempt_count"] + 1,
                )
            elif "SET status=%s" in sql:
                job.update(status=args[0], retry_after=args[1], last_error_code=args[2])
            elif "SET claimed_by=NULL" in sql:
                job.update(
                    claimed_by=None,
                    claim_token=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    screen_assignment_id=None,
                )
            elif "last_error_code=%s" in sql:
                job["last_error_code"] = args[0]
            return Rows()
        if sql.startswith("UPDATE reposeer.packages"):
            if "last_screening_run_id=" in sql:
                self.packages[args[1]]["last_screening_run_id"] = args[0]
            elif "mapping_status=%s" in sql:
                self.packages[args[-1]].update(
                    mapping_status=args[0],
                    mapping_evidence=args[1].obj,
                    mapping_verified_at=args[2],
                )
            elif "mapping_status='verified_current'" in sql:
                self.packages[args[1]].update(
                    mapping_status="verified_current", mapping_evidence=args[0].obj
                )
            elif "SET repository_id=" in sql:
                self.packages[args[-1]].update(
                    repository_id=args[0],
                    candidate_repo_full_name=args[1],
                    mapping_status="candidate",
                )
            else:
                self.packages[args[-1]]["mapping_status"] = "unresolved"
            return Rows()
        if sql.startswith("UPDATE reposeer.repositories"):
            self.repos[args[-1]]["github_repo_id"] = args[0]
            if "activity_coverage_status=" in sql:
                self.repos[args[-1]].update(
                    activity_coverage_status=args[2], activity_coverage_json=args[3].obj
                )
            return Rows()
        if sql.startswith("UPDATE reposeer.workers"):
            return Rows()
        raise AssertionError(sql)


class Drive(c.Drive):
    """Real transfer validation with a filesystem-backed fake rclone process."""

    def __init__(self, root):
        super().__init__("testremote:")
        # Keep virtual Drive storage on short local TEMP paths (Windows Drive Stream
        # rejects long physical paths even though remote UUID paths are valid).
        self.scratch = tempfile.TemporaryDirectory(prefix="rd-")
        self.root, self.writes, self.fail = Path(self.scratch.name), [], None

    def local(self, path):
        return (
            self.root / path.removeprefix("testremote:")
            if path.startswith("testremote:")
            else Path(path)
        )

    def _run(self, args, *, missing_ok=False):
        if args[0] in {"version", "lsd"}:
            return ""
        if args[0] == "lsjson":
            path = self.local(args[1])
            return (
                json.dumps(
                    [
                        dict(
                            Path=p.relative_to(path).as_posix(), Size=p.stat().st_size, IsDir=False
                        )
                        for p in (path.rglob("*") if "--recursive" in args else path.iterdir())
                        if (p.is_dir() if "--dirs-only" in args else p.is_file())
                    ]
                )
                if path.exists()
                else None
            )
        if args[0] == "deletefile":
            self.local(args[1]).unlink()
            return ""
        assert args[0] == "copyto"
        source, target = self.local(args[1]), self.local(args[2])
        if args[2].startswith("testremote:"):
            if self.fail and self.fail in args[2]:
                raise c.CoordinationError("upload_checksum_mismatch")
            self.writes.append(args[2])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return ""


def assignment(packages=("pypi:flask",), kind="collect_repository"):
    value = dict(
        schema_version=1,
        kind=kind,
        worker_id="D2" if kind == "screen_package" else "C1",
        lease_expires_at=c.format_utc_timestamp(c.now() + timedelta(hours=48)),
    )
    if kind == "screen_package":
        value.update(
            assignment_id=str(uuid4()),
            jobs=[
                dict(job_id=str(uuid4()), package_id=p, claim_token=str(uuid4())) for p in packages
            ],
        )
    else:
        value.update(
            job_id=str(uuid4()),
            claim_token=str(uuid4()),
            scope_key="initial",
            expected_repo_full_name="pallets/flask",
            targets=list(packages),
            collection=c.COLLECTION,
        )
    return c.sign_assignment(value)


def claim(registry, value):
    for j in value.get("jobs", [value]):
        packages = [j["package_id"]] if value["kind"] == "screen_package" else value["targets"]
        registry.jobs[j["job_id"]] = dict(
            job_id=j["job_id"],
            job_kind=value["kind"],
            package_id=j.get("package_id"),
            status="claimed",
            claimed_by=value["worker_id"],
            claim_token=j["claim_token"],
            assignment_sha256=value["assignment_sha256"],
            assignment_targets=packages,
            lease_expires_at=c.timestamp(value["lease_expires_at"]),
            attempt_count=1,
            max_attempts=3,
            scope_key=value.get("scope_key", "initial"),
            priority=0,
            screen_assignment_id=value.get("assignment_id"),
            repo_full_name=value.get("expected_repo_full_name"),
            repository_id=None if value["kind"] == "screen_package" else 1,
        )
        for p in packages:
            registry.packages[p] = dict(
                package_id=p,
                mapping_status="candidate",
                repository_id=None if value["kind"] == "screen_package" else 1,
            )
    if value["kind"] == "collect_repository":
        registry.repos[1] = dict(
            repository_id=1, repo_full_name=value["expected_repo_full_name"], github_repo_id=None
        )


def stored_partial(
    tmp_path, environment, monkeypatch, *, unresolved=False, scope="initial", budget_limited=False
):
    pipeline, paths, _, options, _ = environment
    options["fail_provider"] = True
    if unresolved:
        options["mapping"] = "unresolved"
    options["issues_prs"] = budget_limited
    budgets = c.collection_options(scope)
    result = pipeline.run(
        ["pypi:flask"], max_pages=budgets["max_pages"], max_records=budgets["max_records"]
    )
    assert result.status != "complete"
    value = c.sign_assignment(dict(assignment(), scope_key=scope, collection=budgets))
    assignment_path = tmp_path / "assignment.json"
    c.write_json(assignment_path, value)
    drive = Drive(tmp_path / "remote")
    monkeypatch.setattr(w, "Drive", lambda remote: drive)
    monkeypatch.setattr(i, "Drive", lambda remote: drive)
    calls = []

    def runner(command, *, stdout, **kwargs):
        calls.append(command)
        destination = Path(command[command.index("--data-dir") + 1])
        shutil.copytree(paths.base_dir, destination, dirs_exist_ok=True)
        stdout.write(f"Integration run ID: {result.integration_run_id}\n".encode())
        return subprocess.CompletedProcess(command, 1)

    return value, assignment_path, drive, runner, calls


def test_discovery_validation_and_idempotent_import(tmp_path):
    assert {package_id(n) for n in ["Foo_Bar", "foo.bar", "FOO-BAR"]} == {"pypi:foo-bar"}
    source = tmp_path / "names.txt"
    source.write_text("Foo_Bar\nfoo.bar\nFOO-BAR\n", encoding="utf-8")
    result = PackageDiscoveryPipeline().run(
        PackageDiscoveryOptions("file", 1, tmp_path / "data", input_file=source)
    )
    registry = Registry()
    first = d.import_discovery(result.manifest_path.parent, registry=registry, remote="testremote:")
    second = d.import_discovery(
        result.manifest_path.parent, registry=registry, remote="testremote:"
    )
    assert (
        first["packages_inserted"]
        == first["screen_jobs_created"]
        == second["duplicates_skipped"]
        == 1
    )
    assert len(registry.packages) == len(registry.jobs) == 1
    before = copy.deepcopy(registry.__dict__)
    csv_path = result.candidates_path
    original = csv_path.read_bytes()
    csv_path.write_bytes(original + b"tampered")
    with pytest.raises(c.CoordinationError, match="checksum"):
        d.import_discovery(result.manifest_path.parent, registry=registry)
    assert before == registry.__dict__
    csv_path.write_bytes(original)
    manifest = c.read_json(result.manifest_path)
    manifest["sampling_run_id"] = "../invalid"
    c.write_json(result.manifest_path, manifest, immutable=False)
    with pytest.raises(c.CoordinationError, match="uuid"):
        d.import_discovery(result.manifest_path.parent, registry=registry)
    assert before == registry.__dict__


def test_screening_reuses_source_policy_and_deduplicates_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value = assignment(
        [
            "pypi:foo-bar",
            "pypi:baz",
            "pypi:home-only",
            "pypi:conflict",
            "pypi:network",
            "pypi:previously-verified",
        ],
        kind="screen_package",
    )
    path = tmp_path / "assignment.json"
    c.write_json(path, value)
    drive = Drive(tmp_path / "remote")
    monkeypatch.setattr(d, "Drive", lambda remote: drive)

    class PyPI:
        def get_package_info(self, name):
            if name == "network":
                raise HTTPClientError("private-provider-body", status_code=429)
            urls = {"Source": "https://github.com/Pallets/Flask"}
            if name == "home-only":
                urls = {"Home": "https://github.com/pallets/flask"}
            if name == "conflict":
                urls["Code"] = "https://github.com/other/repository"
            return {"info": {"name": name, "project_urls": urls}}

    result = d.screen_local(path, tmp_path / "screening", client=PyPI())
    registry = Registry()
    claim(registry, value)
    first_job = value["jobs"][0]
    registry.jobs[first_job["job_id"]]["package_id"] = "pypi:unrelated"
    with pytest.raises(c.CoordinationError, match="assignment_db_package_mismatch"):
        d.import_screening(result["output"], registry=registry, remote="testremote:")
    assert not registry.runs and not registry.artifacts
    registry.jobs[first_job["job_id"]]["package_id"] = first_job["package_id"]
    registry.repos[1] = dict(repository_id=1, repo_full_name="already/verified", github_repo_id=123)
    registry.packages["pypi:previously-verified"].update(
        mapping_status="verified_current",
        repository_id=1,
        candidate_repo_full_name="already/verified",
    )
    original_mapping = copy.deepcopy(registry.packages["pypi:previously-verified"])
    imported = d.import_screening(result["output"], registry=registry, remote="testremote:")
    assert imported["jobs_queued"] == 1 and imported["jobs_retry"] == 1
    assert len(registry.repos) == 2
    assert sum(r["repo_full_name"] == "pallets/flask" for r in registry.repos.values()) == 1
    assert all(
        registry.packages["pypi:previously-verified"][k] == v for k, v in original_mapping.items()
    )
    assert (
        registry.packages["pypi:previously-verified"]["last_screening_run_id"]
        == value["assignment_id"]
    )
    assert len([j for j in registry.jobs.values() if j["job_kind"] == "collect_repository"]) == 1
    assert registry.packages["pypi:home-only"]["mapping_status"] == "unresolved"
    assert registry.packages["pypi:conflict"]["mapping_status"] == "unresolved"
    assert registry.packages["pypi:foo-bar"]["mapping_status"] == "candidate"
    assert drive.writes[-1].endswith("/READY.json")
    before = copy.deepcopy(registry.__dict__)
    assert (
        d.import_screening(result["output"], registry=registry, remote="testremote:")[
            "duplicates_skipped"
        ]
        == 6
    )
    assert registry.__dict__ == before


def test_dispatch_upload_recovery_and_disabled_workers(tmp_path, monkeypatch):
    registry, drive = Registry(), Drive(tmp_path / "remote")
    registry.execute(
        "INSERT INTO reposeer.crawl_jobs(job_kind,package_id) VALUES ('screen_package',%s)",
        ("pypi:demo",),
    )
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    drive.fail = "assignments"
    with pytest.raises(c.CoordinationError, match="upload"):
        d.dispatch("screen_package", "D2", 100, tmp_path / "out", registry=registry)
    job = next(iter(registry.jobs.values()))
    assert job["status"] == "claimed" and job["attempt_count"] == 1
    token = job["claim_token"]
    drive.fail = None
    registry.workers["D2"]["is_enabled"] = False
    with pytest.raises(c.CoordinationError, match="disabled"):
        d.dispatch("screen_package", "D2", 100, tmp_path / "out", registry=registry)
    registry.workers["D2"]["is_enabled"] = True
    recovered = d.dispatch("screen_package", "D2", 100, tmp_path / "out", registry=registry)
    assert (
        recovered["recovered_assignments"]
        and job["claim_token"] == token
        and job["attempt_count"] == 1
    )
    registry.workers["D2"]["is_enabled"] = False
    with pytest.raises(c.CoordinationError, match="disabled"):
        d.dispatch("screen_package", "D2", 100, tmp_path / "out", registry=registry)


def test_partial_exit_one_is_bundled_and_upload_retries_without_api(
    tmp_path, environment, monkeypatch
):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, calls = stored_partial(tmp_path, environment, monkeypatch)
    drive.fail = "crawl/submissions"
    with pytest.raises(c.CoordinationError, match="upload"):
        w.run("C1", path, tmp_path / "work", runner=runner)
    root = tmp_path / "work" / value["job_id"] / value["claim_token"]
    raw = (root / "raw/github/repositories.jsonl").read_bytes()
    receipt = c.read_json(root / "COORDINATION_RECEIPT.json")
    assert (
        receipt["overall_status"] == "partial" and receipt["verified_github_repo_id"] == 900000001
    )
    assert receipt["collected_repository"] and receipt["verified_package_ids"] == ["pypi:flask"]
    drive.fail = "crawl/inbox"
    with pytest.raises(c.CoordinationError, match="upload"):
        w.run("C1", path, tmp_path / "work", runner=runner)
    assert len(calls) == 1
    drive.fail = None
    w.run("C1", path, tmp_path / "work", runner=runner)
    assert len(calls) == 1 and raw == (root / "raw/github/repositories.jsonl").read_bytes()
    assert "crawl/inbox/" in drive.writes[-1]
    assert "--skip-external" not in calls[0]


def test_ingestion_replay_and_versioned_catalog(tmp_path, environment, monkeypatch):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, _ = stored_partial(tmp_path, environment, monkeypatch)
    w.run("C1", path, tmp_path / "work", runner=runner)
    registry = Registry()
    claim(registry, value)
    transport = drive._run

    def outside_transaction(args, **kwargs):
        assert registry.active_transactions == 0
        return transport(args, **kwargs)

    monkeypatch.setattr(drive, "_run", outside_transaction)
    root = tmp_path / "work" / value["job_id"] / value["claim_token"]
    drive.fail = "canonical/runs"
    with pytest.raises(c.CoordinationError):
        i.ingest_one(root, drive, registry)
    assert (
        not registry.runs
        and not registry.artifacts
        and registry.jobs[value["job_id"]]["status"] == "claimed"
    )
    drive.fail = None
    first = i.ingest_one(root, drive, registry)
    assert (
        first["job_collected"]
        and first["job_status"] == "completed"
        and first["overall_status"] == "partial"
    )
    assert first["followup_scope"] is None
    assert len(registry.jobs) == 1  # External-only partial does not create activity followups.
    assert registry.packages["pypi:flask"]["mapping_status"] == "verified_current"
    assert i.ingest_one(root, drive, registry)["duplicate"]
    assert len(registry.runs) == 1 and len(registry.artifacts) == 4

    def sql_catalog_only(args, **kwargs):
        assert not (args[0] == "copyto" and args[1].startswith("testremote:canonical/runs/"))
        assert not (args[0] == "lsjson" and args[1] == "testremote:crawl/submissions")
        return outside_transaction(args, **kwargs)

    monkeypatch.setattr(drive, "_run", sql_catalog_only)
    registry.packages["pypi:later"] = dict(
        package_id="pypi:later", repository_id=1, mapping_status="candidate"
    )
    drive.fail = "canonical/catalog/active.json"
    with pytest.raises(c.CoordinationError, match="upload"):
        i.rebuild_catalog(drive, registry)
    assert len(registry.runs) == 1 and registry.jobs[value["job_id"]]["status"] == "completed"
    assert not (drive.root / "canonical/catalog/active.json").exists()
    drive.fail = None
    assert i.rebuild_catalog(drive, registry) == 1
    original = c.files(drive.root / "canonical")
    assert i.rebuild_catalog(drive, registry) == 1 and c.files(drive.root / "canonical") == original
    drive.fail = "crawl/inbox-processed"
    archive_retry = i.run("testremote:", tmp_path / "central", registry=registry)
    assert archive_retry["retryable"] and drive.listing("crawl/inbox")
    assert len(registry.runs) == 1
    drive.fail = None
    replay = i.run("testremote:", tmp_path / "central", registry=registry)
    assert replay["catalog_rows"] is None and not replay["pending"]
    assert replay["accepted"][0]["duplicate"]
    assert not drive.listing("crawl/inbox")
    marker_path = (
        drive.root
        / "crawl/submissions/C1"
        / value["job_id"]
        / first["integration_run_id"]
        / "READY.json"
    )
    marker = c.read_json(marker_path)
    marker["payload_sha256"] = "0" * 64
    c.write_json(marker_path, marker, immutable=False)
    # Replayed notification with tampered READY is rejected without DB mutation.
    processed = (
        drive.root
        / "crawl/inbox-processed/C1"
        / value["job_id"]
        / (first["integration_run_id"] + ".json")
    )
    pending = drive.root / "crawl/inbox/C1" / value["job_id"] / processed.name
    pending.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(processed, pending)
    before = copy.deepcopy(registry.__dict__)
    checked = i.run("testremote:", tmp_path / "central", registry=registry, dry_run=True)
    assert checked["rejected"][0]["error_code"] == "notification_ready_mismatch"
    assert registry.__dict__ == before
    quarantined = i.run("testremote:", tmp_path / "central", registry=registry)
    assert quarantined["rejected"] and not drive.listing("crawl/inbox")
    assert drive.listing("crawl/inbox-quarantine")
    assert list((tmp_path / "central/quarantine").glob("*.json"))
    marker["payload_sha256"] = c.read_json(processed)["ready_payload_sha256"]
    c.write_json(marker_path, marker, immutable=False)
    quarantined_notice = drive.root / "crawl/inbox-quarantine/C1" / value["job_id"] / processed.name
    pending.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(quarantined_notice, pending)
    assert i.run("testremote:", tmp_path / "central", registry=registry)["accepted"][0]["duplicate"]
    # Explicit legacy metadata backfill validates the saved run, then progresses.
    run = first["integration_run_id"]
    registry.runs[run]["catalog_metadata"] = None
    with pytest.raises(c.CoordinationError, match="legacy_catalog_metadata_missing"):
        i.rebuild_catalog(drive, registry)
    monkeypatch.setattr(drive, "_run", outside_transaction)
    preview = i.scan_legacy("testremote:", tmp_path / "central", limit=1, registry=registry)
    assert preview["dry_run"] and len(preview["notifications"]) == 1
    i.scan_legacy("testremote:", tmp_path / "central", limit=1, apply=True, registry=registry)
    assert registry.runs[run]["catalog_metadata"]["package_ids"] == ["pypi:flask"]
    assert not i.scan_legacy("testremote:", tmp_path / "central", limit=1, registry=registry)[
        "notifications"
    ]
    pointer = c.read_json(drive.root / "canonical/catalog/active.json")
    assert c.fingerprint(drive.root / pointer["relative_path"])[0] == pointer["sha256"]


def test_unverified_partial_is_accepted_without_completing_job(tmp_path, environment, monkeypatch):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, _ = stored_partial(
        tmp_path, environment, monkeypatch, unresolved=True
    )
    w.run("C1", path, tmp_path / "work", runner=runner)
    registry = Registry()
    claim(registry, value)
    result = i.ingest_one(
        tmp_path / "work" / value["job_id"] / value["claim_token"], drive, registry
    )
    assert (
        result["artifact_accepted"]
        and not result["job_collected"]
        and result["job_status"] == "retry"
    )
    assert registry.packages["pypi:flask"]["mapping_status"] == "candidate"
    assert registry.repos[1]["github_repo_id"] is None
    assert len(registry.jobs) == 1


def test_invalid_bundles_and_stale_claims_cannot_complete_jobs(tmp_path, environment, monkeypatch):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, _ = stored_partial(tmp_path, environment, monkeypatch)
    w.run("C1", path, tmp_path / "work", runner=runner)
    root = tmp_path / "work" / value["job_id"] / value["claim_token"]
    registry = Registry()
    claim(registry, value)
    registry.repos[2] = dict(
        repository_id=2, repo_full_name="alias/flask", github_repo_id=900000001
    )
    with pytest.raises(c.CoordinationError, match="github_numeric_id_collision_quarantine"):
        i.ingest_one(root, drive, registry)
    assert not registry.runs and not registry.artifacts
    del registry.repos[2]
    registry.jobs[value["job_id"]]["claim_token"] = str(uuid4())
    with pytest.raises(c.CoordinationError, match="stale_claim"):
        i.ingest_one(root, drive, registry)
    assert not registry.runs and not registry.artifacts
    with pytest.raises(c.CoordinationError, match="worker_mismatch"):
        i.validate_submission(root, worker="C2")
    raw = root / "raw/github/repositories.jsonl"
    original = raw.read_bytes()
    raw.write_bytes(original + b"changed")
    with pytest.raises(c.CoordinationError, match="checksum"):
        i.ingest_one(root, drive, registry)
    raw.write_bytes(original)
    (root / "READY.json").unlink()
    with pytest.raises(OSError):
        i.ingest_one(root, drive, registry)
    assert not registry.runs and registry.jobs[value["job_id"]]["status"] == "claimed"


def test_paths_assignments_and_credentials_fail_closed(tmp_path, monkeypatch):
    for path in ["../x", "/absolute", "C:/x", "x\\y", "a/../x", ".env", "a/.git/config"]:
        with pytest.raises(c.CoordinationError):
            c.safe_path(tmp_path, path)
    value = assignment()
    for changes in [
        dict(kind="shell"),
        dict(targets=["pypi:Foo_Bar"]),
        dict(targets=["https://evil.example/"]),
        dict(expected_repo_full_name="owner/repo;start-process"),
    ]:
        with pytest.raises((ValueError, TypeError)):
            c.validate_assignment(c.sign_assignment(dict(value, **changes)))
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-1234")
    assert "test-secret-1234" not in c.redact("GITHUB_TOKEN=test-secret-1234 Bearer abcdef")
    monkeypatch.setenv("REPOSEER_REGISTRY_DSN", "not-a-real-dsn")
    with pytest.raises(c.CoordinationError, match="must_not_have_registry_dsn"):
        w.run("C1", tmp_path / "absent.json", tmp_path / "work", dry_run=True)
    assert not (tmp_path / "work").exists()


def test_requeue_defaults_readonly_and_refuses_unknown_drive_state(tmp_path, monkeypatch):
    registry, drive = Registry(), Drive(tmp_path / "remote")
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    listing = drive.listing
    calls = []

    def targeted(prefix):
        assert registry.active_transactions == 0
        assert prefix not in {"crawl/submissions", "screening/submissions"}
        calls.append(prefix)
        return listing(prefix)

    monkeypatch.setattr(drive, "listing", targeted)
    irrelevant = drive.root / "crawl/submissions/C7/unrelated"
    irrelevant.mkdir(parents=True)
    for n in range(100):
        (irrelevant / str(n)).write_bytes(b"x")
    assert not d.requeue_expired(registry=registry)["jobs_requeued"]
    assert not calls
    value = c.sign_assignment(
        dict(assignment(), lease_expires_at=c.format_utc_timestamp(c.now() - timedelta(hours=1)))
    )
    claim(registry, value)
    before = copy.deepcopy(registry.__dict__)
    preview = d.requeue_expired(pending_dir=tmp_path / "empty", registry=registry)
    assert preview["dry_run"] and preview["jobs_requeued"] == [value["job_id"]]
    assert registry.__dict__ == before

    def unavailable(prefix):
        assert registry.active_transactions == 0
        raise c.CoordinationError("drive_unavailable")

    monkeypatch.setattr(drive, "listing", unavailable)
    with pytest.raises(c.CoordinationError):
        d.requeue_expired(apply=True, pending_dir=tmp_path / "empty", registry=registry)
    assert registry.__dict__ == before
    monkeypatch.setattr(drive, "listing", targeted)
    applied = d.requeue_expired(apply=True, pending_dir=tmp_path / "empty", registry=registry)
    job = registry.jobs[value["job_id"]]
    assert applied["jobs_requeued"] == [value["job_id"]] and job["status"] == "retry"
    assert job["claim_token"] is job["claimed_by"] is job["screen_assignment_id"] is None
    assert job["attempt_count"] == 1 and job["retry_after"] > c.now()
    assert not d.requeue_expired(apply=True, pending_dir=tmp_path / "empty", registry=registry)[
        "jobs_requeued"
    ]


@pytest.mark.parametrize(
    "kind,upload,protected",
    [
        ("collect_repository", "current", True),
        ("collect_repository", "unknown", True),
        ("collect_repository", "old", False),
        ("collect_repository", "local", True),
        ("screen_package", "current", True),
        ("screen_package", "empty", False),
        ("screen_package", "legacy", True),
        ("screen_package", "old", False),
    ],
)
def test_requeue_targeted_claim_submissions(tmp_path, monkeypatch, kind, upload, protected):
    registry, drive = Registry(), Drive(tmp_path / "remote")
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    value = c.sign_assignment(
        dict(
            assignment(kind=kind),
            lease_expires_at=c.format_utc_timestamp(c.now() - timedelta(hours=1)),
        )
    )
    claim(registry, value)
    item = value.get("jobs", [value])[0]
    if kind == "screen_package":
        prefix = f"screening/submissions/D2/{value['assignment_id']}"
    else:
        prefix = f"crawl/submissions/C1/{item['job_id']}/{uuid4()}"
    if upload in {"current", "old", "unknown"}:
        if upload == "old":
            old = copy.deepcopy(value)
            old.get("jobs", [old])[0]["claim_token"] = str(uuid4())
            uploaded = c.sign_assignment(old)
        else:
            uploaded = value
        c.write_json(
            drive.root / prefix / ("other.json" if upload == "unknown" else "ASSIGNMENT.json"),
            uploaded,
        )
    if upload == "legacy":
        registry.jobs[item["job_id"]]["screen_assignment_id"] = None
    if upload == "local":
        c.write_json(tmp_path / "pending/.pending/assignment.json", value)
    listing = drive.listing

    def targeted(path):
        assert registry.active_transactions == 0
        assert path not in {"crawl/submissions", "screening/submissions"}
        expected = prefix if kind == "screen_package" else prefix.rsplit("/", 1)[0]
        assert path == expected
        return listing(path)

    monkeypatch.setattr(drive, "listing", targeted)
    result = d.requeue_expired(apply=True, pending_dir=tmp_path / "pending", registry=registry)
    assert (item["job_id"] in result["protected_jobs"]) == protected
    assert (registry.jobs[item["job_id"]]["status"] == "claimed") == protected
    assert registry.jobs[item["job_id"]]["attempt_count"] == 1


def test_requeue_concurrent_claim_change_and_malformed_assignment(tmp_path, monkeypatch):
    registry, drive = Registry(), Drive(tmp_path / "remote")
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    value = c.sign_assignment(
        dict(assignment(), lease_expires_at=c.format_utc_timestamp(c.now() - timedelta(hours=1)))
    )
    claim(registry, value)
    job = registry.jobs[value["job_id"]]
    newer = str(uuid4())

    def race(prefix):
        assert registry.active_transactions == 0
        job["claim_token"] = newer
        return {}

    monkeypatch.setattr(drive, "listing", race)
    result = d.requeue_expired(apply=True, pending_dir=tmp_path / "empty", registry=registry)
    assert (
        not result["jobs_requeued"]
        and result["protection_reasons"][value["job_id"]] == "claim_changed_or_locked"
    )
    assert job["status"] == "claimed" and job["claim_token"] == newer
    monkeypatch.setattr(drive, "listing", Drive.listing.__get__(drive))
    c.atomic_bytes(
        drive.root / f"crawl/submissions/C1/{value['job_id']}/{uuid4()}/ASSIGNMENT.json",
        b"not JSON",
    )
    before = copy.deepcopy(registry.__dict__)
    with pytest.raises(ValueError):
        d.requeue_expired(apply=True, pending_dir=tmp_path / "empty", registry=registry)
    assert registry.__dict__ == before


def test_requeue_screen_assignment_schema_and_dispatch(tmp_path, monkeypatch):
    assert "screen_assignment_id" in c.SCHEMA["crawl_jobs"].split()
    migration = Path("supabase/migrations/20261021000300_coordination_followups.sql")
    assert not migration.read_bytes().startswith(b"\xef\xbb\xbf")
    assert migration.read_text(encoding="utf-8").startswith("--")
    assert "ADD COLUMN IF NOT EXISTS screen_assignment_id uuid" in migration.read_text(
        encoding="utf-8"
    )
    registry, drive = Registry(), Drive(tmp_path / "remote")
    value = assignment(["pypi:a", "pypi:b"], kind="screen_package")
    claim(registry, value)
    for job in registry.jobs.values():
        job.update(status="queued", attempt_count=0)
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    dispatched = d.dispatch("screen_package", "D2", 100, tmp_path / "out", registry=registry)
    batch = c.read_json(dispatched["assignments"][0])["assignment_id"]
    assert all(j["screen_assignment_id"] == batch for j in registry.jobs.values())


def test_postgresql_competing_transactions_skip_locked(monkeypatch):
    dsn = os.environ.get("REPOSEER_COORDINATION_TEST_DSN")
    if not dsn:
        pytest.skip("requires a dedicated localhost PostgreSQL test database")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row

    info = conninfo_to_dict(dsn)
    c.require(
        info.get("host", "localhost") in {"localhost", "127.0.0.1", "::1"},
        "test_database_must_be_local",
    )
    c.require(
        info.get("dbname", "").startswith("reposeer_coordination_test"),
        "dedicated_test_database_required",
    )
    root = Path(__file__).resolve().parents[2]
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5) as setup:
        for role in ["anon", "authenticated"]:
            if not setup.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                setup.execute(
                    psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(psycopg.sql.Identifier(role))
                )
        for path in sorted((root / "supabase/migrations").glob("*.sql")):
            setup.execute(path.read_text(encoding="utf-8-sig"))
        # Verify additive migration repeatability on this disposable local DB only.
        setup.execute(
            (root / "supabase/migrations/20261021000300_coordination_followups.sql").read_text(
                encoding="utf-8-sig"
            )
        )
        monkeypatch.setenv("REPOSEER_REGISTRY_DSN", dsn)
        registry = c.Registry()
        with registry.connect(readonly=True):
            pass
        with (
            registry.writer_lock(),
            pytest.raises(c.CoordinationError, match="another_ingest_writer"),
        ):
            with registry.writer_lock():
                pass
        key = "pypi:coord-test-" + uuid4().hex
        setup.execute("INSERT INTO reposeer.packages(package_id) VALUES (%s)", (key,))
        job = setup.execute(
            "INSERT INTO reposeer.crawl_jobs(job_kind,package_id,priority) VALUES ('screen_package',%s,2147483647) RETURNING job_id",
            (key,),
        ).fetchone()["job_id"]
        repo = None
        run = str(uuid4())
        try:
            numeric = uuid4().int % (2**63 - 1) + 1
            repo = setup.execute(
                "INSERT INTO reposeer.repositories(repo_full_name,github_repo_id) VALUES (%s,%s) RETURNING repository_id",
                ("coord-test/" + uuid4().hex, numeric),
            ).fetchone()["repository_id"]
            for scope in c.COLLECTION_BUDGETS:
                statement = "INSERT INTO reposeer.crawl_jobs(job_kind,repository_id,scope_key) VALUES ('collect_repository',%s,%s) ON CONFLICT DO NOTHING RETURNING job_id"
                assert setup.execute(statement, (repo, scope)).fetchone()
                assert setup.execute(statement, (repo, scope)).fetchone() is None
            with pytest.raises(psycopg.errors.UniqueViolation), setup.transaction():
                setup.execute(
                    "INSERT INTO reposeer.repositories(repo_full_name,github_repo_id) VALUES (%s,%s)",
                    ("coord-test/" + uuid4().hex, numeric),
                )
            setup.execute(
                "INSERT INTO reposeer.crawl_runs(run_id,run_kind,status,catalog_metadata) VALUES (%s,'integration','complete','{}'::jsonb)",
                (run,),
            )
            with pytest.raises(psycopg.errors.RaiseException), setup.transaction():
                setup.execute(
                    "UPDATE reposeer.crawl_runs SET catalog_metadata='{}'::jsonb || jsonb_build_object('changed',true) WHERE run_id=%s",
                    (run,),
                )
            with (
                psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as one,
                psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as two,
            ):
                with one.transaction():
                    first = one.execute(d.CLAIM_SQL, ("screen_package", 1)).fetchall()
                    assert first[0]["job_id"] == job
                    with two.transaction():
                        second = two.execute(d.CLAIM_SQL, ("screen_package", 1)).fetchall()
                        assert all(row["job_id"] != job for row in second)
                    one.execute(
                        "UPDATE reposeer.crawl_jobs SET status='claimed',claimed_by='D2',claim_token=%s,lease_expires_at=now()+interval '1 hour',attempt_count=1 WHERE job_id=%s",
                        (uuid4(), job),
                    )
        finally:
            setup.execute("DELETE FROM reposeer.crawl_runs WHERE run_id=%s", (run,))
            if repo is not None:
                setup.execute("DELETE FROM reposeer.crawl_jobs WHERE repository_id=%s", (repo,))
                setup.execute("DELETE FROM reposeer.repositories WHERE repository_id=%s", (repo,))
            setup.execute("DELETE FROM reposeer.crawl_jobs WHERE job_id=%s", (job,))
            setup.execute("DELETE FROM reposeer.packages WHERE package_id=%s", (key,))


def test_worker_dry_run_and_interruption_keep_failure_receipt(tmp_path, monkeypatch):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value = assignment()
    path = tmp_path / "assignment.json"
    c.write_json(path, value)
    preview = w.run("C1", path, tmp_path / "work", dry_run=True)
    assert preview["dry_run"] and not (tmp_path / "work").exists()

    def interrupted(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    with pytest.raises(c.CoordinationError, match="collector_process_interrupted"):
        w.run("C1", path, tmp_path / "work", runner=interrupted)
    root = tmp_path / "work" / value["job_id"] / value["claim_token"]
    assert (
        c.read_json(root / "failure_receipt.json")["reason_code"] == "collector_process_interrupted"
    )
    assert not (root / "READY.json").exists()
    old = c.files(root)
    new_value = c.sign_assignment(dict(value, claim_token=str(uuid4())))
    c.write_json(path, new_value, immutable=False)
    with pytest.raises(c.CoordinationError, match="collector_process_interrupted"):
        w.run("C1", path, tmp_path / "work", runner=interrupted)
    new_root = tmp_path / "work" / value["job_id"] / new_value["claim_token"]
    assert new_root != root and c.files(root) == old
    assert (new_root / "failure_receipt.json").exists()


def test_reissued_crawl_claim_uses_immutable_token_path(tmp_path, monkeypatch):
    registry, drive = Registry(), Drive(tmp_path / "remote")
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    value = assignment()
    claim(registry, value)
    job_id = value["job_id"]
    registry.jobs[job_id].update(status="retry", attempt_count=0)
    output = tmp_path / "assignments"
    first = d.dispatch("collect_repository", "C1", 1, output, registry=registry)
    original = c.read_json(first["assignments"][0])
    registry.jobs[job_id]["status"] = "retry"
    second = d.dispatch("collect_repository", "C1", 1, output, registry=registry)
    updated = c.read_json(second["assignments"][0])
    assert updated["claim_token"] != original["claim_token"]
    assert registry.jobs[job_id]["attempt_count"] == 2
    assert first["assignments"][0] != second["assignments"][0]
    assert c.read_json(first["assignments"][0]) == original
    assert (
        c.read_json(
            drive.root / "crawl/assignments/C1" / job_id / (updated["claim_token"] + ".json")
        )
        == updated
    )
    with pytest.raises(c.CoordinationError, match="stale_claim"):
        c.check_claim(registry.jobs[job_id], original)


@pytest.mark.parametrize(
    "scope,next_scope",
    [("initial", "followup:1"), ("followup:1", "followup:2"), ("followup:2", None)],
)
def test_budget_limited_scope_queues_next_once_then_stops(
    tmp_path, environment, monkeypatch, scope, next_scope
):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, calls = stored_partial(
        tmp_path, environment, monkeypatch, scope=scope, budget_limited=True
    )
    w.run("C1", path, tmp_path / "work", runner=runner)
    registry = Registry()
    claim(registry, value)
    root = tmp_path / "work" / value["job_id"] / value["claim_token"]
    first = i.ingest_one(root, drive, registry)
    assert (
        first["job_status"] == "completed"
        and first["activity_coverage"]["status"] == "budget_limited"
    )
    assert first["followup_scope"] == next_scope
    assert registry.repos[1]["activity_coverage_status"] == "budget_limited"
    assert i.ingest_one(root, drive, registry)["duplicate"]
    queued = [j for j in registry.jobs.values() if j["status"] == "queued"]
    assert len(queued) == int(next_scope is not None)
    if queued:
        assert queued[0]["scope_key"] == next_scope
    assert calls[0][calls[0].index("--max-pages") + 1] == str(
        c.collection_options(scope)["max_pages"]
    )
    assert calls[0][calls[0].index("--max-records") + 1] == str(
        c.collection_options(scope)["max_records"]
    )
    for changes in [
        dict(scope_key="followup:3"),
        dict(
            collection=c.collection_options("initial")
            if scope != "initial"
            else dict(c.COLLECTION, max_pages=2)
        ),
    ]:
        with pytest.raises(c.CoordinationError):
            c.validate_assignment(c.sign_assignment(dict(value, **changes)))


def test_late_package_reuses_accepted_evidence_without_github_calls(
    tmp_path, environment, monkeypatch
):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, _ = stored_partial(tmp_path, environment, monkeypatch)
    w.run("C1", path, tmp_path / "work", runner=runner)
    registry = Registry()
    claim(registry, value)
    i.ingest_one(tmp_path / "work" / value["job_id"] / value["claim_token"], drive, registry)
    github_calls = len([r for r in environment[2] if r.url.host == "api.github.com"])
    monkeypatch.setattr(d, "Drive", lambda remote: drive)

    class PyPI:
        def get_package_info(self, name):
            return dict(
                info=dict(name=name, project_urls={"Source": "https://github.com/pallets/flask"})
            )

    for name, corrupt in [("later-package", False), ("broken-evidence", True)]:
        screen = assignment(["pypi:" + name], kind="screen_package")
        claim(registry, screen)
        screen_path = tmp_path / (name + ".json")
        c.write_json(screen_path, screen)
        result = d.screen_local(screen_path, tmp_path / "screening", client=PyPI())
        if corrupt:
            raw = (
                drive.root
                / "screening/submissions/D2"
                / screen["assignment_id"]
                / "pypi_raw"
                / (name + ".json")
            )
            raw.write_bytes(b"{}")
        imported = d.import_screening(result["output"], registry=registry, remote="testremote:")
        package = registry.packages["pypi:" + name]
        if not corrupt:
            assert imported["verification"]["results"][0]["verified"], imported["verification"][
                "results"
            ]
        assert package["last_screening_run_id"] == screen["assignment_id"]
        assert package["mapping_status"] == ("candidate" if corrupt else "verified_current"), (
            imported
        )
        assert imported["jobs_queued"] == 0
        assert imported["verification"]["verified"] == int(not corrupt)
    assert len([j for j in registry.jobs.values() if j["job_kind"] == "collect_repository"]) == 1
    assert len([r for r in environment[2] if r.url.host == "api.github.com"]) == github_calls
    registry.packages["pypi:legacy"] = dict(
        package_id="pypi:legacy", mapping_status="candidate", repository_id=1
    )
    before = copy.deepcopy(registry.__dict__)
    preview = d.verify_pending(registry=registry, remote="testremote:")
    assert any(r["reason_code"] == "screening_evidence_missing" for r in preview["results"])
    assert registry.__dict__ == before


def test_claim_changed_during_upload_leaves_orphan_without_acceptance(
    tmp_path, environment, monkeypatch
):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, _ = stored_partial(tmp_path, environment, monkeypatch)
    w.run("C1", path, tmp_path / "work", runner=runner)
    registry = Registry()
    claim(registry, value)
    upload = drive.upload

    def slow_upload(root, prefix):
        assert registry.active_transactions == 0
        upload(root, prefix)
        registry.jobs[value["job_id"]]["claim_token"] = str(uuid4())

    monkeypatch.setattr(drive, "upload", slow_upload)
    root = tmp_path / "work" / value["job_id"] / value["claim_token"]
    with pytest.raises(c.CoordinationError, match="stale_claim"):
        i.ingest_one(root, drive, registry)
    assert not registry.runs and not registry.artifacts
    assert registry.jobs[value["job_id"]]["status"] == "claimed"
    assert drive.listing("canonical/runs")
    # Upload can replay after an interrupted acceptance, with the original valid claim.
    registry.jobs[value["job_id"]]["claim_token"] = value["claim_token"]
    monkeypatch.setattr(drive, "upload", upload)
    assert i.ingest_one(root, drive, registry)["artifact_accepted"]
    with registry.writer_lock(), pytest.raises(c.CoordinationError, match="another_ingest_writer"):
        i.run("testremote:", tmp_path / "central", registry=registry)


def test_catalog_10001_sql_snapshots_need_no_accepted_drive_reads(tmp_path, monkeypatch):
    registry, drive = Registry(), Drive(tmp_path / "remote")
    monkeypatch.setattr(i, "Drive", lambda remote: drive)
    observed = []
    transport = drive._run

    def no_history_reads(args, **kwargs):
        assert registry.active_transactions == 0
        assert not (args[0] == "copyto" and args[1].startswith("testremote:canonical/runs/"))
        assert not (args[0] == "lsjson" and args[1] == "testremote:crawl/submissions")
        observed.append(args)
        return transport(args, **kwargs)

    monkeypatch.setattr(drive, "_run", no_history_reads)
    value = assignment()
    for n in range(10001):
        run = str(uuid5(NAMESPACE_URL, "coordination-catalog-test/" + str(n)))
        receipt = dict(
            integration_run_id=run,
            expected_repo_full_name="pallets/flask",
            verified_github_repo_id=900000001,
            overall_status="partial",
        )
        snapshot = i.catalog_snapshot(
            value, receipt, drive, "a" * 64, c.timestamp("2026-10-21T00:00:00.000Z")
        )
        registry.runs[run] = dict(run_id=run, run_kind="integration", catalog_metadata=snapshot)
        registry.artifacts[snapshot["stored_artifact_path"] + "/CHECKSUMS.sha256"] = dict(
            run_id=run, artifact_kind="bundle", status="ingested"
        )
    assert i.rebuild_catalog(drive, registry) == 10001
    import polars as pl

    table = pl.read_parquet(drive.root / "canonical/catalog/ingestion_index.parquet")
    assert table.height == 10001 and table["package_ids"].to_list() == [["pypi:flask"]] * 10001
    original = c.fingerprint(drive.root / "canonical/catalog/ingestion_index.parquet")
    registry.packages["pypi:new-package"] = dict(package_id="pypi:new-package", repository_id=1)
    assert i.rebuild_catalog(drive, registry) == 10001
    assert original == c.fingerprint(drive.root / "canonical/catalog/ingestion_index.parquet")
    observed.clear()
    result = i.run("testremote:", tmp_path / "central", registry=registry)
    assert result["catalog_rows"] is None and not result["accepted"]
    assert len([a for a in observed if a[0] == "lsjson"]) == 7


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing_newest", None),
        ("wrong_numeric", "accepted_repository_evidence_missing"),
        ("corrupt_raw", "github_raw_checksum"),
        ("no_raw", "accepted_repository_evidence_missing"),
        ("conflicting_identity", "invalid_github_repository"),
        ("bad_checksum", "accepted_repository_checksum"),
        ("malformed_checksums", "accepted_repository_checksum_list_invalid"),
    ],
)
def test_verify_pending_newest_usable_evidence_and_integrity(tmp_path, monkeypatch, mode, reason):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    registry, drive = Registry(), Drive(tmp_path / "remote")
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    network_calls = []

    def forbidden(*args, **kwargs):
        network_calls.append(True)
        pytest.fail("verification must not fetch provider evidence")

    monkeypatch.setattr(t020.httpx.Client, "send", forbidden)
    crawl = assignment()
    claim(registry, crawl)
    registry.repos[1]["github_repo_id"] = 900000001
    screen = assignment(["pypi:late-a", "pypi:late-b"], kind="screen_package")
    claim(registry, screen)
    path = tmp_path / "screen.json"
    c.write_json(path, screen)

    class PyPI:
        def get_package_info(self, name):
            return dict(
                info=dict(name=name, project_urls={"Source": "https://github.com/pallets/flask"})
            )

    result = d.screen_local(path, tmp_path / "screen", client=PyPI())
    screen_id = screen["assignment_id"]
    screen_root = Path(result["output"])
    screen_prefix = f"screening/submissions/D2/{screen_id}"
    registry.runs[screen_id] = dict(run_id=screen_id, run_kind="screening", worker_id="D2")
    registry.artifacts[drive.path(screen_prefix + "/CHECKSUMS.sha256")] = dict(
        run_id=screen_id,
        artifact_kind="bundle",
        status="ingested",
        storage_path=drive.path(screen_prefix + "/CHECKSUMS.sha256"),
        sha256=c.fingerprint(screen_root / "CHECKSUMS.sha256")[0],
    )
    for package in ("pypi:late-a", "pypi:late-b"):
        registry.packages[package].update(repository_id=1, last_screening_run_id=screen_id)
    older, newest = str(uuid4()), str(uuid4())
    hashes = {}
    for index, run_id in enumerate([older, newest]):
        root = drive.root / f"canonical/runs/{run_id}"
        c.write_json(root / "ASSIGNMENT.json", crawl)
        if mode != "no_raw" and (index == 0 or mode != "missing_newest"):
            numeric = 900000002 if mode == "wrong_numeric" else 900000001
            payload = dict(
                t020.repository_raw().payload,
                id=numeric,
                full_name="pallets/flask",
                html_url="https://github.com/pallets/flask",
                private=False,
                fork=False,
                is_template=False,
                mirror_url=None,
            )
            if mode == "conflicting_identity" and index == 1:
                payload["id"] = 900000002
            raw = t020.repository_raw(
                raw_record_id="raw:" + str(uuid4()),
                run_id=str(uuid4()),
                repo_id="github:repository:" + str(numeric),
                source_record_id=str(numeric),
                source_url="https://api.github.com/repos/pallets/flask",
                payload=payload,
            )
            c.atomic_bytes(
                root / "raw/github/repositories.jsonl",
                c.canonical(raw.model_dump(mode="json")) + b"\n",
            )
            hashes[run_id] = c.fingerprint(root / "raw/github/repositories.jsonl")[0]
        c.seal_bundle(root, crawl, run_id)
        checksums = root / "CHECKSUMS.sha256"
        registry.runs[run_id] = dict(
            run_id=run_id,
            job_id=crawl["job_id"],
            run_kind="integration",
            worker_id="C1",
            status="partial",
            finished_at=c.timestamp(f"2026-10-21T0{index}:00:00.000Z"),
            catalog_metadata=None
            if index == 0 or mode == "wrong_numeric"
            else dict(github_repo_id=900000001),
        )
        registry.artifacts[drive.path(f"canonical/runs/{run_id}/CHECKSUMS.sha256")] = dict(
            run_id=run_id,
            artifact_kind="bundle",
            status="ingested",
            storage_path=drive.path(f"canonical/runs/{run_id}/CHECKSUMS.sha256"),
            sha256=c.fingerprint(checksums)[0],
        )
        if index == 1:
            if mode == "corrupt_raw":
                with (root / "raw/github/repositories.jsonl").open("ab") as stream:
                    stream.write(b"corrupted")
            elif mode in {"bad_checksum", "malformed_checksums"}:
                checksums.write_bytes(b"malformed checksum list")
                if mode == "malformed_checksums":
                    registry.artifacts[drive.path(f"canonical/runs/{run_id}/CHECKSUMS.sha256")][
                        "sha256"
                    ] = c.fingerprint(checksums)[0]
    reads = []
    transport = drive._run

    def tracked(args, **kwargs):
        assert registry.active_transactions == 0
        if args[0] == "copyto" and args[1].startswith("testremote:canonical/runs/"):
            reads.append(args[1])
        return transport(args, **kwargs)

    monkeypatch.setattr(drive, "_run", tracked)
    before = copy.deepcopy(registry.__dict__)
    preview = d.verify_pending(registry=registry, remote="testremote:")
    assert registry.__dict__ == before and preview["dry_run"]
    reads.clear()
    applied = d.verify_pending(registry=registry, remote="testremote:", apply=True)
    assert applied["verified"] == (2 if reason is None else 0), applied
    for package in ("pypi:late-a", "pypi:late-b"):
        row = registry.packages[package]
        assert row["mapping_status"] == ("verified_current" if reason is None else "candidate")
        if reason is None:
            evidence = row["mapping_evidence"]
            assert (
                evidence["integration_run_id"] == older
                and evidence["github_sha256"] == hashes[older]
            )
            assert evidence["historical_validity"] == "unverified"
        else:
            assert row["mapping_evidence"]["reason_code"] == reason
    if reason is None:
        assert reads.count(drive.path(f"canonical/runs/{older}/raw/github/repositories.jsonl")) == 1
        assert reads.count(drive.path(f"canonical/runs/{newest}/CHECKSUMS.sha256")) == 1
    elif mode in {"corrupt_raw", "conflicting_identity", "bad_checksum", "malformed_checksums"}:
        assert not any(older in path for path in reads)  # Integrity failures never fall back.
    assert not network_calls
