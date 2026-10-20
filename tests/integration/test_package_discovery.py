"""Offline PyPI index streaming, frame sampling, registry integrity and T-020 handoff."""

import csv
import gzip
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.clients.pypi_index import (
    INDEX_ACCEPT,
    INDEX_URL,
    PackageDiscoveryError,
    PyPIIndexClient,
)
from reposeer.config import settings
from reposeer.pipelines.collection import parse_targets
from reposeer.pipelines.package_discovery import (
    CSV_FIELDS,
    PackageDiscoveryOptions,
    PackageDiscoveryPipeline,
)
from reposeer.storage.paths import StoragePaths

NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def index(names):
    yield b"<!DOCTYPE html><html><body>"
    for name in names:
        yield f'<a href="/simple/{name}/">{name}</a>\n'.encode()
    yield b"</body></html>"


class Chunks(httpx.SyncByteStream):
    def __init__(self, factory):
        self.factory, self.closed = factory, False

    def __iter__(self):
        yield from self.factory()

    def close(self):
        self.closed = True


def response(names, **headers):
    return httpx.Response(
        200,
        headers={
            "content-type": INDEX_ACCEPT + "; charset=UTF-8",
            "X-PyPI-Last-Serial": "12345",
            "ETag": '"index-etag"',
            **headers,
        },
        stream=Chunks(lambda: index(names)),
    )


def options(tmp_path, **kwargs):
    return PackageDiscoveryOptions(
        source="pypi-simple", count=2, data_dir=tmp_path / "data root", **kwargs
    )


def verify(result, root):
    m = read(result.manifest_path)
    assert m["status"] == result.status == "complete"
    artifacts = [
        m["artifacts"]["candidates_csv"],
        m["artifacts"]["targets"],
        *m["artifacts"]["batches"],
    ]
    for artifact in artifacts:
        relative = Path(artifact["relative_path"])
        assert (
            not relative.is_absolute()
            and ".." not in relative.parts
            and "\\" not in artifact["relative_path"]
        )
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == artifact["sha256"]
    with result.candidates_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert tuple(rows[0]) == CSV_FIELDS
    assert all(
        row["eligibility_status"] == "name_valid_only"
        and row["mapping_status"] == "not_checked"
        and row["collection_status"] == "not_started"
        for row in rows
    )
    assert [int(row["sample_rank"]) for row in rows] == list(range(1, len(rows) + 1))
    assert all(row["sampling_run_id"] == result.sampling_run_id for row in rows)
    targets = result.targets_path.read_bytes()
    assert b"\r" not in targets and targets.endswith(b"\n")
    combined = b""
    for artifact in m["artifacts"]["batches"]:
        data = (root / artifact["relative_path"]).read_bytes()
        assert b"\r" not in data
        parsed = parse_targets(data.decode().splitlines())
        assert 1 <= len(parsed) <= 100 and len(parsed) == artifact["row_count"]
        assert all(target["target_type"] == "pypi" for target in parsed)
        combined += data
    assert combined == targets
    ids = targets.decode().splitlines()
    assert len(set(ids)) == len(ids) == result.selected_count == m["sampling"]["selected_count"]
    assert ids == [row["package_id"] for row in rows]
    assert [row["batch_id"] for row in rows] == [
        f"batch_{i // m['sampling']['batch_size'] + 1:03d}" for i in range(len(rows))
    ]
    assert StoragePaths(root).processing_inputs() == [] and not (root / "raw").exists()
    return m, ids


