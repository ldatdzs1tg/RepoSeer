# Pipeline Orchestration

## End-to-End Ingestion Flow

```text
Repo URL
  ↓
GitHub metadata collector (T-013)
  ↓
GitHub activity collector (T-014)
  ↓
Technology Resolver (T-015)
  ↓
External discovery (T-016)
  ↓
Article crawler (T-017)
  ↓
Normalize / deduplicate (T-018)
  ↓
Storage (T-019)
  ↓
Profiling report (T-020)
```
