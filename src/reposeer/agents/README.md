# Specialist agent boundary (T-024)

RepoSeer v3 defines exactly three specialist roles:

- **Maintenance & Lifecycle** (`maintenance_lifecycle`): maintenance context,
  missing notices/history and provisional lifecycle claims.
- **Functional Trend & Demand** (`functional_trend_demand`): package purpose,
  use cases and technical needs, without inferring package user counts.
- **Alternatives & Migration** (`alternatives_migration`): source-backed
  alternatives, compatibility gaps and explicit migration evidence.

The same specialists can receive incomplete research candidates or strict
D06-eligible evidence: **soft intake, evidence enrichment, hard promotion**.
Research produces leads, information gaps and suggestions; it does not bypass
D06/D08, assign labels/official status or change Model A/B probabilities.

Contracts and pure validators live in `reposeer.contracts.specialist_agents`.
T-024 supplies no LLM client, crawler or multi-agent runtime. Execution,
bounded feedback and conflict-review enforcement belong to T-026.

See the [specialist contracts](../../../docs/contracts/specialist_agents.md)
for both lanes, schemas, examples and promotion/verification requirements.