def test_mock_live_snapshot_replay_and_immutable_provenance(tmp_path):
    calls, streams = [], []
    names = ["Flask", "requests", "pydantic", "Demo_Package", "demo.package", "demo--package"]

    def handler(request):
        calls.append(request)
        assert str(request.url) == INDEX_URL and request.headers["Accept"] == INDEX_ACCEPT
        assert "authorization" not in request.headers and "cookie" not in request.headers
        result = response(names)
        streams.append(result.stream)
        return result

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "secret", "Cookie": "secret"},
        params={"api_key": "must-not-be-forwarded"},
        auth=("user", "secret"),
    ) as borrowed:
        with PyPIIndexClient(client=borrowed, clock=lambda: NOW) as client:
            original = PackageDiscoveryPipeline(index_client=client, clock=lambda: NOW).run(
                options(tmp_path)
            )
        assert not borrowed.is_closed
        metadata_before = original.snapshot_path.with_suffix(".json").read_bytes()
        snapshot_before = original.snapshot_path.read_bytes()
        with PyPIIndexClient(
            client=borrowed, clock=lambda: datetime(2026, 10, 21, tzinfo=UTC)
        ) as client:
            same = client.fetch_snapshot(original.snapshot_path.parent)
        assert same.retrieved_at == "2026-10-20T08:00:00.000Z"
    m, ids = verify(original, options(tmp_path).data_dir)
    assert len(calls) == 2 and all(stream.closed for stream in streams)
    assert m["counts"] == dict(
        total_valid_entries=6,
        unique_valid_candidates=4,
        normalized_duplicates=2,
        explicitly_excluded_candidates=0,
        eligible_unique_candidates=4,
    )
    meta = read(original.snapshot_path.with_suffix(".json"))
    assert meta["sha256"] == hashlib.sha256(snapshot_before).hexdigest()
    assert meta["decoded_byte_count"] == len(snapshot_before) and meta["last_serial"] == 12345
    assert meta["etag"] == '"index-etag"' and meta["last_modified"] is None
    assert original.snapshot_path.with_suffix(".json").read_bytes() == metadata_before

    class NoNetwork:
        def fetch_snapshot(self, *args, **kwargs):
            pytest.fail("Snapshot replay attempted HTTP")

    replay = PackageDiscoveryPipeline(index_client=NoNetwork()).run(
        replace(options(tmp_path), source="snapshot", snapshot=original.snapshot_path)
    )
    replay_m, replay_ids = verify(replay, options(tmp_path).data_dir)
    assert (
        replay_ids == ids and replay.targets_path.read_bytes() == original.targets_path.read_bytes()
    )
    assert replay_m["source"]["kind"] == "verified_snapshot"
    assert replay_m["source"]["acquisition_mode"] == "offline_replay"
    assert replay_m["source"]["snapshot_retrieved_at"] == m["source"]["snapshot_retrieved_at"]
    imported = PackageDiscoveryPipeline(index_client=NoNetwork()).run(
        replace(
            options(tmp_path),
            source="snapshot",
            snapshot=original.snapshot_path,
            data_dir=tmp_path / "another root",
        )
    )
    assert imported.snapshot_path.read_bytes() == snapshot_before
    assert verify(imported, tmp_path / "another root")[1] == ids


def test_streaming_utf8_anchor_boundaries_and_foreign_links(tmp_path, monkeypatch):
    import reposeer.clients.pypi_index as module

    monkeypatch.setattr(module, "CHUNK_SIZE", 5)

    def pieces():
        yield b"<html><body><!--caf\xc3"
        yield b"\xa9-->"
        yield b"<a hr"
        yield b'ef="/simple/De'
        yield b'mo_Name/">Demo_Name</a>'
        for href in (
            "https://evil.example/simple/demo/",
            "/simple/demo/?x=1",
            "/simple/demo/#x",
            "/simple/invalid!name/",
            "/about/",
            "https://user:secret@pypi.org/simple/demo/",
        ):
            yield f'<a href="{href}">demo</a>'.encode()
        yield b'<a>missing</a><a href="https://pypi.org/simple/requests/">requests</a></body></html>'

    with PyPIIndexClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, headers={"content-type": "text/html"}, stream=Chunks(pieces)
            )
        ),
        clock=lambda: NOW,
    ) as client:
        result = PackageDiscoveryPipeline(index_client=client).run(options(tmp_path))
    m, ids = verify(result, options(tmp_path).data_dir)
    assert set(ids) == {"pypi:demo-name", "pypi:requests"}
    assert m["parsing"] == dict(
        anchors_seen=9, valid_project_entries=2, invalid_entries=7, duplicate_normalized_names=0
    )
    assert read(result.snapshot_path.with_suffix(".json"))["last_serial"] is None


