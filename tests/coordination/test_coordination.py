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
from uuid import uuid4

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

    @contextmanager
    def connect(self, *, readonly=False):
        snapshot = copy.deepcopy(self.__dict__)
        self.readonly = readonly
        try:
            yield self
        except BaseException:
            self.__dict__.update(snapshot)
            raise
        finally:
            self.readonly = False

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
                return Rows(
                    [
                        dict(
                            a,
                            **{
                                k: self.runs[a["run_id"]][k]
                                for k in ["job_id", "worker_id", "status"]
                            },
                        )
                        for a in self.artifacts.values()
                        if a["artifact_kind"] == "bundle"
                        and self.runs[a["run_id"]]["run_kind"] == "integration"
                        and (not args or a["storage_path"] == args[0])
                    ]
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
                        [
                            j
                            for j in self.jobs.values()
                            if j["job_kind"] == args[0] and j["status"] in {"queued", "retry"}
                        ][: args[1]]
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
            if any(j["job_kind"] == kind and j.get(key) == args[0] for j in self.jobs.values()):
                return Rows()
            identity = str(uuid4())
            self.jobs[identity] = dict(
                job_id=identity,
                job_kind=kind,
                **{key: args[0]},
                status="queued",
                attempt_count=0,
                max_attempts=3,
                scope_key="initial",
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
                    )
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
                    attempt_count=job["attempt_count"] + 1,
                )
            elif "SET status=%s" in sql:
                job.update(status=args[0], retry_after=args[1], last_error_code=args[2])
            elif "SET claimed_by=NULL" in sql:
                job.update(
                    claimed_by=None, claim_token=None, claimed_at=None, lease_expires_at=None
                )
            elif "last_error_code=%s" in sql:
                job["last_error_code"] = args[0]
            return Rows()
        if sql.startswith("UPDATE reposeer.packages"):
            if "mapping_status='verified_current' WHERE" in sql:
                self.packages[args[0]]["mapping_status"] = "verified_current"
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
                        for p in path.rglob("*")
                        if p.is_file()
                    ]
                )
                if path.exists()
                else None
            )
        assert args[0] == "copyto"
        source, target = self.local(args[1]), self.local(args[2])
        if args[2].startswith("testremote:"):
            if self.fail and self.fail in args[2]:
                raise c.CoordinationError("injected_upload_failure")
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
            scope_key="initial",
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


def stored_partial(tmp_path, environment, monkeypatch, *, unresolved=False):
    pipeline, paths, _, options, _ = environment
    options["fail_provider"] = True
    if unresolved:
        options["mapping"] = "unresolved"
    result = pipeline.run(["pypi:flask"], max_pages=1, max_records=30)
    assert result.status != "complete"
    value = assignment()
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
    assert registry.packages["pypi:previously-verified"] == original_mapping
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
    root = tmp_path / "work" / value["job_id"]
    raw = (root / "raw/github/repositories.jsonl").read_bytes()
    receipt = c.read_json(root / "COORDINATION_RECEIPT.json")
    assert (
        receipt["overall_status"] == "partial" and receipt["verified_github_repo_id"] == 900000001
    )
    assert receipt["collected_repository"] and receipt["verified_package_ids"] == ["pypi:flask"]
    drive.fail = None
    w.run("C1", path, tmp_path / "work", runner=runner)
    assert len(calls) == 1 and raw == (root / "raw/github/repositories.jsonl").read_bytes()
    assert drive.writes[-1].endswith("/READY.json")
    assert "--skip-external" not in calls[0]


