import { API_BASE_URL } from './config.js';

const $ = (id) => document.getElementById(id);
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
};
const human = (value) => value ? String(value).replaceAll('_', ' ') : 'Not provided';
const date = (value) => value ? new Date(value).toISOString().replace('T', ' ').replace('.000Z', 'Z').replace('Z', ' UTC') : 'Unknown';
const count = (value) => value == null ? 'Unknown' : new Intl.NumberFormat('en').format(value);
const pill = (text, kind = '') => node('span', text, `pill ${kind}`);
const row = (label, value) => {
  const element = node('div', null, 'kv');
  element.append(node('span', label), node('strong', value));
  return element;
};
const link = (url, text) => {
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error();
    const anchor = node('a', text);
    anchor.href = parsed.href;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    return anchor;
  } catch { return node('span', text); }
};
let report;
let reportSource;
let selectedTab = 'evidence';
let busy = false;
let pollController;

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, { ...options, signal: options.signal || AbortSignal.timeout(15000) });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `http_${response.status}`);
  return payload;
}

function progress(message, state = '') {
  $('progress').className = `progress ${state}`;
  $('progress').textContent = message;
}

function lock(value) {
  busy = value;
  $('analyze').disabled = value;
  $('load-demo').disabled = value || !$('scenario').options.length || $('scenario').disabled;
  $('analyze').textContent = value ? 'Analysis in progress…' : 'Analyze repository';
}

async function submit(body) {
  if (busy) return;
  lock(true);
  // Clear the previous report so it cannot be mistaken for the current request.
  $('report').hidden = true;
  $('empty').hidden = true;
  progress('Submitting analysis to the API…', 'busy');
  pollController = new AbortController();
  try {
    let job = await api('/api/analyses', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    while (['queued', 'running'].includes(job.status)) {
      progress(job.status === 'queued' ? 'Queued · Waiting for the analysis worker.' : 'Running · The orchestrator is resolving and evaluating the available evidence.', 'busy');
      await new Promise((resolve) => setTimeout(resolve, 600));
      job = await api(`/api/analyses/${encodeURIComponent(job.id)}`, { signal: pollController.signal });
    }
    if (job.status !== 'complete' || !job.report) throw new Error(job.error || 'invalid_api_response');
    report = job.report;
    reportSource = job.source;
    render();
    progress(`Complete · ${reportSource === 'mock' ? 'Synthetic example loaded' : 'Orchestrator returned a validated FinalReport'}.`);
  } catch (error) {
    $('empty').hidden = false;
    const messages = {
      invalid_submission: 'Check the package, cutoff and analysis options. The API rejected this request.',
      analysis_failed: 'Analysis failed. Check the API configuration and input bundle, then try again.',
      analysis_queue_full: 'The analysis queue is full. Try again after a running analysis finishes.',
      github_mapping_unresolved: 'No verified GitHub-to-package mapping was found. Select a PyPI package and check the backend input data.',
      github_package_selection_required: 'This repository maps to multiple packages. Fill in the optional PyPI mapping field.',
      cutoff_cannot_be_in_the_future: 'The cutoff must be at or before the current UTC time.',
    };
    progress(messages[error.message] || 'Cannot reach the analysis API. Check that the backend is running and its allowed origin matches this page.', 'error');
  } finally { lock(false); }
}

$('analysis-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const request = {
    identifier: $('identifier').value.trim(),
    mode_requested: $('mode').value,
    purpose: $('purpose').value,
    include_external_insights: $('insights').checked,
    include_agents: $('agents').checked,
    include_dependency_impact: $('impact').checked,
  };
  if ($('package').value.trim()) request.package_name = $('package').value.trim();
  if ($('cutoff').value) request.cutoff_at = new Date(`${$('cutoff').value}Z`).toISOString();
  submit({ source: 'orchestrator', request });
});
$('load-demo').addEventListener('click', () => submit({ source: 'mock', scenario: $('scenario').value }));
window.addEventListener('pagehide', () => pollController?.abort());

