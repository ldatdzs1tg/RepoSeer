# Sprint 3: Multi-Agent Handoff Interface

## Overview
According to the Sprint 2 architectural boundaries, the core data collection, normalization, storage, and profiling layers are fully decoupled from any downstream agent framework.

Sprint 2 produces structured evidence in Parquet and DuckDB tables:
- `data/processed/repositories/`
- `data/processed/github_activity/`
- `data/processed/technologies/`
- `data/processed/external_documents/`
- `data/features/repository_package_snapshots/`

In Sprint 3, specialized AI agents (e.g. LangGraph, CrewAI, PydanticAI) can query this structured evidence to perform qualitative and predictive reasoning:
- **Agent 1 (Repository Health)**: Analyzes commit cadence, issue response velocity, and maintainer continuity.
- **Agent 2 (Technology Trend)**: Evaluates ecosystem vitality, deprecation risks, and technology migrations.
- **Agent 3 (Community / News Sentiment)**: Analyzes community sentiment and external article mentions.
- **Synthesizer Agent**: Merges agent signals into repository risk and viability scores.