@pytest.mark.parametrize("seed", [42, -17, 20261020])
def test_file_order_independent_priority_exclusions_and_103_target_batches(
    tmp_path, seed, monkeypatch
):
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("File mode used HTTP"))
    names = [f"demo-{i:03}" for i in range(110)]
    input_file, exclude_file = tmp_path / "names.txt", tmp_path / "exclude.txt"
    input_file.write_text(
        "# frame\n\n" + "\n".join(names + ["Demo_001", "demo.001"]) + "\n", encoding="utf-8"
    )
    exclude_file.write_text(
        "# exclusions\nDemo_001\ndemo.001\npypi:demo-002\noutside\n", encoding="utf-8"
    )
    opt = PackageDiscoveryOptions(
        "file",
        103,
        tmp_path / "data",
        seed=seed,
        batch_size=50,
        input_file=input_file,
        exclude_file=exclude_file,
    )
    first = PackageDiscoveryPipeline().run(opt)
    m, ids = verify(first, opt.data_dir)
    expected = sorted(
        (name for name in names if name not in {"demo-001", "demo-002"}),
        key=lambda name: (
            hashlib.sha256(f"reposeer:pypi-sample:v1:{seed}:{name}".encode()).digest(),
            name,
        ),
    )[:103]
    assert ids == ["pypi:" + name for name in expected]
    assert m["counts"] == dict(
        total_valid_entries=112,
        unique_valid_candidates=110,
        normalized_duplicates=2,
        explicitly_excluded_candidates=2,
        eligible_unique_candidates=108,
    )
    assert m["exclusion"] == dict(
        file_sha256=hashlib.sha256(exclude_file.read_bytes()).hexdigest(), excluded_name_count=3
    )
    assert m["source"]["input_file_sha256"] == hashlib.sha256(input_file.read_bytes()).hexdigest()
    assert (
        m["source"]["kind"] == "user_supplied_file" and m["source"]["snapshot_retrieved_at"] is None
    )
    assert m["source"]["frame_scope"] == "user_supplied_subset"
    assert [batch["row_count"] for batch in m["artifacts"]["batches"]] == [50, 50, 3]
    assert [batch["batch_id"] for batch in m["artifacts"]["batches"]] == [
        "batch_001",
        "batch_002",
        "batch_003",
    ]
    input_file.write_text("\n".join(reversed(names)), encoding="utf-8")
    exclude_file.write_text("outside\ndemo-002\ndemo-001\n", encoding="utf-8")
    second = PackageDiscoveryPipeline().run(opt)
    assert first.sampling_run_id != second.sampling_run_id
    assert second.targets_path.read_bytes() == first.targets_path.read_bytes()
    assert [path.read_bytes() for path in sorted(second.batches_dir.iterdir())] == [
        path.read_bytes() for path in sorted(first.batches_dir.iterdir())
    ]
    assert verify(second, opt.data_dir)[1] == ids


@pytest.mark.parametrize(
    "mode",
    [
        "503",
        "429",
        "timeout",
        "forbidden",
        "redirect",
        "content_type",
        "serial",
        "utf8",
        "truncated",
        "interrupted",
        "size",
        "compressed_size",
        "wait_budget",
    ],
)
def test_acquisition_failures_retries_and_cleanup(tmp_path, mode):
    calls, sleeps, streams = [], [], []
    good = b"".join(index(["demo", "other"]))

    def interrupted():
        yield b" " * 65536 + b"<html>"
        raise httpx.ReadTimeout("private-stream-error")

    def handler(request):
        calls.append(request)
        if mode in {"503", "429", "wait_budget"} and (len(calls) == 1 or mode == "wait_budget"):
            return httpx.Response(
                503 if mode == "503" else 429,
                headers={"Retry-After": "9999" if mode == "wait_budget" else "2"},
                text="private-upstream-body",
            )
        if mode == "forbidden":
            return httpx.Response(403, text="private-upstream-body")
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/simple/"})
        if mode == "timeout" and len(calls) == 1 or mode == "interrupted":
            stream = Chunks(interrupted)
        else:
            body = good
            if mode == "truncated":
                body = b'<html><body><a href="/simple/demo/">demo</a>'
            if mode == "utf8":
                body = b"<html><body>\xff</body></html>"
            if mode == "compressed_size":
                body = gzip.compress(b"".join(index(["large" + str(i) for i in range(100)])))
            stream = Chunks(lambda: iter([body]))
        streams.append(stream)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json" if mode == "content_type" else "text/html",
                **({"X-PyPI-Last-Serial": "invalid"} if mode == "serial" else {}),
                **({"Content-Encoding": "gzip"} if mode == "compressed_size" else {}),
            },
            stream=stream,
        )

    client = PyPIIndexClient(
        transport=httpx.MockTransport(handler), sleep=sleeps.append, clock=lambda: NOW
    )
    budget = 64 if mode in {"size", "compressed_size"} else 1048576
    try:
        if mode in {"503", "429", "timeout"}:
            snapshot = client.fetch_snapshot(tmp_path / "snapshots", max_decoded_bytes=budget)
            assert snapshot.path.read_bytes() == good
            assert snapshot.sha256 == hashlib.sha256(good).hexdigest()
            assert len(calls) == 2 and sleeps == [2 if mode in {"503", "429"} else 1]
        else:
            with pytest.raises(PackageDiscoveryError) as raised:
                client.fetch_snapshot(tmp_path / "snapshots", max_decoded_bytes=budget)
            assert "private" not in str(raised.value)
            assert list((tmp_path / "snapshots").iterdir()) == []
            assert len(calls) == (3 if mode == "interrupted" else 1)
            if mode == "forbidden":
                assert str(raised.value) == "index_http_status:403" and not sleeps
            if mode == "wait_budget":
                assert str(raised.value) == "index_retry_wait_budget_exceeded" and not sleeps
            if mode in {"size", "compressed_size"}:
                assert str(raised.value) == "index_byte_budget_exceeded"
        assert all(stream.closed for stream in streams)
    finally:
        client.close()
    assert client.client.is_closed


