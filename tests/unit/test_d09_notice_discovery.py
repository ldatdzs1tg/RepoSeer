import base64
import json
from concurrent.futures import CancelledError
from types import SimpleNamespace

from scripts import discover_d09_repo_notices as repo_notices
from scripts.d09_notice_signals import notice_document, notice_hits
from scripts.discover_d09_outcomes import discovery_query


def test_notice_discovery_keeps_context_and_separates_handover():
    hits = notice_hits(
        "The old API is no longer actively maintained.\nDevelopment has moved to repo B."
    )
    assert [(hit["kind"], hit["line"]) for hit in hits] == [
        ("cessation", 1),
        ("scope_or_transition", 2),
    ]
    assert hits[0]["excerpt"] == "The old API is no longer actively maintained."
    assert notice_hits("Dự án ngừng bảo trì.")[0]["kind"] == "cessation"
    assert notice_hits("This parser extracts archive files.") == []


def test_document_search_includes_versioned_notes_and_historical_policy_files():
    assert notice_document("README.rst")
    assert notice_document("docs/maintenance.md")
    assert notice_document("docs/releases/v1.0.md")
    assert notice_document("releasenotes/notes/123-issue.rst")
    assert not notice_document("dependencies/vendor/README.md")
    assert not notice_document("docs/../README.md")


def test_targeted_publisher_query_separates_years():
    sql = discovery_query(["real-package"], headlines=True, by_year=True)
    assert "GROUP BY name, upload_year" in sql
    assert "decommissioned" in sql
    assert "no future releases" in sql


def test_repository_search_uses_pinned_revision_and_preserves_cutoff(tmp_path, monkeypatch):
    earlier = {
        "sha": "a" * 40,
        "commit": {"committer": {"date": "2023-11-01T00:00:00Z"}, "tree": {"sha": "c" * 40}},
    }
    later = {
        "sha": "b" * 40,
        "commit": {"committer": {"date": "2024-04-01T00:00:00Z"}, "tree": {"sha": "d" * 40}},
    }
    document = "docs/maintenance.md"

    def request(_http, _folder, key, route):
        if key == "repository":
            payload = {"id": 42, "full_name": "owner/package", "private": False}
        elif key.endswith("-commit"):
            payload = [earlier if key == "cutoff-commit" else later]
        elif key.endswith("-tree"):
            payload = {"tree": [{"type": "blob", "path": document, "size": 50}]}
        elif key.endswith("-history"):
            payload = [later, earlier]
        elif key == "issue_or_pr":
            payload = [
                {
                    "created_at": "2024-03-01T00:00:00Z",
                    "body": "A feature request.",
                    "html_url": f"https://github.com/owner/package/issues/{number}",
                }
                for number in range(1, 101)
            ]
        elif key == "issue_or_pr-002":
            assert "page=2" in route
            payload = [
                {
                    "created_at": "2024-08-01T00:00:00Z",
                    "body": "This project is abandoned.",
                    "html_url": "https://github.com/owner/package/issues/101",
                }
            ]
        elif "/contents/" in route:
            assert "?ref=" in route
            text = (
                b"The package has been discontinued."
                if route.endswith(later["sha"])
                else b"The package is actively maintained."
            )
            payload = {
                "path": document,
                "encoding": "base64",
                "content": base64.b64encode(text).decode(),
            }
        else:
            payload = []
        return payload, {"path": "receipt.json", "content_sha256": "0" * 64}

    monkeypatch.setattr(repo_notices, "github_json", request)
    target = {
        "package_id": "pypi:package",
        "repo_group_id": "github:repository:42",
        "cutoff_at": "2024-01-01T00:00:00.000Z",
        "horizon_end_at": "2025-01-01T00:00:00.000Z",
    }
    report, hits = repo_notices.scan_target(
        None,
        SimpleNamespace(output_dir=tmp_path, max_documents=4, max_revisions=4, max_history_pages=2),
        target,
    )
    assert report["labels_assigned"] == 0
    assert report["limits"] == []
    assert [(hit["window"], hit["kind"], hit["revision"], hit["label"]) for hit in hits] == [
        ("within_horizon", "cessation", later["sha"], None),
        ("before_or_at_cutoff", "maintenance", earlier["sha"], None),
        ("within_horizon", "cessation", None, None),
    ]


def test_interrupt_saves_completed_notice_targets_without_waiting_in_input_order(
    tmp_path, monkeypatch
):
    targets = tmp_path / "targets.jsonl"
    targets.write_text(
        "".join(
            json.dumps(
                {
                    "package_id": f"pypi:{name}",
                    "repo_group_id": f"github:repository:{repo_id}",
                    "cutoff_at": "2024-01-01T00:00:00.000Z",
                    "horizon_end_at": "2025-01-01T00:00:00.000Z",
                }
            )
            + "\n"
            for repo_id, name in enumerate(("slow", "fast"), start=1)
        ),
        encoding="utf-8",
    )
    previous = tmp_path / "summary.json"
    previous.write_text("{}", encoding="utf-8")

    def scan(_http, _args, target, *, stop):
        subject = target["package_id"]
        if subject == "pypi:slow":
            assert stop.wait(timeout=5)
            raise CancelledError
        return {"package_id": subject, "status": "scanned"}, [{"package_id": subject}]

    def interrupt_on_report(line, *, flush):
        assert flush and json.loads(line)["package_id"] == "pypi:fast"
        raise KeyboardInterrupt

    monkeypatch.setattr(repo_notices, "scan_target", scan)
    monkeypatch.setattr(repo_notices, "print", interrupt_on_report, raising=False)
    result = repo_notices.run(
        SimpleNamespace(
            targets=targets, package=[], max_targets=2, use_gh=True, workers=2, output_dir=tmp_path
        )
    )

    assert result["interrupted"] and result["targets"] == 2
    assert result["scanned"] == result["leads"] == 1
    assert json.loads((tmp_path / "partial-summary.json").read_bytes()) == result
    assert json.loads((tmp_path / "partial-leads.jsonl").read_bytes()) == {
        "package_id": "pypi:fast"
    }
    assert previous.read_text(encoding="utf-8") == "{}"
