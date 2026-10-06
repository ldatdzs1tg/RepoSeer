# Contract: Raw Record

Raw records capture upstream responses without modification:
- `source`: Upstream source identifier (`github`, `pypi`, `hackernews`)
- `entity_id`: Entity primary identifier
- `payload`: Original response JSON
- `collected_at`: UTC timestamp
- `run_id`: Execution batch ID
