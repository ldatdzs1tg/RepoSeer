"""Collect cutoff-scoped first-party D09 leads from pinned docs and GitHub history."""

import argparse
import base64
import json
import re
import subprocess
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from urllib.parse import quote, urlencode

from scripts.collect_d09_batch import parse_rows
from scripts.collect_d09_outcome_sources import capture
from scripts.collect_d09_pilot import provider_time, run_lock, stored_source_matches
from scripts.d09_notice_signals import notice_document, notice_hits
from scripts.run_d09_drive import digest, jsonl
from scripts.verify_d09_evidence import rounded_up_time
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import (
    add_calendar_months,
    checked_timestamp,
    format_utc_timestamp,
    parse_repo_id,
)
from reposeer.contracts.identity import package_id as canonical_package
from reposeer.exceptions import HTTPClientError

API = "https://api.github.com/"
MAX_DOC_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def github_json(http, folder: Path, key: str, route: str) -> tuple[dict | list, dict]:
    url = API + route
    if http is not None:
        return capture(http, folder, key, url)
    path = folder / f"{key}.receipt.json"
    receipt = json.loads(path.read_bytes()) if path.exists() else {}
    if receipt.get("url") == url and stored_source_matches(folder, receipt):
        return json.loads((folder / receipt["path"]).read_bytes()), receipt
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", route],
        capture_output=True,
        timeout=40,
        check=False,
    )
    if result.returncode:
        code = re.search(rb"HTTP (\d{3})", result.stderr)
        raise HTTPClientError(
            f"GitHub request failed for {route}", status_code=int(code[1]) if code else None
        )
    body = result.stdout
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("GitHub response exceeded the evidence size budget")
    payload = json.loads(body)
    sha = digest(body)
    receipt = {
        "path": f"{key}-{sha}.json",
        "url": url,
        "content_sha256": sha,
        "retrieved_at": format_utc_timestamp(datetime.now(UTC)),
    }
    atomic_bytes(folder / receipt["path"], body, immutable=True)
    write_json_atomic(path, receipt)
    return payload, receipt


def checked_targets(path: Path, packages: list[str], maximum: int) -> list[dict]:
    wanted = {canonical_package(name.removeprefix("pypi:")) for name in packages}
    selected = []
    keys = set()
    for row in parse_rows(path.read_bytes()):
        subject, cutoff = row["package_id"], checked_timestamp(row["cutoff_at"])
        if wanted and subject not in wanted:
            continue
        group = row["repo_group_id"]
        parse_repo_id(group)
        if subject != canonical_package(subject.removeprefix("pypi:")) or (
            row["horizon_end_at"] != add_calendar_months(cutoff, 12)
        ):
            raise ValueError("a notice target has invalid package identity or horizon")
        key = (subject, cutoff)
        if key in keys:
            raise ValueError(f"duplicate notice target {key}")
        keys.add(key)
        selected.append(row)
    if not selected:
        raise ValueError("no notice targets matched the selection")
    return selected[:maximum]


def commit_at(row: dict) -> str:
    return rounded_up_time(provider_time(row["commit"]["committer"]["date"]))


