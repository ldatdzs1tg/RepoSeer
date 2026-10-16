# External crawler and content extractor (T-017)

T-017 consumes T-016 candidates and captures current external content. It uses the
T-012 synchronous HTTP client, retries, timeouts and per-origin rate limits. It
does not run discovery, assign relevance, normalize to processed Articles, build
features or infer maintenance status. Those stages remain separate.

RepoSeer's analysis subject is a PyPI package with a verified GitHub repository
mapping. Collect repository metadata through `RepositoryService` / `collect`
(T-013) and repository activity through T-014. T-017 complements those core
observations with candidate content; a general collection of Python discussions
does not demonstrate that the package/repository workflow works.

For live validation, start with package/repository seeds, verify PyPI Source links
against numeric GitHub identities, and retain the upstream mapping observations.
First-party Source/Documentation/Changes/Changelog links in PyPI metadata provide
auditable crawl targets. Preserve the metadata field and originating raw ID in
each derived candidate. Keep these first-party candidates distinct from T-016
search results. Review search candidates separately: their query scope and
`repo_id` do not establish that their contents concern the target package.

## Entry points

```bash
uv run reposeer crawl-external --from-stored
uv run reposeer crawl-external --candidates-file data/raw/external/discovery.jsonl
uv run reposeer crawl-external 'https://example.org/article' --data-dir /tmp/reposeer-crawl
uv run reposeer crawl-external --from-stored --max-candidates 20
```

Choose exactly one URL, `--candidates-file`, or `--from-stored`. Input files must
contain canonical `RawRecord` lines with `source=web_article`,
`source_record_type=article` and `payload.record_stage=discovery_candidate`.
The Python pipeline also accepts `ExternalCandidate` instances and URL strings:

```python
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.storage.raw.jsonl import JsonlStorage

candidates = JsonlStorage().read_contract_records(candidate_path)
with ExternalCrawlPipeline() as crawler:
    result = crawler.crawl(candidates, max_candidates=100)
```

Input is validated before fetching. Batches contain 1–1,000 inputs; each call
attempts at most `max_candidates` (default 100). A partial batch returns an offset
`next_cursor`. Pass `--cursor` with the **same ordered, unchanged input file** to
continue in a new run. This is not automatic recovery or content deduplication.
Rerunning without a cursor appends new observations.

CLI exit codes: 0 for a complete batch, 1 for partial/failed collection or storage
failure, 2 for invalid input/options. A batch with parser failures remains partial
even when the useful raw responses were saved. Error messages exclude untrusted
parser details, credentials and query text.

## Retrieval and extraction

| Candidate | Retrieval |
| --- | --- |
| Web article; linked HN blog; RSS/news URL | HTTPX GET, then Trafilatura main text and metadata |
| `news.ycombinator.com/item?id=N` | Official HN `/v0/item/N.json`; own item body only |
| DEV candidate with a numeric `devto:N` ID | Forem `/api/articles/N`; `body_html` |
| Stack Overflow / `*.stackexchange.com/questions/N` | Stack Exchange `/questions/N?site=...&filter=withbody`; question body |

Native IDs must match the candidate and returned item. DEV additionally checks the
returned article URL. Deleted/missing items, missing native bodies, invalid JSON
and parser errors cannot become successful empty documents. The crawler does not
recursively fetch comments, answers or linked pages. Native payloads preserve
available tags, author, score, edit and comments metadata without treating absent
fields as zero. Unsupported native URL shapes use the bounded HTML path.

Website extraction requires `text/html` or `application/xhtml+xml`; native APIs
require JSON. Empty responses produce errors. A nonempty response remains raw
audit evidence if content extraction fails, with null content/hash/word count and
`content_status=unavailable`. HTTP/network failures have structured errors and no
fabricated raw records. An ordinary 404 does not retry; transient failures use the
shared bounded retry policy.

