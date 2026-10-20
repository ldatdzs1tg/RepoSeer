"""Offline T-020 evidence, live handoffs, stored replay, profiling and public CLI boundaries."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import polars as pl
import pytest
from tests.integration.test_external_crawl_pipeline import HTML
from tests.unit.processing.test_normalization import activity_raw, document_raw, repository_raw
from typer.core import TyperOption
from typer.main import get_command
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.clients.github import GitHubClient
from reposeer.clients.pypi import PyPIClient
from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.config import settings
from reposeer.pipelines.collection import CollectionPipeline
from reposeer.pipelines.external import ExternalPipeline
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.profiling.report import ProfilingReportGenerator
from reposeer.services.activity_batch_service import ActivityBatchService
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.dataset_writer import DATASET_SCHEMAS
from reposeer.storage.raw.jsonl import JsonlStorage

NOW = datetime(2026, 10, 20, 10, tzinfo=UTC)
REPOS = {"pallets/flask": 900000001, "psf/requests": 900000002}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.collection, "cache_enabled", False)
    calls, options = [], {"mapping": "verified_current", "failed_repo": None, "empty": False, "fail_provider": False, "fail_crawler": False, "excluded_repo": None, "issues_prs": False}
    paths = StoragePaths(tmp_path / "root with spaces")

    def handler(request):
        calls.append(request)
        started = list((paths.metadata_dir / "collection_runs").glob("integration_*.json"))
        assert started and any(read(path)["finished_at"] is None for path in started)
        if request.url.host == "pypi.org":
            urls = {"Source": "https://github.com/pallets/flask"}
            if options["mapping"] == "unresolved":
                urls = {"Home": "https://github.com/pallets/flask"}
            elif options["mapping"] == "ambiguous":
                urls["Code"] = "https://github.com/psf/requests"
            return httpx.Response(200, json={"info": {"name": "Flask", "project_urls": urls}})
        if request.url.host == "api.github.com":
            parts = request.url.path.split("/")
            slug = "/".join(parts[2:4]).casefold()
            if slug == options["failed_repo"]:
                return httpx.Response(401, text="secret-provider-body")
            if len(parts) == 4:
                payload = repository_raw().payload
                payload.update(id=REPOS[slug], full_name=slug, html_url=f"https://github.com/{slug}",
                               topics=["flask"] if slug.endswith("flask") else ["requests"], private=False)
                if slug == options["excluded_repo"]:
                    payload["fork"] = True
                if options["mapping"] == "repository_mismatch" and slug.endswith("flask"):
                    payload["full_name"] = "other/unrelated"
                return httpx.Response(200, json=payload)
            group = {"pulls": "pull_requests"}.get(parts[4], parts[4])
            if group == "issues" and options["issues_prs"]:
                return httpx.Response(200, json=[dict(activity_raw("issues").payload, pull_request={})] * 100)
            return httpx.Response(200, json=[activity_raw(group).payload])
        if request.url.host == "hn.algolia.com":
            if options["fail_provider"]:
                return httpx.Response(401, text="secret-provider-body")
            name = "requests" if "requests" in request.url.params["query"].casefold() else "flask"
            hits = [] if options["empty"] else [{
                "objectID": "42" if name == "flask" else "43", "title": "Package tutorial",
                "url": f"https://blog.example/{name}", "created_at": "2024-01-01T00:00:00Z",
            }]
            return httpx.Response(200, json={"hits": hits, "nbPages": 1})
        if request.url.host == "blog.example":
            if options["fail_crawler"]:
                return httpx.Response(404, text="secret-provider-body")
            html = HTML.replace("synthetic package", "Flask package")
            html = html.replace("</article>", '<p>Use <a href="https://github.com/pallets/flask">Flask</a> to build Python web applications.</p></article>')
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        pytest.fail(f"Unexpected host: {request.url.host}")

    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        github = GitHubClient(http_client=http)
        pipeline = CollectionPipeline(
            paths, clock=lambda: NOW,
            pypi_factory=lambda: PyPIClient(http_client=http),
            activity_factory=lambda **kwargs: ActivityBatchService(client=github, **kwargs),
            discovery_factory=lambda **kwargs: ExternalPipeline(http_client=http, **kwargs),
            crawl_factory=lambda **kwargs: ExternalCrawlPipeline(
                http_client=http, policy=URLPolicy(lambda host: ["93.184.216.34"]), **kwargs))
        yield pipeline, paths, calls, options, http


def test_multi_target_full_path_preserves_identity_and_exact_discovery_lineage(environment):
    pipeline, paths, calls, _, http = environment
    result = pipeline.run(["pypi:Flask", "pallets/flask", "https://github.com/PSF/requests",
                           "https://github.com/PALLETS/flask", "pypi:flask"])
    assert result.status == "complete", read(result.manifest_path)
    assert result.targets_requested == result.targets_resolved == 5
    assert result.repositories_collected == 2
    assert sum(request.url.host == "pypi.org" for request in calls) == 1
    assert sum(request.url.path.casefold() == "/repos/pallets/flask" for request in calls) == 1
    assert sum(request.url.path.casefold() == "/repos/psf/requests" for request in calls) == 1
    assert not http._sync_client.is_closed
    manifest = read(result.manifest_path)
    assert manifest["targets"][0]["mapping_status"] == "verified_current"
    assert manifest["targets"][1]["package_id"] is None
    assert {entry["group"] for target in manifest["targets"] for entry in target["activity_group_statuses"]} == {
        "commits", "issues", "pull_requests", "contributors", "releases"}
    raw_repos = JsonlStorage().read_contract_records(paths.raw_dir / "github/repositories.jsonl")
    assert set(manifest["child_run_ids"]["github_metadata"]) == {raw.run_id for raw in raw_repos}
    technologies = [stage["details"]["detection"] for stage in manifest["stages"]
                    if stage["stage"] == "technology_resolution"]
    assert technologies and any(tech["entities"] for tech in technologies)
    documents = JsonlStorage().read_contract_records(paths.raw_dir / "external/documents.jsonl")
    discovery = JsonlStorage().read_contract_records(paths.raw_dir / "external/discovery.jsonl")
    by_id = {raw.raw_record_id: raw for raw in discovery}
    for document in documents:
        assert document.payload["candidate_raw_record_id"] in by_id
        original = by_id[document.payload["candidate_raw_record_id"]]
        assert original.run_id in manifest["child_run_ids"]["external_discovery"]
        assert document.payload["fetched_url"] == original.payload["candidate_url"]
        assert document.payload["discovery_lineage_complete"]
    assert {request.url.path for request in calls if request.url.host == "blog.example"} == {"/flask", "/requests"}
    quality = read(result.data_profile_path)
    assert len(quality["datasets"]) == len(DATASET_SCHEMAS)
    assert all(entry["present"] for entry in quality["datasets"].values())
    assert quality["datasets"]["github_activity"]["available_at_null_rate"] == 1
    assert quality["external_metrics"]["assessments"] > 0
    assert quality["external_metrics"]["documents_extracted"] >= 1
    assert read(result.collection_report_path)["totals"]["repositories_collected"] == 2
    assert result.processing_run_id and result.processing_run_id not in result.child_run_ids.get("github_activity", [])


@pytest.mark.parametrize("mapping,expected", [
    ("verified_current", "verified_current"), ("unresolved", "unresolved"),
    ("ambiguous", "ambiguous"), ("repository_mismatch", "unresolved"),
])
def test_package_mapping_never_invents_repository_scope(environment, mapping, expected):
    pipeline, _, _, options, _ = environment
    options["mapping"] = mapping
    result = pipeline.run(["pypi:flask"])
    target = read(result.manifest_path)["targets"][0]
    assert target["mapping_status"] == expected
    if mapping == "verified_current":
        assert target["repo_id"] == "github:repository:900000001"
        assert len(target["mapping_evidence_raw_ids"]) == 2
    else:
        assert target["repo_id"] is None
        assert result.status != "complete"
        assert not result.child_run_ids.get("external_crawl")


@pytest.mark.parametrize("failure", ["repository", "provider", "no_candidates", "unavailable", "crawler", "excluded", "activity_budget"])
def test_partial_sources_preserve_successful_artifacts_and_report_truth(environment, failure):
    pipeline, paths, _, options, _ = environment
    if failure == "repository":
        options["failed_repo"] = "psf/requests"
    elif failure == "excluded":
        options["excluded_repo"] = "psf/requests"
    elif failure == "activity_budget":
        options["issues_prs"] = True
    elif failure == "provider":
        options["fail_provider"] = True
    elif failure == "no_candidates":
        options["empty"] = True
    elif failure == "crawler":
        options["fail_crawler"] = True
    sources = ["hackernews", "rss"] if failure == "unavailable" else ["hackernews"]
    result = pipeline.run(["pypi:flask", "psf/requests"], discovery_sources=sources)
    manifest = read(result.manifest_path)
    assert result.processing_status == "complete"
    assert result.status == ("complete" if failure == "no_candidates" else "partial")
    raw_repositories = JsonlStorage().read_contract_records(paths.raw_dir / "github/repositories.jsonl")
    assert raw_repositories
    assert {raw.run_id for raw in raw_repositories}.issubset(set(result.child_run_ids["github_metadata"]))
    assert "secret-provider-body" not in result.manifest_path.read_text(encoding="utf-8")
    assert result.collection_report_path.exists() and result.data_profile_path.exists()
    if failure == "repository":
        by_target = {target["normalized_target"]: target for target in manifest["targets"]}
        assert by_target["pypi:flask"]["status"] == "complete"
        assert by_target["psf/requests"]["status"] != "complete"
        totals = read(result.collection_report_path)["totals"]
        assert totals["records_newly_stored"] is None
        assert totals["records_newly_stored_known"] > 0
    if failure == "activity_budget":
        sources = read(result.collection_report_path)["sources"]
        issues = [source for source in sources if source["source"] == "github_issues"]
        assert issues and all(source["records_stored"] == 0 and source["partial"] == 1
                              and source["failed"] == 0 for source in issues)
    if failure == "crawler":
        assert read(result.data_profile_path)["external_metrics"]["content_unavailable"] > 0
    if failure == "unavailable":
        sources = read(result.collection_report_path)["sources"]
        assert any(source["source"] == "rss" and source["attempted"] == 0
                   and source["state"] == "failed" for source in sources)
    if failure in {"provider", "no_candidates"}:
        assert any(stage["reason_codes"] == ["no_discovery_candidates"] for stage in manifest["stages"])
        assert not result.child_run_ids.get("external_crawl")


def test_stored_replay_no_network_no_new_raw_and_scope_is_root_snapshot(environment):
    pipeline, paths, _, _, http = environment
    live = pipeline.run(["pypi:flask"])
    raw_before = {path: path.read_bytes() for path in paths.raw_dir.rglob("*.jsonl")}
    rows_before = {name: pl.read_parquet(paths.processed_dir / name / f"{name}.parquet").height
                   for name in DATASET_SCHEMAS}
    def forbidden(**kwargs):
        pytest.fail("Stored mode constructed a network client")
    stored = CollectionPipeline(paths, clock=lambda: NOW, pypi_factory=forbidden,
                                activity_factory=forbidden, discovery_factory=forbidden, crawl_factory=forbidden)
    first = stored.run(["pypi:flask"], mode="stored", cutoff_at="2024-01-01T00:00:00.000Z")
    second = stored.run(["pypi:flask"], mode="stored")
    assert first.status == second.status == "complete"
    assert not http._sync_client.is_closed
    assert all(path.read_bytes() == data for path, data in raw_before.items())
    assert set(paths.raw_dir.rglob("*.jsonl")) == set(raw_before)
    for name, count in rows_before.items():
        actual = pl.read_parquet(paths.processed_dir / name / f"{name}.parquet").height
        assert actual == count if name != "processing_audit" else actual > count
    saved = read(second.manifest_path)
    assert saved["child_run_ids"] == {}
    assert saved["targets"][0]["mapping_status"] == "verified_current"
    assert all(stage["reason_codes"] == ["stored_mode_no_network"] for stage in saved["stages"]
               if stage["stage"] in {"github_collection", "external_discovery", "external_crawl"})
    assert read(second.collection_report_path)["totals"]["records_newly_stored"] == 0
    assert read(second.data_profile_path)["scope"] == "storage_root_snapshot_after_run"
    assert all(count == 0 for name, count in saved["processing"]["dataset_row_counts"].items()
               if name != "processing_audit")
    assert live.manifest_path.exists()


def test_stored_missing_package_evidence_stays_unresolved(tmp_path):
    paths = StoragePaths(tmp_path)
    JsonlStorage().append_contract_record(paths.raw_path("github", "repositories"), repository_raw())
    result = CollectionPipeline(paths, clock=lambda: NOW).run(["pypi:flask"], mode="stored")
    assert result.status == "partial"
    target = read(result.manifest_path)["targets"][0]
    assert target["repo_id"] is None and target["mapping_status"] == "unresolved"
    assert target["missing_reasons"] == ["missing_stored_evidence"]


def test_profile_nulls_duplicates_freshness_relevance_and_missing_tables(tmp_path):
    paths = StoragePaths(tmp_path)
    repo = repository_raw()
    other = repository_raw(raw_record_id="raw:" + str(uuid4()))
    other.retrieved_at = "2026-10-19T10:00:00.000Z"
    JsonlStorage().append_contract_records_durable(paths.raw_path("github", "repositories"), [repo, repo, other])
    JsonlStorage().append_contract_record(paths.raw_path("external", "documents"), document_raw())
    result = CollectionPipeline(paths, clock=lambda: NOW).run(["Synthetic/Demo"], mode="stored")
    quality = read(result.data_profile_path)
    repositories = quality["datasets"]["repositories"]
    assert repositories["row_count"] == 2
    assert repositories["max_retrieved_at"] == other.retrieved_at
    assert repositories["latest_observation_age_seconds"] == 86400
    assert repositories["null_rates"]["available_at"] == 0  # Existing T-013 current-observation policy.
    assert quality["datasets"]["github_activity"]["present"] is False
    assert quality["datasets"]["github_activity"]["row_count"] is None
    assert not (paths.processed_dir / "github_activity/github_activity.parquet").exists()
    metrics = quality["processing_metrics"]
    assert metrics["input_record_count"] == 4 and metrics["duplicate_input_count"] == 1
    assert metrics["unique_raw_observation_count"] == 3 and metrics["duplicate_input_rate"] == 0.25
    assert quality["external_metrics"]["relevance_status"] == "not_assessed"
    assert quality["external_metrics"]["relevance_rate"] is None
    assert quality["source_coverage"] == "not_verified" and quality["historical_validity"] == "unverified"
    profile_cli = CliRunner().invoke(app, ["profile", "--data-dir", str(paths.base_dir)])
    assert profile_cli.exit_code == 0, profile_cli.output
    report = read(paths.reports_dir / "data_quality/profile_report.json")
    assert set(report["datasets"]) == set(DATASET_SCHEMAS)
    legacy = ProfilingReportGenerator(clock=lambda: NOW).generate_report({
        "missing": tmp_path / "missing.parquet",
        "repositories": paths.processed_dir / "repositories/repositories.parquet"})
    assert set(legacy["datasets"]) == {"repositories"}
    assert legacy["datasets"]["repositories"]["stats"]["row_count"] == 2
    empty = pl.DataFrame(schema={"available_at": pl.String})
    stats = ProfilingReportGenerator(clock=lambda: NOW).dataset_profiler.profile_dataframe(empty)
    assert stats["null_rates"]["available_at"] is None


@pytest.mark.parametrize("invalid", [
    ["--max-pages", "0"], ["--discovery-max-records", "0"], ["--max-candidates", "1001"],
    ["--groups", "unknown"], ["--discovery-sources", "unknown"],
    ["--cutoff-at", "2024-01-01T00:00:00.000Z"],
])
def test_cli_validates_configuration_before_any_network(tmp_path, monkeypatch, invalid):
    import reposeer.cli.collect_dataset as cli
    monkeypatch.setattr(cli, "CollectionPipeline", lambda *args, **kwargs: pytest.fail("Invalid config reached pipeline"))
    targets = tmp_path / "targets.txt"
    targets.write_text("# Full-line comment\n\npypi:flask\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["collect-dataset", "--targets-file", str(targets), *invalid])
    assert result.exit_code == 2


def test_cli_metadata_bad_targets_and_stored_exit_codes(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["collect-dataset", "--help"]).exit_code == 0
    options = {option for parameter in get_command(app).commands["collect-dataset"].params
               if isinstance(parameter, TyperOption) for option in (*parameter.opts, *parameter.secondary_opts)}
    assert {"--targets-file", "--data-dir", "--mode", "--groups", "--max-pages", "--max-records",
            "--discovery-sources", "--discovery-max-pages", "--discovery-max-records",
            "--max-candidates", "--cutoff-at"}.issubset(options)
    targets = tmp_path / "targets.txt"
    targets.write_text("https://user:secret@github.com/a/b\n", encoding="utf-8")
    invalid = runner.invoke(app, ["collect-dataset", "--targets-file", str(targets)])
    assert invalid.exit_code == 2 and "secret" not in invalid.output
    paths = StoragePaths(tmp_path / "data")
    raw = repository_raw()
    JsonlStorage().append_contract_record(paths.raw_path("github", "repositories"), raw)
    targets.write_text("Synthetic/Demo\n", encoding="utf-8")
    good = runner.invoke(app, ["collect-dataset", "--targets-file", str(targets),
                               "--data-dir", str(paths.base_dir), "--mode", "stored"])
    assert good.exit_code == 0, good.output
    targets.write_text("pypi:missing\n", encoding="utf-8")
    partial = runner.invoke(app, ["collect-dataset", "--targets-file", str(targets),
                                  "--data-dir", str(paths.base_dir), "--mode", "stored"])
    assert partial.exit_code == 1
    assert {"collect", "collect-activity", "detect-tech", "discover-external", "crawl-external", "process", "profile"}.issubset(
        get_command(app).commands)



@pytest.mark.parametrize("stage", ["profiling", "activity_persistence"])
def test_interruption_keeps_child_artifacts_and_failed_stage_manifest(environment, monkeypatch, stage):
    pipeline, paths, _, _, _ = environment
    original_factory = pipeline.activity_factory
    if stage == "profiling":
        def fail_report(report, path):
            raise OSError("private-error-value")
        monkeypatch.setattr(pipeline.profiler, "save_report", fail_report)
    else:
        def interrupted_factory(**kwargs):
            service = original_factory(**kwargs)
            append = service.activity_service.raw_storage.append_contract_records_durable
            def interrupt(path, records):
                append(path, records)
                raise OSError("private-error-value")
            service.activity_service.raw_storage.append_contract_records_durable = interrupt
            return service
        pipeline.activity_factory = interrupted_factory
    result = pipeline.run(["pypi:flask"])
    assert result.status == "partial" and result.processing_status == "complete"
    saved = read(result.manifest_path)
    assert "private-error-value" not in result.manifest_path.read_text(encoding="utf-8")
    assert result.collection_report_path.exists()
    assert JsonlStorage().read_contract_records(paths.raw_dir / "github/repositories.jsonl")
    if stage == "profiling":
        assert any(item["code"] == "profiling_failed" for item in saved["errors"])
        assert not result.data_profile_path.exists()
    else:
        assert any(entry["details"].get("resumable") for entry in saved["stages"])
        run_id = result.child_run_ids["github_activity"][0]
        service = original_factory(storage_paths=paths, clock=lambda: NOW)
        try:
            recovered = service.resume(run_id)
            assert recovered.raw_stored_count > 0
        finally:
            service.close()
        raw = JsonlStorage().read_contract_records(paths.raw_dir / "github/commits.jsonl")
        assert len(raw) == len({record.raw_record_id for record in raw}) == 1


def test_default_live_adapters_share_and_close_owned_transport(environment, monkeypatch):
    import reposeer.pipelines.collection as module
    _, paths, _, _, http = environment
    created = []
    def transport(**kwargs):
        created.append(kwargs)
        return http
    monkeypatch.setattr(module, "HTTPClient", transport)
    result = CollectionPipeline(paths, clock=lambda: NOW,
                                url_policy=URLPolicy(lambda host: ["93.184.216.34"])).run(["pypi:flask"])
    assert result.status == "complete", read(result.manifest_path)
    assert len(created) == 1 and http._sync_client.is_closed


@pytest.mark.parametrize("targets", [
    [], ["a/b"] * 101, ["pypi:bad name"], ["https://github.com/a/b/tree/main"],
    ["http://github.com/a/b"], ["a/b # inline comment"],
])
def test_invalid_targets_fail_before_manifest_or_network(tmp_path, targets):
    def forbidden(**kwargs):
        pytest.fail("Invalid targets constructed a network client")
    pipeline = CollectionPipeline(StoragePaths(tmp_path), pypi_factory=forbidden,
                                  activity_factory=forbidden, discovery_factory=forbidden,
                                  crawl_factory=forbidden)
    with pytest.raises(ValueError):
        pipeline.run(targets)
    assert not (tmp_path / "metadata").exists()