def pinned_sha(sha: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("expected a pinned Git commit")
    return sha


def scan_target(http, args, target: dict, *, stop: Event | None = None) -> tuple[dict, list[dict]]:
    subject = target["package_id"]
    cutoff, horizon = target["cutoff_at"], target["horizon_end_at"]
    identity = parse_repo_id(target["repo_group_id"])
    folder = args.output_dir / subject.removeprefix("pypi:") / digest(cutoff.encode())[:12]
    folder.mkdir(parents=True, exist_ok=True)

    def request(key: str, endpoint: str):
        if stop is not None and stop.is_set():
            raise CancelledError
        return github_json(http, folder, key, endpoint)

    route = f"repositories/{identity}"
    repo, _ = request("repository", route)
    if str(repo["id"]) != identity or repo["private"]:
        raise ValueError("repository must be public and match the checked mapping")
    report = {
        "package_id": subject,
        "repo_group_id": target["repo_group_id"],
        "cutoff_at": cutoff,
        "horizon_end_at": horizon,
        "repo": repo["full_name"],
        "status": "scanned",
        "docs_scanned": 0,
        "doc_revisions": 0,
        "limits": [],
    }
    leads = []

    def emit(text, at, channel, source, url, *, revision=None):
        window = (
            "before_or_at_cutoff"
            if at <= cutoff
            else "within_horizon"
            if at <= horizon
            else "after_horizon"
        )
        for hit in notice_hits(text):
            leads.append(
                {
                    "package_id": subject,
                    "cutoff_at": cutoff,
                    "horizon_end_at": horizon,
                    "repo_group_id": target["repo_group_id"],
                    "source_kind": channel,
                    "source_url": url,
                    "source_timestamp": at,
                    "window": window,
                    "revision": revision,
                    "source_path": str((folder / source["path"]).resolve()),
                    "source_sha256": source["content_sha256"],
                    "label": None,
                    **hit,
                }
            )

    paths = set()
    boundaries = {}
    for name, at in (("cutoff", cutoff), ("horizon", horizon)):
        commits, _ = request(
            name + "-commit",
            route + "/commits?" + urlencode({"until": at, "per_page": 1}),
        )
        if not commits or commit_at(commits[0]) > at:
            report["limits"].append(name + "_revision_unavailable")
            continue
        boundary = commits[0]
        tree_sha = pinned_sha(boundary["commit"]["tree"]["sha"])
        tree, _ = request(name + "-tree", f"{route}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            report["limits"].append(name + "_tree_truncated")
        available = {
            entry["path"]
            for entry in tree["tree"]
            if entry["type"] == "blob"
            and entry.get("size", 0) <= MAX_DOC_BYTES
            and notice_document(entry["path"])
        }
        boundaries[name] = (boundary, available)
        paths.update(available)
    if len(paths) > args.max_documents:
        report["limits"].append("document_budget_reached")
    # Root status and release docs precede deep trees with many docs landing pages.
    ordered = sorted(
        paths,
        key=lambda path: (
            path.count("/"),
            not Path(path)
            .stem.lower()
            .startswith(("readme", "changelog", "maintenance", "status", "news")),
            path.lower(),
        ),
    )
    for path in ordered[: args.max_documents]:
        key = "doc-" + digest(path.encode())[:12]
        versions, _ = request(
            key + "-history",
            route
            + "/commits?"
            + urlencode({"path": path, "until": horizon, "per_page": args.max_revisions + 1}),
        )
        if len(versions) > args.max_revisions and commit_at(versions[args.max_revisions]) > cutoff:
            report["limits"].append("revision_budget_reached:" + path)
        revisions = {
            pinned_sha(row["sha"]): row
            for row in versions[: args.max_revisions]
            if commit_at(row) <= horizon
        }
        if "cutoff" in boundaries:
            before, before_paths = boundaries["cutoff"]
            if path in before_paths and not any(
                commit_at(row) <= cutoff for row in revisions.values()
            ):
                revisions[pinned_sha(before["sha"])] = before
        for sha, row in revisions.items():
            try:
                doc, source = request(
                    f"{key}-{sha[:12]}", f"{route}/contents/{quote(path)}?ref={sha}"
                )
            except HTTPClientError as error:
                if error.status_code != 404:
                    raise
                report["limits"].append("document_revision_unavailable:" + path)
                continue
            if (
                doc.get("path") != path
                or doc.get("encoding") != "base64"
                or doc.get("size", 0) > MAX_DOC_BYTES
            ):
                report["limits"].append("document_not_readable:" + path)
                continue
            body = base64.b64decode(doc["content"])
            document_path = f"{key}-{sha[:12]}-{digest(body)}.txt"
            atomic_bytes(folder / document_path, body, immutable=True)
            raw = {**source, "path": document_path, "content_sha256": digest(body)}
            emit(
                body.decode("utf-8", errors="replace"),
                commit_at(row),
                "pinned_document",
                raw,
                f"https://github.com/{repo['full_name']}/blob/{sha}/{quote(path)}",
                revision=sha,
            )
            report["doc_revisions"] += 1
        report["docs_scanned"] += 1

    for kind, suffix, date in (
        ("release_note", "releases?per_page=100", "published_at"),
        ("issue_or_pr", "issues?state=all&sort=created&direction=asc&per_page=100", "created_at"),
        ("issue_comment", "issues/comments?sort=created&direction=asc&per_page=100", "created_at"),
    ):
        last_at = ""
        for page in range(1, args.max_history_pages + 1):
            key, endpoint = (
                (kind, suffix) if page == 1 else (f"{kind}-{page:03d}", f"{suffix}&page={page}")
            )
            try:
                records, source = request(key, route + "/" + endpoint)
            except HTTPClientError as error:
                report["limits"].append(f"history_unavailable:{kind}:{error.status_code}")
                break
            if not isinstance(records, list):
                raise ValueError("expected GitHub history page")
            dated = [
                (rounded_up_time(provider_time(record[date])), record)
                for record in records
                if record.get(date)
            ]
            for at, record in dated:
                text = (
                    (record.get("title") or record.get("name") or "")
                    + "\n"
                    + (record.get("body") or "")
                )
                emit(
                    text,
                    at,
                    kind,
                    source,
                    record["html_url"],
                )
            if len(records) < 100:
                break
            if kind != "release_note":
                times = [last_at, *(at for at, _ in dated)]
                if len(dated) != len(records) or times != sorted(times):
                    report["limits"].append("history_order_unverified:" + kind)
                    break
                last_at = times[-1]
                if last_at > horizon:
                    break
        else:
            report["limits"].append("history_page_capped:" + kind)
    report["leads"] = len(leads)
    report["labels_assigned"] = 0
    return report, leads


def run(args) -> dict:
    targets = checked_targets(args.targets, args.package, args.max_targets)
    stop = Event()

    def scan(row):
        try:
            if args.use_gh:
                return scan_target(None, args, row, stop=stop)
            with HTTPClient(timeout=30, max_retries=1) as http:
                return scan_target(http, args, row, stop=stop)
        except (HTTPClientError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            return {
                "package_id": row["package_id"],
                "cutoff_at": row["cutoff_at"],
                "status": "incomplete",
                "reason": str(error),
            }, []

    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = []
    try:
        for target in targets:
            futures.append(pool.submit(scan, target))
        for future in as_completed(futures):
            report, _ = future.result()
            print(json.dumps(report, ensure_ascii=True), flush=True)
    except KeyboardInterrupt:
        stop.set()
    finally:
        pool.shutdown(cancel_futures=stop.is_set())

    # Keep the saved output in target order while printing results as they finish.
    reports, leads = [], []
    for future in futures:
        try:
            report, found = future.result()
        except CancelledError:
            continue
        reports.append(report)
        leads.extend(found)
    prefix = "partial-" if stop.is_set() else ""
    atomic_bytes(args.output_dir / f"{prefix}leads.jsonl", jsonl(leads), immutable=False)
    result = {
        "targets": len(targets),
        "scanned": sum(row["status"] == "scanned" for row in reports),
        "leads": len(leads),
        "labels_assigned": 0,
        "reports": reports,
    }
    if stop.is_set():
        result["interrupted"] = True
    write_json_atomic(args.output_dir / f"{prefix}summary.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targets",
        type=Path,
        required=True,
        help="JSONL of verified package, repo and cutoff candidates",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument(
        "--use-gh", action="store_true", help="Use an existing GitHub CLI login for public reads"
    )
    parser.add_argument("--max-targets", type=int, default=24)
    parser.add_argument("--max-documents", type=int, default=8)
    parser.add_argument("--max-revisions", type=int, default=4)
    parser.add_argument("--max-history-pages", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if (
        min(args.max_targets, args.max_documents) < 1
        or not 1 <= args.max_revisions <= 99
        or not 1 <= args.max_history_pages <= 20
        or not 1 <= args.workers <= 6
    ):
        parser.error("expected positive scan budgets, 1–99 revisions, 1–20 pages and 1–6 workers")
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output_dir):
            result = run(args)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Repository notice scan stopped: {error}\n")
    if result.get("interrupted"):
        print(
            f"Interrupted after {len(result['reports'])}/{result['targets']} targets. "
            f"Saved {result['leads']} leads to partial-leads.jsonl. "
            "Run the same command again to reuse cached responses."
        )
        return 130
    print(
        f"Scanned {result['scanned']}/{result['targets']} targets. Saved {result['leads']} leads."
    )
    return 0 if result["scanned"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