Publication timestamps with an explicit timezone become UTC milliseconds. Date-only
metadata stays `published_date` with `published_at=null` and `published_precision=date`.
Timezone-free timestamps retain only the explicitly supplied calendar date.
Missing, invalid or future dates remain unknown. Publication dates
are not clamped to the fetch time. Content uses NFC, LF and outer whitespace
trimming, matching the canonical Article hashing rules.

Since crawler `0.2.1`, publication requires an explicit publication field,
article JSON-LD or visible byline. Generic date guesses from Trafilatura are not
used: a documentation footer's copyright year is not a publication date.

Since `0.2.2`, GitHub rendered Markdown extraction removes heading permalink
controls before text extraction. This preserves README section headings and
changelog version/date headings that Trafilatura otherwise discards. The original
response bytes and HTML remain intact. Dates within release sections are not
assigned as the publication date of the whole document.

## Bounds and sessions

- Default 5 redirects (`--max-redirects`, range 0–20); loops and missing locations fail.
- Every initial URL and redirect destination passes HTTP(S), credentials, port,
  hostname and DNS checks. Local/private/link-local/multicast addresses are blocked,
  including mixed public/private DNS answers. Ports are limited to 80/443.
- Default 5,000,000 decoded response bytes (`--max-response-bytes`). Streaming stops
  when the decoded byte budget is exceeded and closes the response.
- Requests remove inherited session credentials, custom authentication headers and
  cookies. Shared sessions remain caller-owned.
- Crawler requests bypass the TTL cache, keeping `fetched_at` tied to an actual fetch.
  Timeout/retry settings remain those in `configs/default.yaml` / `HTTPClient`.

DNS checks are preflight checks rather than a transport/network sandbox. Use trusted
DNS and deployment egress controls to prevent DNS rebinding. Response byte limits
do not constitute a total process-memory or extraction CPU deadline. The legacy
`HTMLFetcher.fetch()` string helper retains T-012 behavior; the T-017 pipeline uses
the bounded `fetch_response()` path with an explicit `URLPolicy`.

## Output contract and T-018 handoff

Canonical raw envelopes are strict-validated and durably appended to
`data/raw/external/documents.jsonl`; discovery files are never modified. Each
observation has a new raw UUID and run UUID. No canonical contract fields or schema
version were changed.

| Location | Meaning |
| --- | --- |
| `source=web_article`, `source_record_type=article` | Existing canonical exchange vocabulary |
| `source_url` | Original candidate resource; matches the collection subject |
| `retrieved_at` | Actual observation time, identical to payload `fetched_at` |
| `event_at` | Extracted publication timestamp, or null when only a date/unknown is known |
| `available_at` | Observation time: retained response bytes prove availability at fetch time |
| `http_status` / `raw_body_sha256` | Actual final response status / SHA-256 of decoded body bytes |
| `payload.record_stage=external_document` | Distinguishes captures from T-016 discovery candidates |
| `payload.source`, `source_item_id` | Discovery source/provider and native source item ID |
| `payload.source_url`, `url`, `fetched_url` | Original URL, document/final website URL, actual fetched endpoint (native API when applicable) |
| `payload.response_body_base64` | Exact HTTP-decoded bytes for independent hash checks/replay |
| `payload.html` / `native_payload` | Source HTML text / native JSON, without renaming provider fields |
| `payload.title`, `author`, `published_at`, `published_date`, `published_precision`, `updated_at` | Extracted metadata; unknown values stay null |
| `payload.content`, `content_status`, `content_sha256`, `word_count` | Extracted main text and its hash/length, separate from the original response |
| `payload.etag`, `last_modified` | Observed response validators; neither proves historical availability |
| `payload.discovery`, `candidate_raw_record_id` | Unchanged candidate envelope/data, including query IDs/hash, source/window, cutoff and subject-match context when provided |