function render() {
  $('report').hidden = false;
  $('report-package').textContent = report.package_id;
  $('report-context').textContent = `${reportSource === 'mock' ? 'SYNTHETIC EXAMPLE' : 'ORCHESTRATOR OUTPUT'} / ${human(report.purpose).toUpperCase()}`;
  $('report-subtitle').textContent = `Cutoff ${date(report.cutoff_at)} · Generated ${date(report.generated_at)}`;
  $('synthetic-notice').hidden = !(reportSource === 'mock' || report.package_id === 'pypi:synthetic-demo');
  $('evidence-count').textContent = report.evidence.length;
  renderOutcome(); renderMetadata(); renderImpact(); renderDetails();
}

function renderOutcome() {
  const card = $('outcome');
  card.replaceChildren(node('span', report.result_status === 'predicted' ? '12-MONTH MAINTENANCE RISK' : 'REPOSITORY STATUS', 'card-label'));
  // Never infer a probability from official status or replace missing values with zero.
  if (report.result_status === 'predicted') {
    const number = node('div', (report.prediction.probability * 100).toFixed(1), 'result-number');
    number.append(node('span', '%'));
    card.append(number, pill(`${human(report.prediction.risk_level)} risk`, 'warning'), node('p', `Estimated cessation probability by ${date(report.horizon_end_at)}.`));
  } else if (['already_abandoned', 'scheduled_cessation'].includes(report.result_status)) {
    card.append(node('h3', report.result_status === 'already_abandoned' ? 'Known cessation' : 'Scheduled cessation'), pill(human(report.official_status.event_type), 'neutral'));
    card.append(node('p', `Effective ${date(report.official_status.effective_at)}`));
    if (report.result_status === 'scheduled_cessation') card.append(node('p', report.within_horizon ? 'Within the 12-month horizon.' : 'Outside the 12-month horizon.'));
    card.append(node('p', 'Official status takes precedence. A prediction is not reported.'));
  } else {
    card.append(node('h3', 'No score available'), pill(human(report.result_status), 'neutral'), node('p', report.reason), node('p', report.reason_code, 'long-value'));
  }
}

function renderMetadata() {
  const card = $('metadata');
  card.replaceChildren(node('span', 'ANALYSIS CONTEXT', 'card-label'));
  card.append(row('Requested mode', report.mode_requested), row('Used mode', report.mode_used || 'No model run'), row('Enhanced status', human(report.enhanced_status)));
  if (report.fallback_reason) card.append(pill('CORE fallback', 'warning'), node('p', report.fallback_reason));
  card.append(node('div', null, 'card-divider'), row('Applicability', human(report.applicability.status)), row('External insights', human(report.external_insights_status)), row('Mapping scope', human(report.mapping?.scope_quality)));
  if (report.external_insights_reason) card.append(node('p', report.external_insights_reason));
}

function renderImpact() {
  const card = $('impact-card');
  const impact = report.dependency_impact;
  card.replaceChildren(node('span', 'DEPENDENCY IMPACT', 'card-label'));
  if (!impact || impact.impact_status === 'unavailable') {
    card.append(pill(impact ? 'Unavailable' : 'Not requested', 'neutral'), node('p', impact?.missing_reason || 'Enable dependency impact to request a separate ecosystem assessment.'));
  } else {
    const numbers = node('div', null, 'impact-numbers');
    for (const [label, value] of [['Direct dependents', impact.direct_dependents], ['Transitive incl. direct', impact.transitive_dependents]]) {
      const item = node('div'); item.append(node('strong', `${impact.counts_are_lower_bounds ? '≥ ' : ''}${count(value)}`), node('span', label)); numbers.append(item);
    }
    card.append(numbers, node('p', `${human(impact.graph_kind)} · ${count(impact.corpus_package_count)} packages in ${impact.corpus_id}`), node('p', `Graph as of ${date(impact.graph_as_of)}`));
    card.append(node('p', `Version: ${impact.target_version || 'unspecified'} · Environment: ${impact.environment || 'unspecified'} · Resolver: ${impact.resolver_version || 'unspecified'}`));
    if (impact.missing_reason) card.append(node('p', impact.missing_reason));
    card.append(node('p', `Sources: ${impact.source_ids.join(', ')}`));
  }
  card.append(node('div', null, 'card-divider'), node('p', 'Impact is independent of cessation probability. Counts describe this corpus, not the entire ecosystem.'));
}

