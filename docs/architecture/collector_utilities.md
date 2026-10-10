# Common collector utilities

All network collection uses `reposeer.collection.HTTPClient`. GitHub, PyPI,
deps.dev, OSV, Hacker News, and the external HTML fetcher use this layer rather
than implementing their own timeout, retry, or rate-limit loops. The client is
synchronous and owns a pooled HTTPX session.

## HTTP policy

- A configurable timeout applies to HTTPX connect, read, write, and pool operations.
  It is not a deadline for an entire collection run or all retries combined.
- `max_retries` is the existing configuration name for **total attempts**,
  including the initial request. The default is 3; set it to 1 for no retry.
- Retry transient network failures and HTTP 429, 500, 502, 503, and 504 with
  exponential backoff. Permanent errors such as 400, 401, 404, and ordinary 403
  fail immediately.
- Honor `Retry-After` as seconds or an HTTP date. GitHub 403 quota exhaustion and
  secondary rate-limit responses also retry. When a rate-limit response omits
  usable headers, use an increasing configured fallback delay. This follows
  [GitHub's rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit).
- Wait for the larger of backoff and the server's cooldown; a server-directed
  delay is not capped by `max_backoff_seconds`. Cooldowns are checked before
  every attempt and tracked separately per origin within a client.
- Exhausted requests raise `HTTPClientError`; rate-limit exhaustion raises its
  subclass `RateLimitExceededError`. Both retain the status and response body,
  and the original exception remains available as the cause.

## Configuration

`configs/default.yaml` controls the shared defaults:

```yaml
collection:
  timeout_seconds: 30
  max_retries: 3
  backoff_factor: 2.0
  max_backoff_seconds: 10.0
  rate_limit_pause_seconds: 60
  rate_limit_buffer: 5
  cache_enabled: false
  cache_ttl_seconds: 300
  # Optional: otherwise use storage.metadata_dir / "http_cache".
  # cache_dir: "data/metadata/http_cache"
```

Explicit constructor options override the HTTP defaults. Invalid zero/negative
timeouts and attempt counts fail validation rather than falling back silently.
Zero backoff is supported for deterministic tests.

## Cache

`ResponseCache` is an optional, disk-backed TTL cache for successful GETs. It
stores decoded response bytes and headers through atomic JSON replacement. A
fresh hit avoids both network calls and rate-limit sleeps. Expired or damaged
entries are misses; storage failures log a warning and collection continues.

Keys hash the full request URL, query, and effective headers, so different
credentials, cookies, or Accept representations cannot share a response. Request
headers and URLs are not stored in the entry. Response bodies may contain private
data, so choose a suitable local cache directory.

Errors, non-GET requests, `no-store`/`no-cache`, `Set-Cookie`, and `Vary: *`
responses are not cached. This is a basic TTL cache: it does not implement HTTP
revalidation, eviction, or every Cache-Control directive. Cache is disabled by
default. `get(..., use_cache=False)` bypasses both reads and writes for one call.

## Shared sessions, logging, and checkpoints

GitHub and Hacker News accept `http_client=`, and `HTMLFetcher` accepts the same
client. Providers use absolute URLs and apply their own request headers, allowing
one client to serve all three. An injected session belongs to the caller;
provider/fetcher context managers close only sessions they create themselves.
Use `with HTTPClient(...)` to close the shared session once the run finishes.

HTTP attempts, retries, cache issues, and collection errors use Python's standard
logging under `reposeer.http.*` and `reposeer.collection`. HTTP diagnostics omit
query parameters and authentication headers. `handle_collection_error` adds
`run_id` and `entity_id` fields and counts failures through `CollectionProgress`.
Applications can use `configs/logging.yaml` with `logging.config.dictConfig`;
the library does not change global logging configuration.

`RunManager` creates and atomically saves `CollectionContext` checkpoints beneath
`storage.metadata_dir / "collection_runs"` (or an explicit directory). Resume
preserves the start time, counts, and string/integer checkpoint values. Malformed
checkpoints raise `CollectionError`; unsafe run IDs and overwriting existing runs
are rejected. A failed write retains the last valid checkpoint.

Pass one `CollectionProgress(context, manager)` to `RepositoryCollector`,
`HackerNewsClient`, and `HTMLFetcher` to persist their counts and last completed
repository/query/URL. Failures increase the error count without advancing a
checkpoint. These are **fetch-progress checkpoints**: they do not persist output
records or automatically skip entities on resume. Persist outputs separately
before using progress to decide what work can be skipped.

```python
from pathlib import Path

from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.clients.github import GitHubClient
from reposeer.collection import CollectionProgress, HTTPClient, ResponseCache, RunManager
from reposeer.collectors.external.crawler.fetcher import HTMLFetcher
from reposeer.collectors.github.repository import RepositoryCollector

manager = RunManager()
context = manager.start_run()  # Later: manager.resume_run(context.run_id)
progress = CollectionProgress(context, manager)
cache = ResponseCache(Path("data/metadata/http_cache"), ttl_seconds=300)

with HTTPClient(cache=cache) as http:
    with GitHubClient(http_client=http) as github:
        repository = RepositoryCollector(github, progress=progress).collect("pallets", "flask")
    with HackerNewsClient(http_client=http, progress=progress) as discovery:
        candidates = discovery.search(repository.name, limit=5)
    with HTMLFetcher(http, progress=progress) as fetcher:
        for candidate in candidates:
            html = fetcher.fetch(candidate.url)
            # Store/extract HTML and persist downstream outputs here.
```

## Verification

Run the unit tests and offline collector smoke test without API credentials:

```bash
uv run --extra dev pytest tests/unit/collection tests/integration/test_github_pipeline.py tests/integration/test_external_pipeline.py tests/integration/test_collector_utilities.py
uv run --extra dev pytest -m "not smoke"
uv run --extra dev ruff check .
```

The tests use HTTPX's mock transport and a fake clock. They cover timeout
propagation, retry limits/backoff, permanent errors, both Retry-After formats,
GitHub 403 limits, origin isolation, cache expiry/credential isolation/corruption,
compressed responses, checkpoint recovery, interrupted writes, and reuse across
the GitHub collector, Hacker News provider, and HTML crawler. Existing `smoke`
tests remain available for optional live GitHub verification.