The payload is a capture container for native evidence plus derived extraction;
it is **not** a canonical processed `Article`. T-018 can reconstruct source bytes,
repeat extraction, canonicalize URLs and build Articles with provenance pointing
to these raw IDs. Candidate repo/package IDs are carried as discovery scope,
not proof that the text is about those subjects. `relevance_status=unverified`.

Successful extraction sets `content_available_at=fetched_at`,
`availability_basis=observed_response` and `historical_version_status=observation_only`.
The saved response proves this revision existed at observation time; it does not
prove it existed unchanged on its publication date. Extraction failures retain
`content_available_at=null`. Original observations collected before this refinement
remain unchanged with their earlier null values.
The collector rejects historical `CollectionRequest.cutoff_at/time_range` before
fetching; historical cutoffs inside candidate lineage are preserved as context only.

For optional repository scope, `ExternalCrawlPipeline(repository_evidence=[...])`
accepts `RepositoryScopeEvidence` containing canonical PyPI and GitHub raw records.
The existing package resolver checks PyPI's official source URL against GitHub's
numeric repository identity. Only candidates carrying that package ID receive the
mapping; conflicting IDs fail. The capture retains the original discovery record
and `payload.repository_scope` cites both raw evidence IDs and observation times.
This establishes current discovery scope, not article relevance or historical mapping.

To enrich existing captures using actual upstream sources and retain original lines:

```bash
uv run python scripts/enrich_external_documents.py --live --data-dir data
```

This uses the existing GitHub repository collector, saves PyPI mapping evidence,
refetches each distinct resource and appends new raw observations. A validated
latest-per-URL view is written to `data/reports/t017_latest_documents.jsonl`, with
field changes recorded in `data/reports/t017_enrichment.json`. Explicit HTML
bylines and article JSON-LD are supported alongside source metadata.

A manifest is atomically written to
`data/metadata/collection_runs/crawl_manifest_<run_id>.json` **after** durable raw
persistence. It includes canonical result metadata/errors, raw IDs, extraction
count and cursor. Coverage describes this requested candidate batch only; it is
not a complete source/query/window census or an E1 feature-eligibility claim.
No checkpoint is advanced before persistence. A failed manifest write may leave
valid raw observations; retrying appends new observations for later deduplication.

## Offline verification

Extraction acceptance cases are in
`tests/integration/test_external_extraction_contract.py`. They include exact
golden text for ordinary articles and GitHub Markdown release headings, metadata,
retained response bytes and extraction failure handling. Live validation should
also inspect source text: a successful nonempty extraction and valid hashes alone
do not establish semantic completeness.

```bash
uv run python scripts/crawl_external_sample.py --data-dir /tmp/reposeer-t017-demo
uv run pytest -q tests/unit/collection/test_external_crawler.py tests/integration/test_external_crawl_pipeline.py tests/integration/test_external_extraction_contract.py
uv run python scripts/validate_external_documents.py data/raw/external/documents.jsonl data/reports/t017_latest_documents.jsonl
uv run python scripts/export_contract.py --check
uv run python scripts/validate_examples.py
uv run pytest -q -m 'not smoke'
```

The sample uses `examples/external_crawl/article.html`, MockTransport and a fake
public DNS resolver; it performs no network access and validates strict raw replay.
Its output is synthetic and must stay separate from real collected datasets.
Tests cover the real T-016 handoff, redirects, compressed bodies and hashes, native
API bodies, unavailable content, parser errors, mixed failures, byte bounds,
credential isolation, DNS checks, cursor handling and CLI behavior.

Provider interfaces follow the official [HTTPX documentation](https://www.python-httpx.org/quickstart/),
[Trafilatura API](https://trafilatura.readthedocs.io/en/latest/corefunctions.html),
[HN API](https://github.com/HackerNews/API), [Forem API](https://developers.forem.com/api/v1)
and [Stack Exchange question API](https://api.stackexchange.com/docs/questions-by-ids).
