"""Streaming official PyPI index acquisition and immutable sampling-source artifacts."""

import codecs
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from reposeer.collection.http.retry import is_retryable_exception
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import checked_timestamp, format_utc_timestamp, normalize_pypi_name

INDEX_URL = "https://pypi.org/simple/"
INDEX_ACCEPT = "application/vnd.pypi.simple.v1+html"
CONTENT_TYPES = {INDEX_ACCEPT, "text/html"}
CHUNK_SIZE = 65536
MAX_INDEX_BYTES = 536870912
DEFAULT_INDEX_BYTES = 268435456
logger = logging.getLogger("reposeer.pypi_index")


class PackageDiscoveryError(ValueError):
    """Controlled discovery failure; its message never contains source bodies or secrets."""


@dataclass(frozen=True)
class ProgressEvent:
    """Ephemeral acquisition/sampling observations, never persisted in artifacts."""

    stage: str
    completed: int | None = None
    total: int | None = None
    message: str | None = None
    items_seen: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


def report_progress(progress: ProgressCallback | None, stage: str, **values) -> None:
    if progress is not None:
        progress(ProgressEvent(stage, **values))


@dataclass(frozen=True)
class IndexSnapshot:
    path: Path
    sha256: str
    decoded_byte_count: int
    source_url: str
    retrieved_at: str
    content_type: str
    last_serial: int | None
    etag: str | None
    last_modified: str | None

    def metadata(self) -> dict:
        return dict(
            snapshot_version="1",
            source_kind="pypi_simple_html",
            source_url=self.source_url,
            content_type=self.content_type,
            sha256=self.sha256,
            decoded_byte_count=self.decoded_byte_count,
            retrieved_at=self.retrieved_at,
            last_serial=self.last_serial,
            etag=self.etag,
            last_modified=self.last_modified,
        )