const tabs = [...document.querySelectorAll('[role=tab]')];
function selectTab(tab, focus = false) {
  selectedTab = tab.dataset.tab;
  for (const item of tabs) { const selected = item === tab; item.setAttribute('aria-selected', selected); item.tabIndex = selected ? 0 : -1; }
  $('details').setAttribute('aria-labelledby', tab.id);
  if (focus) tab.focus();
  if (report) renderDetails();
}
for (const tab of tabs) {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', (event) => {
    let index = tabs.indexOf(tab);
    if (event.key === 'ArrowRight') index = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') index = (index + tabs.length - 1) % tabs.length;
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = tabs.length - 1;
    else return;
    event.preventDefault(); selectTab(tabs[index], true);
  });
}

function renderDetails() {
  const detail = $('details');
  detail.replaceChildren();
  ({ evidence: renderEvidence, coverage: renderCoverage, sources: renderSources, model: renderModel })[selectedTab](detail);
}

function heading(parent, title, subtitle) {
  const head = node('div', null, 'detail-heading'); const text = node('div'); text.append(node('h2', title), node('p', subtitle)); head.append(text); parent.append(head);
}

function renderEvidence(parent) {
  heading(parent, 'The evidence behind the report', 'Citations retain their source, quote offsets and role in the analysis.');
  if (!report.evidence.length) parent.append(node('p', 'No admissible evidence was returned for this report.', 'empty-message'));
  for (const evidence of report.evidence) {
    const item = node('article', null, 'evidence-item'); const tags = node('div', null, 'evidence-tags');
    tags.append(pill(human(evidence.origin)), pill(human(evidence.signal_type), 'neutral'), pill(human(evidence.use), evidence.use === 'report_only' ? 'warning' : 'good'), pill(human(evidence.context), 'neutral'));
    item.append(tags, node('div', evidence.claim, 'claim'));
    for (const citation of evidence.citations) {
      const source = report.sources.find((s) => s.source_id === citation.source_id);
      const block = node('div', null, 'citation');
      block.append(node('blockquote', citation.quote), source ? link(source.source_url, `${source.source} · ${citation.source_id}`) : node('span', citation.source_id), node('div', `Offsets ${citation.start_offset}–${citation.end_offset} · Event ${date(evidence.event_at)}`));
      item.append(block);
    }
    if (evidence.exclusion_reason) item.append(node('p', `Report only: ${evidence.exclusion_reason}`, 'detail-note'));
    if (evidence.uncertainty) item.append(node('p', `Uncertainty: ${evidence.uncertainty}`, 'detail-note'));
    if (evidence.agent) item.append(node('p', `${evidence.agent.agent_id} · ${evidence.agent.model_version} · prompt ${evidence.agent.prompt_version}`, 'long-value'));
    parent.append(item);
  }
  if (report.official_status) {
    const official = report.official_status;
    parent.append(node('h3', 'Official status verification'), row('Announced', date(official.announced_at)), row('Verified', date(official.verified_at)), row('Verifier', official.verifier), row('Ledger revision', official.ledger_revision), row('Evidence IDs', official.evidence_ids.join(', ')));
  }
}