@pytest.mark.parametrize(
    "corruption", ["bytes", "version", "url", "timestamp", "serial", "size", "missing"]
)
def test_corrupt_snapshot_and_metadata_fail_closed(tmp_path, corruption, monkeypatch):
    with PyPIIndexClient(
        transport=httpx.MockTransport(lambda r: response(["demo", "other"])), clock=lambda: NOW
    ) as client:
        snapshot = client.fetch_snapshot(tmp_path / "snapshots")
    sidecar = snapshot.path.with_suffix(".json")
    meta = read(sidecar)
    if corruption == "bytes":
        with snapshot.path.open("ab") as stream:
            stream.write(b"!")
    elif corruption == "missing":
        sidecar.unlink()
    else:
        key, value = {
            "version": ("snapshot_version", "2"),
            "url": ("source_url", "https://evil.example/"),
            "timestamp": ("retrieved_at", "2024-01-01"),
            "serial": ("last_serial", True),
            "size": ("decoded_byte_count", "1"),
        }[corruption]
        meta[key] = value
        sidecar.write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("Replay used HTTP"))
    with pytest.raises((PackageDiscoveryError, OSError)) as raised:
        PackageDiscoveryPipeline().run(
            replace(options(tmp_path), source="snapshot", snapshot=snapshot.path)
        )
    if corruption == "bytes":
        assert str(raised.value) == "snapshot_checksum_mismatch"
    assert not list(options(tmp_path).data_dir.glob("registry/runs/*/manifest.json"))


@pytest.mark.parametrize("failure", ["row", "exclusion", "insufficient", "write", "publish"])
def test_file_and_persistence_failures_preserve_previous_runs(tmp_path, monkeypatch, failure):
    import reposeer.pipelines.package_discovery as module

    path = tmp_path / "names.txt"
    path.write_text("Demo_Name\nother\nthird\n", encoding="utf-8")
    opt = replace(options(tmp_path), source="file", input_file=path)
    original = PackageDiscoveryPipeline().run(opt)
    before = {
        file: file.read_bytes()
        for file in original.manifest_path.parent.rglob("*")
        if file.is_file()
    }
    if failure == "row":
        path.write_text("demo\nhttps://github.com/user/repo\n", encoding="utf-8")
    elif failure == "exclusion":
        exclusions = tmp_path / "exclude.txt"
        exclusions.write_text("demo\ninvalid # inline\n", encoding="utf-8")
        opt = replace(opt, exclude_file=exclusions)
    elif failure == "insufficient":
        opt = replace(opt, count=100)
    elif failure == "write":

        def fail(path, names):
            path.write_text("partial", encoding="utf-8")
            raise OSError("private-disk-error")

        monkeypatch.setattr(module, "_write_targets", fail)
    else:
        replace_dir = module.os.replace

        def fail(source, destination):
            if Path(source).is_dir():
                raise OSError("private-publish-error")
            replace_dir(source, destination)

        monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises((PackageDiscoveryError, OSError)) as raised:
        PackageDiscoveryPipeline().run(opt)
    if failure in {"row", "exclusion"}:
        assert str(raised.value) == f"invalid_{'input' if failure == 'row' else 'exclusion'}_row:2"
    if failure == "insufficient":
        assert str(raised.value) == "insufficient_eligible_candidates"
    assert all(file.read_bytes() == value for file, value in before.items())
    assert list((opt.data_dir / "registry/runs").iterdir()) == [original.manifest_path.parent]
    assert not list((opt.data_dir / "registry").glob(".sampling-*"))