def validate_byte_budget(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INDEX_BYTES:
        raise ValueError("max_index_bytes_must_be_between_1_and_536870912")


def file_fingerprint(path: Path, max_bytes: int | None = None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise PackageDiscoveryError("index_byte_budget_exceeded")
            digest.update(chunk)
    return digest.hexdigest(), size


class IndexHTMLParser(HTMLParser):
    """Incremental index structure/link validation; only one anchor is retained."""

    def __init__(self, on_project: Callable[[str], None] | None = None):
        super().__init__(convert_charrefs=True)
        self.on_project = on_project
        self.anchors_seen = self.valid_project_entries = self.invalid_entries = 0
        self.html_seen = self.body_seen = self.body_closed = self.html_closed = False
        self.anchor: list[str] | None = None
        self.href: str | None = None
        self.anchor_size = 0

    def handle_starttag(self, tag, attrs):
        if self.html_closed:
            raise PackageDiscoveryError("malformed_index_html")
        if tag == "html":
            if self.html_seen:
                raise PackageDiscoveryError("malformed_index_html")
            self.html_seen = True
        elif tag == "body":
            if not self.html_seen or self.body_seen:
                raise PackageDiscoveryError("malformed_index_html")
            self.body_seen = True
        elif tag == "a":
            if self.anchor is not None:
                raise PackageDiscoveryError("malformed_index_html")
            self.anchors_seen += 1
            self.anchor, self.anchor_size = [], 0
            hrefs = [value for name, value in attrs if name == "href"]
            self.href = (
                hrefs[0] if len(hrefs) == 1 and self.body_seen and not self.body_closed else None
            )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in {"html", "body", "a"}:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.html_closed and data.strip():
            raise PackageDiscoveryError("malformed_index_html")
        if self.anchor is not None:
            self.anchor_size += len(data)
            if self.anchor_size > 1048576:
                raise PackageDiscoveryError("index_token_budget_exceeded")
            self.anchor.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            try:
                href = self.href
                if (
                    not href
                    or any(c.isspace() or ord(c) < 32 for c in href)
                    or any(c in href for c in "?#\\")
                ):
                    raise ValueError("invalid_project_href")
                parts = urlsplit(urljoin(INDEX_URL, href))
                match = re.fullmatch(r"/simple/([A-Za-z0-9._-]+)/", parts.path)
                if parts.scheme != "https" or parts.netloc.lower() != "pypi.org" or not match:
                    raise ValueError("invalid_project_href")
                name = normalize_pypi_name(match[1])
                if normalize_pypi_name("".join(self.anchor).strip()) != name:
                    raise ValueError("project_link_name_mismatch")
            except ValueError:
                self.invalid_entries += 1
            else:
                self.valid_project_entries += 1
                if self.on_project is not None:
                    self.on_project(name)
            self.anchor, self.href = None, None
        elif tag == "body":
            if not self.body_seen or self.body_closed or self.anchor is not None:
                raise PackageDiscoveryError("malformed_index_html")
            self.body_closed = True
        elif tag == "html":
            if not self.html_seen or not self.body_closed or self.html_closed:
                raise PackageDiscoveryError("malformed_index_html")
            self.html_closed = True

    def feed(self, data):
        super().feed(data)
        if len(self.rawdata) > 1048576:
            raise PackageDiscoveryError("index_token_budget_exceeded")

    def finish(self):
        self.close()
        if (
            not (self.html_seen and self.body_seen and self.body_closed and self.html_closed)
            or self.anchor is not None
            or self.rawdata.strip()
        ):
            raise PackageDiscoveryError("truncated_index_html")


def parse_index(
    path: Path,
    on_project: Callable[[str], None] | None = None,
    *,
    max_bytes: int = DEFAULT_INDEX_BYTES,
    progress: ProgressCallback | None = None,
) -> tuple[IndexHTMLParser, str, int]:
    parser, decoder = IndexHTMLParser(on_project), codecs.getincrementaldecoder("utf-8-sig")()
    digest, size = hashlib.sha256(), 0
    total = path.stat().st_size if progress is not None else None
    report_progress(progress, "parsing", completed=0, total=total, items_seen=0)
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise PackageDiscoveryError("index_byte_budget_exceeded")
                digest.update(chunk)
                parser.feed(decoder.decode(chunk))
                report_progress(
                    progress,
                    "parsing",
                    completed=size,
                    total=total,
                    items_seen=parser.valid_project_entries,
                )
        parser.feed(decoder.decode(b"", final=True))
        parser.finish()
    except UnicodeError as error:
        raise PackageDiscoveryError("invalid_index_utf8") from error
    report_progress(
        progress,
        "parsing",
        completed=size,
        total=total,
        items_seen=parser.valid_project_entries,
    )
    return parser, digest.hexdigest(), size


def load_snapshot(
    path: Path,
    *,
    max_bytes: int = DEFAULT_INDEX_BYTES,
    progress: ProgressCallback | None = None,
) -> IndexSnapshot:
    validate_byte_budget(max_bytes)
    # Several full reads follow; an indeterminate phase avoids double-counting bytes.
    report_progress(progress, "verifying", message="Verifying snapshot")
    try:
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        if (
            not isinstance(meta, dict)
            or meta.get("snapshot_version") != "1"
            or meta.get("source_kind") != "pypi_simple_html"
            or meta.get("source_url") != INDEX_URL
            or meta.get("content_type", "").split(";")[0].strip().lower() not in CONTENT_TYPES
            or not isinstance(meta.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
            or path.name != meta["sha256"] + ".html"
            or type(meta.get("decoded_byte_count")) is not int
            or meta["decoded_byte_count"] < 0
            or (
                meta.get("last_serial") is not None
                and (type(meta["last_serial"]) is not int or meta["last_serial"] < 0)
            )
            or any(
                meta.get(key) is not None and not isinstance(meta[key], str)
                for key in ("etag", "last_modified")
            )
        ):
            raise ValueError("invalid_metadata")
        checked_timestamp(meta.get("retrieved_at"))
    except (ValueError, TypeError, AttributeError) as error:
        raise PackageDiscoveryError("invalid_snapshot_metadata") from error
    # Check bytes first so a changed HTML byte always reports checksum mismatch.
    digest, size = file_fingerprint(path, max_bytes)
    if digest != meta["sha256"] or size != meta["decoded_byte_count"]:
        raise PackageDiscoveryError("snapshot_checksum_mismatch")
    _, parsed_hash, parsed_size = parse_index(path, max_bytes=max_bytes)
    if (parsed_hash, parsed_size) != (digest, size):
        raise PackageDiscoveryError("snapshot_changed_during_validation")
    return IndexSnapshot(
        path,
        digest,
        size,
        INDEX_URL,
        meta["retrieved_at"],
        meta["content_type"],
        meta.get("last_serial"),
        meta.get("etag"),
        meta.get("last_modified"),
    )


def _publish_snapshot(
    temporary: Path, snapshot: IndexSnapshot, destination_dir: Path
) -> IndexSnapshot:
    destination = destination_dir / f"{snapshot.sha256}.html"
    sidecar = destination.with_suffix(".json")
    if destination.exists():
        if file_fingerprint(destination) != (snapshot.sha256, snapshot.decoded_byte_count):
            raise PackageDiscoveryError("existing_snapshot_checksum_mismatch")
        if sidecar.exists():
            return load_snapshot(destination, max_bytes=MAX_INDEX_BYTES)
    elif sidecar.exists():
        raise PackageDiscoveryError("orphan_snapshot_metadata")
    else:
        os.replace(temporary, destination)
    if not sidecar.exists():
        write_json_atomic(sidecar, snapshot.metadata())
    return IndexSnapshot(
        destination,
        snapshot.sha256,
        snapshot.decoded_byte_count,
        snapshot.source_url,
        snapshot.retrieved_at,
        snapshot.content_type,
        snapshot.last_serial,
        snapshot.etag,
        snapshot.last_modified,
    )


def copy_snapshot(snapshot: IndexSnapshot, destination_dir: Path) -> IndexSnapshot:
    """Import verified bytes into the selected root, retaining original source metadata."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    if snapshot.path.resolve() == (destination_dir / snapshot.path.name).resolve():
        return load_snapshot(snapshot.path, max_bytes=MAX_INDEX_BYTES)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination_dir, suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            digest, size = hashlib.sha256(), 0
            with snapshot.path.open("rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > snapshot.decoded_byte_count:
                        raise PackageDiscoveryError("snapshot_changed_during_copy")
                    digest.update(chunk)
                    output.write(chunk)
            if (digest.hexdigest(), size) != (snapshot.sha256, snapshot.decoded_byte_count):
                raise PackageDiscoveryError("snapshot_changed_during_copy")
            output.flush()
            os.fsync(output.fileno())
        return _publish_snapshot(temporary, snapshot, destination_dir)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PyPIIndexClient:
    """Own a public HTTPX session or borrow an injected session without closing it.

    Streaming remains separate from the shared JSON client: that client's bounded
    response helper materializes decoded bytes. Transient classification is shared.
    All redirects are rejected; the fixed official endpoint needs no redirect.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | float | None = None,
        max_attempts: int = 3,
        max_wait_seconds: float = 10,
        total_wait_seconds: float = 30,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ):
        if client is not None and transport is not None:
            raise ValueError("choose_client_or_transport")
        self.timeout = (
            httpx.Timeout(120, connect=20, write=20, pool=20)
            if timeout is None
            else httpx.Timeout(timeout)
        )
        if any(
            value is None or not math.isfinite(value) or value <= 0
            for value in (
                self.timeout.connect,
                self.timeout.read,
                self.timeout.write,
                self.timeout.pool,
            )
        ):
            raise ValueError("index_timeout_must_be_finite_positive")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("index_attempts_must_be_between_1_and_10")
        if any(
            not math.isfinite(value) or value < 0
            for value in (max_wait_seconds, total_wait_seconds)
        ):
            raise ValueError("index_waits_must_be_finite_nonnegative")
        self.max_attempts, self.max_wait_seconds, self.total_wait_seconds = (
            max_attempts,
            max_wait_seconds,
            total_wait_seconds,
        )
        self.sleep, self.clock = sleep, clock or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self.client = (
            client
            if client is not None
            else httpx.Client(transport=transport, timeout=self.timeout, follow_redirects=False)
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_client:
            self.client.close()
            self._owns_client = False

    def _retry_delay(self, error: httpx.HTTPError, attempt: int, waited: float) -> float:
        delay = min(2 ** (attempt - 1), self.max_wait_seconds)
        if isinstance(error, httpx.HTTPStatusError):
            header = error.response.headers.get("Retry-After")
            if header:
                try:
                    value = (
                        float(header)
                        if re.fullmatch(r"[0-9]+", header)
                        else (parsedate_to_datetime(header) - self.clock()).total_seconds()
                    )
                except (ValueError, TypeError, OverflowError):
                    value = 0
                if math.isfinite(value):
                    delay = max(delay, value)
        if delay > self.total_wait_seconds - waited:
            raise PackageDiscoveryError("index_retry_wait_budget_exceeded")
        return delay

    def fetch_snapshot(
        self,
        destination_dir: Path,
        *,
        max_decoded_bytes: int = DEFAULT_INDEX_BYTES,
        progress: ProgressCallback | None = None,
    ) -> IndexSnapshot:
        validate_byte_budget(max_decoded_bytes)
        destination_dir.mkdir(parents=True, exist_ok=True)
        waited = 0.0
        for attempt in range(1, self.max_attempts + 1):
            temporary, size, status, started = None, 0, None, time.monotonic()
            report_progress(
                progress,
                "downloading",
                completed=0,
                message=f"Downloading PyPI Index (attempt {attempt}/{self.max_attempts})",
            )
            try:
                request = self.client.build_request("GET", INDEX_URL, timeout=self.timeout)
                # Borrowed clients may carry query credentials as default params.
                request.url = httpx.URL(INDEX_URL)
                request.headers = httpx.Headers(
                    {
                        "Host": "pypi.org",
                        "Accept": INDEX_ACCEPT,
                        "Accept-Encoding": "gzip, deflate",
                        "User-Agent": "RepoSeer/0.1.0",
                    }
                )
                # Disable borrowed session auth and cookies as well as automatic redirects.
                with closing(
                    self.client.send(request, stream=True, auth=None, follow_redirects=False)
                ) as response:
                    status = response.status_code
                    if response.is_redirect:
                        raise PackageDiscoveryError("index_redirect_rejected")
                    response.raise_for_status()
                    if status != 200:
                        raise PackageDiscoveryError("index_unexpected_status")
                    content_type = response.headers.get("Content-Type", "")
                    if content_type.split(";")[0].strip().lower() not in CONTENT_TYPES:
                        raise PackageDiscoveryError("index_unsupported_content_type")
                    serial = response.headers.get("X-PyPI-Last-Serial")
                    if serial is not None and not re.fullmatch(r"[0-9]+", serial):
                        raise PackageDiscoveryError("index_invalid_serial")
                    try:
                        last_serial = int(serial) if serial is not None else None
                    except ValueError as error:
                        raise PackageDiscoveryError("index_invalid_serial") from error
                    parser, decoder, digest = (
                        IndexHTMLParser(),
                        codecs.getincrementaldecoder("utf-8-sig")(),
                        hashlib.sha256(),
                    )
                    with tempfile.NamedTemporaryFile(
                        dir=destination_dir, suffix=".tmp", delete=False
                    ) as output:
                        temporary = Path(output.name)
                        for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                            size += len(chunk)
                            if size > max_decoded_bytes:
                                raise PackageDiscoveryError("index_byte_budget_exceeded")
                            digest.update(chunk)
                            output.write(chunk)
                            parser.feed(decoder.decode(chunk))
                            report_progress(progress, "downloading", completed=size)
                        parser.feed(decoder.decode(b"", final=True))
                        parser.finish()
                        output.flush()
                        os.fsync(output.fileno())
                    snapshot = IndexSnapshot(
                        temporary,
                        digest.hexdigest(),
                        size,
                        INDEX_URL,
                        format_utc_timestamp(self.clock()),
                        content_type,
                        last_serial,
                        response.headers.get("ETag"),
                        response.headers.get("Last-Modified"),
                    )
                return _publish_snapshot(temporary, snapshot, destination_dir)
            except httpx.HTTPError as error:
                if attempt == self.max_attempts or not is_retryable_exception(error):
                    code = (
                        f"index_http_status:{status}"
                        if isinstance(error, httpx.HTTPStatusError)
                        else "index_stream_failed"
                    )
                    raise PackageDiscoveryError(code) from error
                delay = self._retry_delay(error, attempt, waited)
            except UnicodeError as error:
                raise PackageDiscoveryError("invalid_index_utf8") from error
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                logger.info(
                    "host=pypi.org attempt=%d status=%s decoded_bytes=%d duration=%.3f",
                    attempt,
                    status,
                    size,
                    time.monotonic() - started,
                )
            report_progress(
                progress,
                "downloading",
                completed=size,
                message=f"Retrying in {delay:g}s (attempt {attempt}/{self.max_attempts})",
            )
            self.sleep(delay)
            waited += delay
        raise PackageDiscoveryError("index_attempts_exhausted")