function renderCoverage(parent) {
  heading(parent, 'Coverage & missingness', 'Unknown counts remain unknown. Completeness and historical validity are separate checks.');
  const wrap = node('div', null, 'table-scroll'); const table = node('table'); const head = node('thead'); const tr = node('tr');
  for (const text of ['Stage / source', 'Status', 'Source census', 'History', 'Observed', 'Processed / eligible']) tr.append(node('th', text));
  head.append(tr); table.append(head); const body = node('tbody');
  for (const coverage of report.coverage) {
    const row = node('tr'); const first = node('td', coverage.coverage_id); first.append(node('small', `${coverage.stage} · ${coverage.source_ids.join(', ') || 'No sources'}`));
    if (coverage.window) first.append(node('small', `${date(coverage.window.start_at)} – ${date(coverage.window.end_at)}`));
    if (coverage.missing_reason) first.append(node('small', coverage.missing_reason));
    row.append(first, node('td', human(coverage.status)), node('td', human(coverage.source_coverage)), node('td', human(coverage.historical_validity)), node('td', count(coverage.records_observed)), node('td', `${count(coverage.processed_documents)} / ${count(coverage.eligible_documents)}`)); body.append(row);
  }
  table.append(body); wrap.append(table); parent.append(wrap);
  if (!report.coverage.length) parent.append(node('p', 'No coverage records returned.', 'empty-message'));
}

function renderSources(parent) {
  heading(parent, 'Source provenance & cutoff', `Analysis cutoff: ${date(report.cutoff_at)}. Retrieval time alone does not establish historical availability.`);
  if (!report.sources.length) parent.append(node('p', 'No source records returned.', 'empty-message'));
  for (const source of report.sources) {
    const item = node('article', null, 'source-item');
    const title = link(source.source_url, `${source.source_id} · ${source.source}`); title.className = 'source-title';
    item.append(title, row('Source URL', source.source_url), row('Available at', date(source.available_at)), row('Retrieved at', date(source.retrieved_at)), row('Valid at', date(source.valid_at)), row('Temporal basis', human(source.temporal_basis)), row('Revision', source.revision || 'Unknown'), row('Raw records', source.raw_record_ids.join(', ') || 'None recorded'), row('Missing fields', source.missing_fields.join(', ') || 'None recorded'));
    item.append(node('p', `SHA-256: ${source.content_sha256 || 'Unknown'}`, 'long-value'));
    parent.append(item);
  }
}

function renderModel(parent) {
  heading(parent, 'Model context & limitations', 'Use the report within its declared population, time boundary and evidence coverage.');
  if (report.result_status === 'predicted') {
    for (const [label, prediction] of [['Selected prediction', report.prediction], ['CORE baseline', report.core_prediction]]) {
      if (!prediction) continue;
      const model = prediction.model;
      parent.append(node('h3', label), row('Model', `${model.model_id} / ${model.model_version}`), row('Feature set', model.feature_set), row('Calibration', model.calibration_version), row('Risk thresholds', model.risk_threshold_version), row('Training population', model.training_population), row('Calibration population', model.calibration_population), row('Frozen at', date(model.frozen_at)), row('Probability', `${(prediction.probability * 100).toFixed(1)}%`));
    }
  } else parent.append(node('p', 'This report does not contain a model prediction.'));
  parent.append(row('Applicability policy', report.applicability.policy_version), row('Analysis run', report.analysis_run_id));
  if (report.backtest) parent.append(node('p', JSON.stringify(report.backtest), 'long-value'));
  const limitations = [...report.applicability.reasons, ...report.applicability.limitations, ...report.limitations];
  const list = node('ul', null, 'limitations');
  for (const limitation of new Set(limitations)) list.append(node('li', limitation));
  parent.append(list);
}

$('download').addEventListener('click', () => {
  if (!report) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2) + '\n'], { type: 'application/json' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `reposeer-${report.analysis_run_id}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});

async function connect() {
  try {
    await api('/api/health');
    $('connection').textContent = 'API connected'; $('connection').classList.add('online');
    const { scenarios } = await api('/api/scenarios');
    $('scenario').replaceChildren(...scenarios.map((scenario) => { const option = node('option', scenario.label); option.value = scenario.id; return option; }));
    $('scenario').disabled = !scenarios.length;
    $('load-demo').disabled = !scenarios.length;
    if (!scenarios.length) { $('scenario').append(node('option', 'Examples not enabled')); }
  } catch {
    $('connection').textContent = 'API offline';
    progress('Start the backend API to analyze a package or load example reports.', 'error');
  }
}
connect();