@pytest.mark.parametrize(
    "flags",
    [
        ["--count", "0"],
        ["--count", "10001"],
        ["--batch-size", "0"],
        ["--batch-size", "101"],
        ["--seed", "not-int"],
        ["--max-index-bytes", "0"],
        ["--max-index-bytes", "536870913"],
        ["--source", "unknown"],
        ["--source", "file"],
        ["--source", "snapshot"],
        ["--snapshot", "snapshot.html"],
        ["--input-file", "names.txt"],
        ["--source", "file", "--input-file", "names.txt", "--snapshot", "index.html"],
        ["--source", "snapshot", "--snapshot", "index.html", "--input-file", "names.txt"],
    ],
)
def test_cli_validates_before_acquisition(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(
        PyPIIndexClient,
        "fetch_snapshot",
        lambda *a, **k: pytest.fail("Invalid arguments used network"),
    )
    result = CliRunner().invoke(
        app,
        [
            "discover-packages",
            "--source",
            "pypi-simple",
            "--count",
            "2",
            "--data-dir",
            str(tmp_path / "data"),
            *flags,
        ],
    )
    assert result.exit_code == 2, result.output
    assert not (tmp_path / "data").exists()


def test_cli_file_help_runtime_exit_and_no_token_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "github_token", None)
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: pytest.fail("Offline CLI used HTTP"))
    runner = CliRunner()
    help_result = runner.invoke(app, ["discover-packages", "--help"])
    assert help_result.exit_code == 0
    names = tmp_path / "names.txt"
    names.write_text("Demo_Package\ndemo.package\ndemo--package\n", encoding="utf-8")
    args = [
        "discover-packages",
        "--source",
        "file",
        "--input-file",
        str(names),
        "--count",
        "1",
        "--data-dir",
        str(tmp_path / "data"),
    ]
    successful = runner.invoke(app, args)
    assert successful.exit_code == 0, successful.output
    assert (
        "Selected packages: 1" in successful.output
        and "Unique eligible names: 1" in successful.output
    )
    manifest = next((tmp_path / "data/registry/runs").glob("*/manifest.json"))
    assert read(manifest)["counts"]["normalized_duplicates"] == 2
    failed = runner.invoke(app, [*args, "--count", "2"])
    assert failed.exit_code == 1 and "insufficient_eligible_candidates" in failed.output
    assert runner.invoke(app, ["collect-dataset", "--help"]).exit_code == 0


