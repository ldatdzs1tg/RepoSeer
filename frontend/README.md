# RepoSeer frontend (T-028)

This directory is a standalone, buildless HTML/CSS/JavaScript client. It has no
Python imports, model files, secrets, framework dependencies or shared filesystem
requirement with the backend. Replace or redesign this entire directory without
changing the application orchestrator. The public boundary is the
[HTTP API](../docs/architecture/demo_api.md) and T-021 FinalReport JSON.

From the repository root, start **two processes**:

```bash
# Terminal 1: backend, offline synthetic pipeline + six report-state fixtures
uv run --frozen reposeer demo-api \
  --input-bundle examples/application/synthetic_bundle.json \
  --model examples/application/synthetic_model_a.json \
  --agent-replay examples/application/synthetic_agent_replay.json \
  --mock-dir examples/analysis
```

```bash
# Terminal 2: frontend; Python is only a static file server here
python3 -m http.server 5173 --bind 127.0.0.1 --directory frontend
```

Open **http://localhost:5173** (use this exact origin with the default backend
configuration). Windows users can use `python` instead of `python3`; the backend
command may be written on one line in PowerShell.

Click **Load example** for CORE, ENHANCED, fallback, known cessation, scheduled
cessation and no-score. Click **Analyze repository** with `synthetic-demo` to run
the actual T-027 orchestrator using the synthetic bundle. Request ENHANCED to see
the explicit CORE fallback; enable insights and specialist agents to exercise
T-025/T-026 replay. These fixtures demonstrate integration, not model quality.

For real prepared inputs, replace `--input-bundle` and `--model`. For current
network collection, remove `--input-bundle` and add `--live`. A GitHub URL needs
an independently verified package mapping; use the optional PyPI mapping field
to select its package. Missing historical proof can correctly return no-score.
Live LLM requests additionally require server-side `--agent-live`, the functional
extra and provider configuration described in the orchestrator guide. A browser
checkbox alone never grants network access or configures a provider.

Change the API address in [config.js](config.js). If you change the frontend
origin, start the backend with the corresponding `--origin http://localhost:PORT`.
CORS allows one exact origin. Do not add a trailing slash to `--origin`.
The demo API is a local development server, not an authenticated public service.

Files to edit:

- `index.html`: layout, form controls and accessible tab structure.
- `styles.css`: typography, colors, responsive layout and components.
- `app.js`: API adapter and FinalReport rendering; untrusted text uses DOM text
  nodes, and citation links allow HTTP(S) only.
- `config.js`: backend URL.

The UI displays queued/running/complete/failed job states without invented stage
percentages. Predictions appear only for `result_status=predicted`. Official
status and no-score never receive a numeric risk. Unknown coverage counts remain
unknown; dependency impact has its own panel. Export JSON downloads the original
FinalReport, not a transformed UI view. API jobs are ephemeral and retained only
for the latest 32 results; orchestrator reports persist in the configured data
directory. Reloading the page starts a fresh UI session.