def test_ingestion_replay_and_versioned_catalog(tmp_path, environment, monkeypatch):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, _ = stored_partial(tmp_path, environment, monkeypatch)
    w.run("C1", path, tmp_path / "work", runner=runner)
    registry = Registry()
    claim(registry, value)
    root = tmp_path / "work" / value["job_id"]
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
    assert registry.packages["pypi:flask"]["mapping_status"] == "verified_current"
    assert i.ingest_one(root, drive, registry)["duplicate"]
    assert len(registry.runs) == 1 and len(registry.artifacts) == 4
    drive.fail = "canonical/catalog/active.json"
    with pytest.raises(c.CoordinationError, match="upload"):
        i.rebuild_catalog(drive, registry)
    assert len(registry.runs) == 1 and registry.jobs[value["job_id"]]["status"] == "completed"
    assert not (drive.root / "canonical/catalog/active.json").exists()
    drive.fail = None
    assert i.rebuild_catalog(drive, registry) == 1
    original = c.files(drive.root / "canonical")
    assert i.rebuild_catalog(drive, registry) == 1 and c.files(drive.root / "canonical") == original
    replay = i.run("testremote:", tmp_path / "central", registry=registry)
    assert replay["catalog_rows"] == 1 and not replay["pending"] and not replay["accepted"]
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
    before = copy.deepcopy(registry.__dict__)
    with pytest.raises(c.CoordinationError, match="checksum_conflict"):
        i.run("testremote:", tmp_path / "central", registry=registry, dry_run=True)
    assert registry.__dict__ == before
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
    result = i.ingest_one(tmp_path / "work" / value["job_id"], drive, registry)
    assert (
        result["artifact_accepted"]
        and not result["job_collected"]
        and result["job_status"] == "retry"
    )
    assert registry.packages["pypi:flask"]["mapping_status"] == "candidate"
    assert registry.repos[1]["github_repo_id"] is None


def test_invalid_bundles_and_stale_claims_cannot_complete_jobs(tmp_path, environment, monkeypatch):
    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    value, path, drive, runner, _ = stored_partial(tmp_path, environment, monkeypatch)
    w.run("C1", path, tmp_path / "work", runner=runner)
    root = tmp_path / "work" / value["job_id"]
    registry = Registry()
    claim(registry, value)
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
    value = assignment()
    claim(registry, value)
    registry.jobs[value["job_id"]]["lease_expires_at"] = c.now() - timedelta(hours=1)
    monkeypatch.setattr(d, "Drive", lambda remote: drive)
    before = copy.deepcopy(registry.__dict__)
    preview = d.requeue_expired(pending_dir=tmp_path / "empty", registry=registry)
    assert preview["dry_run"] and preview["jobs_requeued"] == [value["job_id"]]
    assert registry.__dict__ == before

    def unavailable(prefix):
        raise c.CoordinationError("drive_unavailable")

    monkeypatch.setattr(drive, "listing", unavailable)
    with pytest.raises(c.CoordinationError):
        d.requeue_expired(apply=True, pending_dir=tmp_path / "empty", registry=registry)
    assert registry.__dict__ == before


def test_postgresql_competing_transactions_skip_locked():
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
            setup.execute(path.read_text(encoding="utf-8"))
        key = "pypi:coord-test-" + uuid4().hex
        setup.execute("INSERT INTO reposeer.packages(package_id) VALUES (%s)", (key,))
        job = setup.execute(
            "INSERT INTO reposeer.crawl_jobs(job_kind,package_id,priority) VALUES ('screen_package',%s,2147483647) RETURNING job_id",
            (key,),
        ).fetchone()["job_id"]
        try:
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
    root = tmp_path / "work" / value["job_id"]
    assert (
        c.read_json(root / "failure_receipt.json")["reason_code"] == "collector_process_interrupted"
    )
    assert not (root / "READY.json").exists()


def test_reissued_crawl_claim_replaces_assignment_pointer_only(tmp_path, monkeypatch):
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
    assert c.read_json(output / "history" / (original["assignment_sha256"] + ".json")) == original
    assert c.read_json(drive.root / "crawl/assignments/C1" / (job_id + ".json")) == updated
    with pytest.raises(c.CoordinationError, match="stale_claim"):
        c.check_claim(registry.jobs[job_id], original)