def test_100000_streamed_projects_and_sqlite_scratch_cleanup(tmp_path, monkeypatch):
    import reposeer.pipelines.package_discovery as module

    connections, scratch_paths = [], []
    connect = module.sqlite3.connect

    def record(path):
        scratch_paths.append(Path(path))
        connection = connect(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", record)

    def handler(request):
        assert str(request.url) == INDEX_URL
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=Chunks(lambda: index(f"project-{i}" for i in range(100000))),
        )

    with PyPIIndexClient(transport=httpx.MockTransport(handler), clock=lambda: NOW) as client:
        result = PackageDiscoveryPipeline(index_client=client).run(
            replace(options(tmp_path), count=23, batch_size=10)
        )
    m, ids = verify(result, options(tmp_path).data_dir)
    assert m["counts"]["eligible_unique_candidates"] == m["counts"]["total_valid_entries"] == 100000
    assert len(ids) == 23 and result.batch_count == 3
    assert scratch_paths and all(not path.parent.exists() for path in scratch_paths)
    assert all(not path.is_relative_to(options(tmp_path).data_dir) for path in scratch_paths)
    for connection in connections:
        with pytest.raises(module.sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_existing_corrupt_content_addressed_snapshot_is_never_overwritten(tmp_path):
    with PyPIIndexClient(
        transport=httpx.MockTransport(lambda request: response(["demo", "other"]))
    ) as client:
        snapshot = client.fetch_snapshot(tmp_path / "snapshots")
        metadata = snapshot.path.with_suffix(".json").read_bytes()
        snapshot.path.write_bytes(b"corrupted-existing-evidence")
        with pytest.raises(PackageDiscoveryError, match="existing_snapshot_checksum_mismatch"):
            client.fetch_snapshot(snapshot.path.parent)
        assert snapshot.path.read_bytes() == b"corrupted-existing-evidence"
        assert snapshot.path.with_suffix(".json").read_bytes() == metadata
        assert not list(snapshot.path.parent.glob("*.tmp"))


def test_progress_streaming_retry_parsing_and_replay(tmp_path):
    from reposeer.clients.pypi_index import parse_index

    events, calls = [], []
    body = b"".join(index(f"project-{i}" for i in range(3000)))

    def interrupted():
        yield body[:65536]
        raise httpx.ReadTimeout("interrupted")

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=Chunks(interrupted if len(calls) == 1 else lambda: iter([body])),
        )

    with PyPIIndexClient(
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        clock=lambda: NOW,
    ) as client:
        observed = PackageDiscoveryPipeline(index_client=client, progress=events.append).run(
            replace(options(tmp_path), count=5, batch_size=2)
        )
    download = [event for event in events if event.stage == "downloading"]
    assert all(event.total is None for event in download)
    starts = [i for i, event in enumerate(download) if event.completed == 0]
    assert len(starts) == 2 and "attempt 2/3" in download[starts[1]].message
    assert download[starts[1] - 1].completed == 65536
    assert "Retrying" in download[starts[1] - 1].message
    for attempt in (download[: starts[1]], download[starts[1] :]):
        assert [event.completed for event in attempt] == sorted(
            event.completed for event in attempt
        )
    assert download[-1].completed == len(body)
    parsed = [event for event in events if event.stage == "parsing"]
    assert [event.completed for event in parsed] == sorted(event.completed for event in parsed)
    assert parsed[0].completed == 0 and parsed[-1].completed == len(body)
    assert all(event.total == len(body) for event in parsed)
    assert parsed[-1].items_seen == 3000
    exported = [event for event in events if event.stage == "exporting"]
    assert [event.completed for event in exported] == [0, 1, 2, 3]
    assert all(event.total == 3 for event in exported)
    assert events[-2].stage == "finalizing" and events[-1].stage == "completed"
    assert observed.manifest_path.is_file()

    # Both standalone parsing and offline replay expose real bytes, without HTTP.
    standalone = []
    parse_index(observed.snapshot_path, progress=standalone.append)
    assert standalone == parsed
    replay_options = replace(
        options(tmp_path),
        source="snapshot",
        snapshot=observed.snapshot_path,
        count=5,
        batch_size=2,
    )
    silent = PackageDiscoveryPipeline(progress=None).run(replay_options)
    replay_events = []
    replay = PackageDiscoveryPipeline(progress=replay_events.append).run(replay_options)
    assert replay_events[1].stage == "verifying"
    assert replay_events[1].total is None and replay_events[1].completed is None
    assert not any(event.stage == "downloading" for event in replay_events)
    assert (
        observed.targets_path.read_bytes()
        == silent.targets_path.read_bytes()
        == replay.targets_path.read_bytes()
    )
    assert [p.read_bytes() for p in sorted(silent.batches_dir.iterdir())] == [
        p.read_bytes() for p in sorted(replay.batches_dir.iterdir())
    ]
    before, after = read(silent.manifest_path), read(replay.manifest_path)
    for key in (
        "manifest_version",
        "source",
        "sampling",
        "counts",
        "parsing",
        "exclusion",
        "limitations",
    ):
        assert before[key] == after[key]
    assert before.keys() == after.keys()


