# Contract: Raw Record

Use `reposeer.contracts.RawRecord` (contract `0.1.0`), not the legacy DTO in `schemas/raw.py`. Every declared key is required, including nullable values.

- Version/type: `schema_version`, `record_type="raw_record"`.
- Observation/run: `raw_record_id="raw:<lowercase UUIDv4>"`, `run_id`.
- Provider: `source`, `source_record_type`, nullable `source_record_id`, `source_url`.
- Verified subjects: nullable canonical `repo_id`, `package_id`; never infer package mapping.
- UTC millisecond time: `retrieved_at`, nullable `event_at`, `available_at`; neither may exceed retrieval.
- Nullable response metadata: `http_status`, `raw_body_sha256`.
- Native JSON object: `payload`, preserving provider keys and values for replay.

Raw JSONL is immutable/append-only through `JsonlStorage.append_contract_record`; `read_contract_records` strictly validates each object/line. Generic legacy storage APIs do not establish contract safety. No headers, authentication token or URL credentials belong in the envelope.

`raw_body_sha256` hashes actual HTTP body bytes after HTTP content decoding and before JSON parsing, when those bytes are known. A reserialized JSON hash is not that hash. The T-012 GitHub client returns parsed JSON, so the T-011 adapter emits null hash/status, null repository event/unknown historical availability, and preserves native `full_name`, `archived`, `open_issues_count`.

See [full contract](data_contract.md#6-raw-record-envelope) and [synthetic example](../../examples/contracts/github_raw_record.json).