def test_file_progress_is_byte_bounded_and_export_failure_never_completes(tmp_path, monkeypatch):
    import reposeer.pipelines.package_discovery as module

    names = tmp_path / "names.txt"
    names.write_bytes(b"# comment\n\n" + b"demo\nother\n" * 15000)
    opt = replace(options(tmp_path), source="file", input_file=names, batch_size=1)
    events = []
    result = PackageDiscoveryPipeline(progress=events.append).run(opt)
    reading = [event for event in events if event.stage == "reading_file"]
    assert len(reading) <= names.stat().st_size // 65536 + 2
    assert [event.completed for event in reading] == sorted(event.completed for event in reading)
    assert reading[-1].completed == reading[-1].total == names.stat().st_size
    assert reading[-1].items_seen == 30000
    assert verify(result, opt.data_dir)[0]["counts"]["unique_valid_candidates"] == 2

    original_write = module._write_targets

    def fail(path, selected):
        if path.name == "batch_002.txt":
            raise OSError("injected storage failure")
        original_write(path, selected)

    monkeypatch.setattr(module, "_write_targets", fail)
    events.clear()
    with pytest.raises(OSError):
        PackageDiscoveryPipeline(progress=events.append).run(opt)
    assert [event.completed for event in events if event.stage == "exporting"] == [0, 1]
    assert not any(event.stage == "completed" for event in events)
    assert list((opt.data_dir / "registry/runs").iterdir()) == [result.manifest_path.parent]


@pytest.mark.parametrize(
    "tty,ci,no_progress",
    [
        (False, None, False),
        (True, None, True),
        (True, "1", False),
        (True, None, False),
    ],
)
def test_cli_progress_stays_on_stderr_and_respects_tty_ci_and_opt_out(
    tmp_path,
    monkeypatch,
    tty,
    ci,
    no_progress,
):
    from io import StringIO

    from rich.console import Console

    import reposeer.cli.discover_packages as module

    terminal = StringIO()
    monkeypatch.setenv("TERM", "xterm-256color")

    def console(**kwargs):
        monkeypatch.setattr(module.sys.stderr, "isatty", lambda: tty)
        return Console(file=terminal, force_terminal=True, width=140)

    monkeypatch.setattr(module, "Console", console)
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    names = tmp_path / "names.txt"
    names.write_text("demo\nother\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "discover-packages",
            "--source",
            "file",
            "--input-file",
            str(names),
            "--count",
            "2",
            "--data-dir",
            str(tmp_path / "data"),
            *(["--no-progress"] if no_progress else []),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Selected packages: 2" in result.stdout and "\x1b" not in result.stdout
    if tty and ci is None and not no_progress:
        assert "Completed" in terminal.getvalue()
    else:
        assert terminal.getvalue() == "" and "\x1b" not in result.stderr


@pytest.mark.parametrize("failure", [PackageDiscoveryError("injected"), KeyboardInterrupt()])
def test_terminal_progress_closes_on_failure_or_interrupt(tmp_path, monkeypatch, failure):
    from io import StringIO

    from rich.console import Console

    import reposeer.cli.discover_packages as module
    from reposeer.clients.pypi_index import ProgressEvent

    renderers, terminal = [], StringIO()
    monkeypatch.setenv("TERM", "xterm-256color")
    original = module._DiscoveryProgress

    def renderer(console):
        instance = original(console)
        renderers.append(instance)
        return instance

    class FailedPipeline:
        def __init__(self, *, progress):
            self.progress = progress

        def run(self, options):
            self.progress(ProgressEvent("finalizing", message="Finalizing"))
            raise failure

    monkeypatch.setattr(module, "_DiscoveryProgress", renderer)
    monkeypatch.setattr(module, "PackageDiscoveryPipeline", FailedPipeline)

    def console(**kwargs):
        monkeypatch.setattr(module.sys.stderr, "isatty", lambda: True)
        return Console(file=terminal, force_terminal=True)

    monkeypatch.setattr(module, "Console", console)
    monkeypatch.delenv("CI", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "discover-packages",
            "--source",
            "file",
            "--input-file",
            "unused.txt",
            "--count",
            "1",
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code != 0
    assert renderers and not renderers[0].live.is_started
    assert "Completed" not in terminal.getvalue() and "Sampling Run ID" not in result.stdout
