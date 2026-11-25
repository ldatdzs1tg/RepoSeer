import { API_BASE_URL } from './config.js';

const $ = (id) => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';

const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
};

const svgEl = (tag, attrs = {}, text) => {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
};

const human = (value) => (value ? String(value).replaceAll('_', ' ') : 'Not provided');
const date = (value) => (value ? new Date(value).toISOString().replace('T', ' ').replace('.000Z', 'Z').replace('Z', ' UTC') : 'Unknown');
const shortDate = (value) => (value ? new Date(value).toISOString().slice(0, 10) : 'Unknown');
const count = (value) => (value == null ? 'Unknown' : new Intl.NumberFormat('en').format(value));
const pill = (text, kind = '') => node('span', text, `pill ${kind}`.trim());
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
  } catch {
    return node('span', text);
  }
};

let report;
let reportSource;
let reportScenarioLabel = '';
let selectedTab = 'evidence';
let evidenceFilter = 'all';
let evidenceQuery = '';
let busy = false;
let pollController;
let timerInterval;
let startTimeMs = 0;
const sessionHistory = [];
const scenarioLabels = new Map();

// ============================================================================
// Ambient Floating Particles (Luminous sparks in dark, soft blue bokeh in light)
// ============================================================================
function initParticles() {
  const container = $('ambient-particles');
  if (!container) return;
  const countDots = 28;
  for (let i = 0; i < countDots; i++) {
    const dot = node('span', null, i % 3 === 0 ? 'ambient-dot alt' : 'ambient-dot');
    const x = ((i * 37.3 + 11) % 96) + 2;
    const y = ((i * 53.7 + 7) % 92) + 4;
    const size = 2.5 + (i % 5) * 1.1;
    const dur = 7 + (i % 7) * 1.8;
    const delay = -((i * 1.9) % dur);
    const dx = ((i % 2 === 0 ? 1 : -1) * (14 + (i % 4) * 9));
    const dy = -(18 + (i % 5) * 10);
    dot.style.setProperty('--x', `${x.toFixed(1)}%`);
    dot.style.setProperty('--y', `${y.toFixed(1)}%`);
    dot.style.setProperty('--size', `${size.toFixed(1)}px`);
    dot.style.setProperty('--dur', `${dur.toFixed(1)}s`);
    dot.style.setProperty('--delay', `${delay.toFixed(1)}s`);
    dot.style.setProperty('--dx', `${dx}px`);
    dot.style.setProperty('--dy', `${dy}px`);
    container.append(dot);
  }
}
initParticles();

// ============================================================================
// Spotlight Mouse Glow (Global Cursor Aura + Card Spotlight)
// ============================================================================
const cursorGlow = $('cursor-glow');
const hideCursorGlow = () => cursorGlow?.classList.remove('visible');

document.addEventListener('pointermove', (event) => {
  if (cursorGlow && event.pointerType !== 'touch') {
    cursorGlow.style.transform = `translate3d(${event.clientX}px, ${event.clientY}px, 0)`;
    if (!cursorGlow.classList.contains('visible')) cursorGlow.classList.add('visible');
  }
  const card = event.target.closest('.spotlight-card');
  if (!card) return;
  const rect = card.getBoundingClientRect();
  card.style.setProperty('--mx', `${event.clientX - rect.left}px`);
  card.style.setProperty('--my', `${event.clientY - rect.top}px`);
});

// Native <select> / date pickers capture pointer events outside the DOM; fade aura cleanly while open.
document.addEventListener('pointerdown', (event) => {
  if (event.target.closest('select, input[type="datetime-local"]')) {
    hideCursorGlow();
  }
});

document.addEventListener('mouseout', (event) => {
  if (!event.relatedTarget) hideCursorGlow();
});
window.addEventListener('blur', hideCursorGlow);

// ============================================================================
// Theme Management (OS Auto on first load, Sun <-> Moon slide switch)
// ============================================================================
function getEffectiveDark() {
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'dark') return true;
  if (attr === 'light') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function applyTheme(mode) {
  if (mode === 'dark' || mode === 'light') {
    localStorage.setItem('reposeer_theme', mode);
    document.documentElement.setAttribute('data-theme', mode);
  } else {
    localStorage.removeItem('reposeer_theme');
    document.documentElement.removeAttribute('data-theme');
  }
  const toggle = $('theme-toggle');
  if (toggle) toggle.checked = getEffectiveDark();
}

function initTheme() {
  const stored = localStorage.getItem('reposeer_theme');
  applyTheme(stored === 'dark' || stored === 'light' ? stored : 'auto');

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!localStorage.getItem('reposeer_theme')) {
      const toggle = $('theme-toggle');
      if (toggle) toggle.checked = getEffectiveDark();
    }
  });

  $('theme-toggle')?.addEventListener('change', (event) => {
    const next = event.target.checked ? 'dark' : 'light';
    applyTheme(next);
  });
}
initTheme();

// ============================================================================
// Clipboard & Count-up Animation
// ============================================================================
async function copyText(text, triggerEl = null) {
  try {
    await navigator.clipboard.writeText(text);
    if (triggerEl) {
      const labelEl = triggerEl.querySelector('span') || triggerEl;
      const original = labelEl.textContent;
      labelEl.textContent = 'Copied';
      setTimeout(() => { labelEl.textContent = original; }, 1200);
    }
  } catch {
    // Ignore clipboard rejection silently
  }
}

function animateNumber(element, target, decimals = 0, prefix = '') {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || typeof target !== 'number' || Number.isNaN(target)) {
    element.textContent = `${prefix}${decimals ? target.toFixed(decimals) : count(target)}`;
    return;
  }
  const duration = 680;
  const start = performance.now();
  const tick = (now) => {
    const t = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    const val = target * eased;
    element.textContent = `${prefix}${decimals ? val.toFixed(decimals) : count(Math.round(val))}`;
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ============================================================================
// Pipeline Stepper & API
// ============================================================================
const PIPE_ORDER = ['submit', 'queued', 'running', 'complete'];

function setPipeline(step, message, state = '') {
  const bar = $('pipeline-bar');
  bar.dataset.state = state || (step === 'complete' ? 'complete' : 'idle');
  $('progress').className = `progress ${state}`.trim();
  $('progress').textContent = message;

  const nodes = bar.querySelectorAll('.pipe-node');
  const idx = PIPE_ORDER.indexOf(step);
  nodes.forEach((el, i) => {
    el.classList.remove('done', 'active', 'error');
    if (state === 'error') {
      if (i < idx) el.classList.add('done');
      else if (i === Math.max(idx, 0)) el.classList.add('error');
    } else if (step === 'complete') {
      el.classList.add('done');
    } else if (idx >= 0) {
      if (i < idx) el.classList.add('done');
      else if (i === idx) el.classList.add('active');
    }
  });
}

function startTimer() {
  startTimeMs = performance.now();
  const el = $('elapsed-timer');
  el.hidden = false;
  el.textContent = '0.0s';
  clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    el.textContent = `${((performance.now() - startTimeMs) / 1000).toFixed(1)}s`;
  }, 100);
}

function stopTimer() {
  clearInterval(timerInterval);
  if (startTimeMs) {
    $('elapsed-timer').textContent = `${((performance.now() - startTimeMs) / 1000).toFixed(2)}s`;
  }
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, { ...options, signal: options.signal || AbortSignal.timeout(15000) });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `http_${response.status}`);
  return payload;
}

function lock(value) {
  busy = value;
  $('analyze').disabled = value;
  const btnText = $('analyze').querySelector('.btn-text');
  if (btnText) btnText.textContent = value ? 'Analyzing…' : 'Analyze repository';
  document.querySelectorAll('.preset-chip, .launch-card').forEach((el) => { el.disabled = value; });
}

async function submit(body, labelHint = '') {
  if (busy) return;
  lock(true);
  $('report').hidden = true;
  $('empty').hidden = true;
  startTimer();
  setPipeline('submit', 'Submitting analysis request to API…', 'busy');
  pollController = new AbortController();
  let lastStep = 'submit';
  try {
    let job = await api('/api/analyses', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    while (['queued', 'running'].includes(job.status)) {
      lastStep = job.status;
      setPipeline(
        job.status,
        job.status === 'queued'
          ? 'Queued · Waiting for the analysis worker.'
          : 'Running · Orchestrator is resolving and evaluating available evidence.',
        'busy'
      );
      await new Promise((resolve) => setTimeout(resolve, 450));
      job = await api(`/api/analyses/${encodeURIComponent(job.id)}`, { signal: pollController.signal });
    }
    if (job.status !== 'complete' || !job.report) throw new Error(job.error || 'invalid_api_response');
    stopTimer();
    report = job.report;
    reportSource = job.source;
    reportScenarioLabel = reportSource === 'mock' ? (scenarioLabels.get(body.scenario) || human(body.scenario)) : '';
    evidenceFilter = 'all';
    evidenceQuery = '';
    pushHistory(report, reportSource, reportScenarioLabel || labelHint || report.mode_used || report.result_status);
    render();
    setPipeline('complete', `Complete · ${reportSource === 'mock' ? 'Synthetic example loaded' : 'Orchestrator returned a validated FinalReport'}.`, '');
  } catch (error) {
    stopTimer();
    $('empty').hidden = false;
    const messages = {
      invalid_submission: 'Check the package, cutoff and analysis options. The API rejected this request.',
      analysis_failed: 'Analysis failed. Check the API configuration and input bundle, then try again.',
      analysis_queue_full: 'The analysis queue is full. Try again after a running analysis finishes.',
      github_mapping_unresolved: 'No verified GitHub-to-package mapping was found. Select a PyPI package and check the backend input data.',
      github_package_selection_required: 'This repository maps to multiple packages. Fill in the optional PyPI mapping field.',
      cutoff_cannot_be_in_the_future: 'The cutoff must be at or before the current UTC time.',
    };
    setPipeline(lastStep, messages[error.message] || 'Cannot reach the analysis API. Check that the backend is running and its allowed origin matches this page.', 'error');
  } finally {
    lock(false);
  }
}

// ============================================================================
// Session History in Sidebar
// ============================================================================
function pushHistory(rep, source, tag) {
  const entry = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    report: rep,
    source,
    tag,
    time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
  };
  sessionHistory.unshift(entry);
  if (sessionHistory.length > 8) sessionHistory.pop();
  renderHistory(entry.id);
}

function renderHistory(activeId) {
  const section = $('history-section');
  const list = $('history-list');
  $('history-count').textContent = sessionHistory.length;
  section.hidden = !sessionHistory.length;
  list.replaceChildren();

  for (const item of sessionHistory) {
    const btn = node('button', null, `history-item ${item.id === activeId ? 'active' : ''}`.trim());
    btn.type = 'button';
    const meta = node('div', null, 'history-item-meta');
    const pkgName = item.report.package_id.replace(/^pypi:/, '');
    const primaryTitle = item.source === 'mock' ? item.tag : pkgName;
    const subTitle = item.source === 'mock' ? `${pkgName} · ${item.time}` : `${human(item.tag)} · ${item.time}`;
    meta.append(
      node('span', primaryTitle, 'history-item-pkg'),
      node('span', subTitle, 'history-item-sub')
    );

    let badgeText = human(item.report.result_status);
    let badgeKind = '';
    if (item.report.result_status === 'predicted') {
      badgeText = `${(item.report.prediction.probability * 100).toFixed(0)}%`;
      badgeKind = item.report.prediction.risk_level || '';
    } else if (item.report.result_status === 'already_abandoned') {
      badgeText = 'Ceased';
      badgeKind = 'high';
    } else if (item.report.result_status === 'scheduled_cessation') {
      badgeText = 'Sched.';
      badgeKind = 'moderate';
    } else {
      badgeText = 'N/A';
    }
    btn.append(meta, node('span', badgeText, `history-badge ${badgeKind}`.trim()));
    btn.addEventListener('click', () => {
      report = item.report;
      reportSource = item.source;
      reportScenarioLabel = item.source === 'mock' ? item.tag : '';
      evidenceFilter = 'all';
      evidenceQuery = '';
      render();
      renderHistory(item.id);
      setPipeline('complete', `Restored from session history (${item.time}).`, '');
      $('report').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    list.append(btn);
  }
}

// ============================================================================
// Form, Shortcuts, Presets & Navigation
// ============================================================================
$('analysis-form').addEventListener('submit', (event) => {
  event.preventDefault();
  document.querySelectorAll('#scenario-pills .preset-chip.active').forEach((b) => b.classList.remove('active'));
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
  submit({ source: 'orchestrator', request }, request.mode_requested);
});

$('clear-identifier')?.addEventListener('click', () => {
  $('identifier').value = '';
  $('identifier').focus();
});

document.addEventListener('keydown', (event) => {
  if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) {
    event.preventDefault();
    $('identifier').focus();
    $('identifier').select();
  } else if (event.key === 'Escape' && !$('json-drawer').hidden) {
    closeJsonDrawer();
  }
});

document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-fill], [data-scenario]');
  if (!trigger || busy) return;
  if (trigger.dataset.fill) {
    $('identifier').value = trigger.dataset.fill;
    $('identifier').focus();
  } else if (trigger.dataset.scenario) {
    const sc = trigger.dataset.scenario;
    document.querySelectorAll('#scenario-pills .preset-chip').forEach((b) => {
      b.classList.toggle('active', b.dataset.scenario === sc);
    });
    submit({ source: 'mock', scenario: sc }, sc);
  }
});

$('sidebar-report-link')?.addEventListener('click', (e) => {
  e.preventDefault();
  if (report && !$('report').hidden) {
    $('report').scrollIntoView({ behavior: 'smooth' });
  }
});

window.addEventListener('pagehide', () => pollController?.abort());

// ============================================================================
// Report Rendering
// ============================================================================
function render() {
  $('empty').hidden = true;
  $('report').hidden = false;
  $('nav-report-status').hidden = false;
  $('sidebar-json-btn').disabled = false;
  $('breadcrumb-current').textContent = report.package_id;

  $('report-package').textContent = report.package_id;
  const scenarioBadge = $('report-scenario-badge');
  if (reportScenarioLabel) {
    scenarioBadge.textContent = reportScenarioLabel;
    scenarioBadge.hidden = false;
  } else {
    scenarioBadge.hidden = true;
  }
  const runChip = $('report-run-id');
  runChip.textContent = `run:${report.analysis_run_id.slice(0, 8)}…`;
  runChip.onclick = () => copyText(report.analysis_run_id, runChip);

  $('report-context').textContent = `${reportSource === 'mock' ? 'SYNTHETIC EXAMPLE' : 'ORCHESTRATOR OUTPUT'} · ${human(report.purpose).toUpperCase()}`;
  $('report-subtitle').textContent = `Cutoff ${date(report.cutoff_at)} · Generated ${date(report.generated_at)}`;
  $('synthetic-notice').hidden = !(reportSource === 'mock' || report.package_id === 'pypi:synthetic-demo');

  $('evidence-count').textContent = report.evidence.length;
  $('coverage-count').textContent = report.coverage.length;
  $('sources-count').textContent = report.sources.length;

  renderOutcome();
  renderMetadata();
  renderImpact();
  renderDetails();
}

function buildRadialGauge(probability, riskLevel) {
  const clamped = Math.min(Math.max(probability, 0), 1);
  const pct = clamped * 100;
  const wrap = node('div', null, 'gauge-wrap');
  const svg = svgEl('svg', { class: 'risk-gauge-svg', viewBox: '0 0 220 132', role: 'img', 'aria-label': `${pct.toFixed(1)}% estimated 12-month cessation probability` });

  const defs = svgEl('defs');
  const addGrad = (id, c1, c2) => {
    const g = svgEl('linearGradient', { id, x1: '0%', y1: '0%', x2: '100%', y2: '0%' });
    g.append(svgEl('stop', { offset: '0%', 'stop-color': c1 }), svgEl('stop', { offset: '100%', 'stop-color': c2 }));
    defs.append(g);
  };
  addGrad('gauge-low', '#34d399', '#059669');
  addGrad('gauge-mod', '#fbbf24', '#ea580c');
  addGrad('gauge-high', '#fb7185', '#dc2626');
  svg.append(defs);

  // Semi-circle arc from (24, 112) to (196, 112), radius 86
  const r = 86;
  const arcLen = Math.PI * r;
  const d = 'M 24 112 A 86 86 0 0 1 196 112';

  svg.append(svgEl('path', { d, class: 'gauge-track' }));

  const levelClass = ['low', 'moderate', 'high'].includes(riskLevel) ? riskLevel : (pct < 35 ? 'low' : pct < 70 ? 'moderate' : 'high');
  const arc = svgEl('path', {
    d,
    class: `gauge-arc ${levelClass}`,
    'stroke-dasharray': arcLen.toFixed(2),
    'stroke-dashoffset': arcLen.toFixed(2),
  });
  svg.append(arc);

  // Tick labels 0% and 100%
  svg.append(
    svgEl('text', { x: 18, y: 129, class: 'gauge-sub-text', 'text-anchor': 'start' }, '0%'),
    svgEl('text', { x: 110, y: 15, class: 'gauge-sub-text', 'text-anchor': 'middle' }, '50%'),
    svgEl('text', { x: 202, y: 129, class: 'gauge-sub-text', 'text-anchor': 'end' }, '100%')
  );

  const valText = svgEl('text', { x: 105, y: 94, class: 'gauge-value-text', 'text-anchor': 'middle' }, '0.0');
  const unitText = svgEl('text', { x: 154, y: 88, class: 'gauge-unit-text', 'text-anchor': 'start' }, '%');
  const subText = svgEl('text', { x: 110, y: 114, class: 'gauge-sub-text', 'text-anchor': 'middle' }, 'CESSATION PROB.');
  svg.append(valText, unitText, subText);

  wrap.append(svg);

  // Animate arc & counter on next frame
  requestAnimationFrame(() => {
    const offset = arcLen * (1 - clamped);
    arc.style.strokeDashoffset = offset.toFixed(2);
    animateNumber(valText, pct, 1);
  });

  return wrap;
}

function renderOutcome() {
  const card = $('outcome');
  const label = node('span', report.result_status === 'predicted' ? '12-MONTH MAINTENANCE RISK' : 'REPOSITORY STATUS', 'card-label');
  card.replaceChildren(label);

  // Never infer a probability from official status or replace missing values with zero.
  if (report.result_status === 'predicted') {
    const prob = report.prediction.probability;
    const rl = report.prediction.risk_level;
    const pillKind = rl === 'low' ? 'good' : rl === 'high' ? 'danger' : 'warning';
    const center = node('div', null, 'outcome-center');
    center.append(
      pill(`${human(rl)} risk`, pillKind),
      node('p', `Estimated cessation probability by ${date(report.horizon_end_at)}.`)
    );
    card.append(buildRadialGauge(prob, rl), center);
  } else if (['already_abandoned', 'scheduled_cessation'].includes(report.result_status)) {
    const isAbandoned = report.result_status === 'already_abandoned';
    const emblem = node('div', null, `status-emblem ${isAbandoned ? 'danger' : 'warning'}`);
    const icon = svgEl('svg', { width: 22, height: 22, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
    if (isAbandoned) {
      icon.append(svgEl('circle', { cx: 12, cy: 12, r: 10 }), svgEl('line', { x1: 15, y1: 9, x2: 9, y2: 15 }), svgEl('line', { x1: 9, y1: 9, x2: 15, y2: 15 }));
    } else {
      icon.append(svgEl('circle', { cx: 12, cy: 12, r: 10 }), svgEl('polyline', { points: '12 6 12 12 16 14' }));
    }
    emblem.append(icon);

    card.append(
      emblem,
      node('h3', isAbandoned ? 'Known cessation' : 'Scheduled cessation'),
      pill(human(report.official_status.event_type), isAbandoned ? 'danger' : 'warning')
    );

    if (report.result_status === 'scheduled_cessation') {
      const box = node('div', null, 'horizon-timeline');
      const cutoffMs = new Date(report.cutoff_at).getTime();
      const effMs = new Date(report.official_status.effective_at).getTime();
      const endMs = new Date(report.horizon_end_at).getTime();
      const ratio = endMs > cutoffMs ? Math.min(Math.max((effMs - cutoffMs) / (endMs - cutoffMs), 0.08), 1) : 0.5;
      box.append(node('strong', report.within_horizon ? 'Within the 12-month horizon' : 'Outside the 12-month horizon'));
      const bar = node('div', null, 'horizon-bar');
      const fill = node('div', null, 'horizon-bar-fill');
      fill.style.width = `${(ratio * 100).toFixed(0)}%`;
      bar.append(fill);
      const labels = node('div', null, 'horizon-labels');
      labels.append(node('span', `Cutoff ${shortDate(report.cutoff_at)}`), node('span', `Effective ${shortDate(report.official_status.effective_at)}`));
      box.append(bar, labels);
      card.append(box);
    } else {
      card.append(node('p', `Effective ${date(report.official_status.effective_at)}`));
    }
    card.append(node('p', 'Official status takes precedence. A prediction is not reported.'));
  } else {
    const emblem = node('div', null, 'status-emblem neutral');
    const icon = svgEl('svg', { width: 22, height: 22, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2 });
    icon.append(svgEl('circle', { cx: 12, cy: 12, r: 10 }), svgEl('line', { x1: 12, y1: 8, x2: 12, y2: 12 }), svgEl('line', { x1: 12, y1: 16, x2: 12.01, y2: 16 }));
    emblem.append(icon);
    card.append(
      emblem,
      node('h3', 'No score available'),
      pill(human(report.result_status), 'neutral'),
      node('p', report.reason),
      node('p', report.reason_code, 'long-value')
    );
  }
}

function renderMetadata() {
  const card = $('metadata');
  card.replaceChildren(node('span', 'ANALYSIS CONTEXT', 'card-label'));

  const flow = node('div', null, 'mode-flow');
  flow.append(
    node('span', report.mode_requested, 'mode-chip'),
    node('span', '→', 'mode-arrow'),
    node('span', report.mode_used || 'NO MODEL', 'mode-chip'),
    pill(human(report.enhanced_status), report.enhanced_status === 'used' ? 'good' : report.enhanced_status === 'fallback' ? 'warning' : 'neutral')
  );
  card.append(flow);

  if (report.fallback_reason) {
    card.append(node('p', `Fallback: ${human(report.fallback_reason)}`, 'detail-note'));
  }

  card.append(
    row('Applicability', human(report.applicability.status)),
    row('External insights', human(report.external_insights_status)),
    row('Mapping scope', human(report.mapping?.scope_quality))
  );
  if (report.mapping?.repo_id) {
    card.append(row('Repository', report.mapping.repo_id));
  }
  if (report.external_insights_reason) {
    card.append(node('p', report.external_insights_reason));
  }
}

function buildOrbitSvg(direct, transitive) {
  const svg = svgEl('svg', { class: 'orbit-svg', viewBox: '0 0 96 96', 'aria-hidden': 'true' });
  svg.append(
    svgEl('circle', { cx: 48, cy: 48, r: 40, class: 'orbit-ring outer' }),
    svgEl('circle', { cx: 48, cy: 48, r: 24, class: 'orbit-ring inner' }),
    svgEl('circle', { cx: 48, cy: 48, r: 10, fill: 'var(--primary)', opacity: 0.2 }),
    svgEl('circle', { cx: 48, cy: 48, r: 5.5, fill: 'var(--primary)' })
  );
  const directNodes = Math.min(Math.max(direct || 0, 0), 6);
  for (let i = 0; i < directNodes; i++) {
    const a = (i / directNodes) * Math.PI * 2 - Math.PI / 4;
    svg.append(svgEl('circle', { cx: (48 + 24 * Math.cos(a)).toFixed(1), cy: (48 + 24 * Math.sin(a)).toFixed(1), r: 3.5, fill: 'var(--primary)' }));
  }
  const extraTrans = Math.min(Math.max((transitive || 0) - (direct || 0), 0), 8);
  for (let i = 0; i < extraTrans; i++) {
    const a = (i / extraTrans) * Math.PI * 2 + 0.3;
    svg.append(svgEl('circle', { cx: (48 + 40 * Math.cos(a)).toFixed(1), cy: (48 + 40 * Math.sin(a)).toFixed(1), r: 3, fill: 'var(--accent)' }));
  }
  return svg;
}

function renderImpact() {
  const card = $('impact-card');
  const impact = report.dependency_impact;
  card.replaceChildren(node('span', 'DEPENDENCY IMPACT', 'card-label'));

  if (!impact || impact.impact_status === 'unavailable') {
    const emblem = node('div', null, 'status-emblem neutral');
    const icon = svgEl('svg', { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2 });
    icon.append(svgEl('circle', { cx: 18, cy: 5, r: 3 }), svgEl('circle', { cx: 6, cy: 12, r: 3 }), svgEl('circle', { cx: 18, cy: 19, r: 3 }), svgEl('line', { x1: 8.59, y1: 13.51, x2: 15.42, y2: 17.49 }), svgEl('line', { x1: 15.41, y1: 6.51, x2: 8.59, y2: 10.49 }));
    emblem.append(icon);
    card.append(
      emblem,
      pill(impact ? 'Unavailable' : 'Not requested', 'neutral'),
      node('p', impact?.missing_reason ? human(impact.missing_reason) : 'Enable dependency impact to request a separate ecosystem assessment.')
    );
  } else {
    const visualRow = node('div', null, 'impact-visual-row');
    visualRow.append(buildOrbitSvg(impact.direct_dependents, impact.transitive_dependents));

    const numbers = node('div', null, 'impact-numbers');
    const prefix = impact.counts_are_lower_bounds ? '≥ ' : '';
    for (const [label, value] of [['Direct', impact.direct_dependents], ['Transitive', impact.transitive_dependents]]) {
      const tile = node('div', null, 'impact-tile');
      const valEl = node('strong', `${prefix}${count(value)}`);
      tile.append(node('span', label), valEl);
      numbers.append(tile);
      if (typeof value === 'number') animateNumber(valEl, value, 0, prefix);
    }
    visualRow.append(numbers);
    card.append(visualRow);

    if (impact.corpus_package_count && typeof impact.transitive_dependents === 'number') {
      const wrap = node('div', null, 'corpus-bar-wrap');
      const share = ((impact.transitive_dependents / impact.corpus_package_count) * 100).toFixed(1);
      const head = node('div', null, 'corpus-bar-head');
      head.append(node('span', `${count(impact.corpus_package_count)} pkgs in ${impact.corpus_id}`), node('strong', `${share}% corpus`));
      const track = node('div', null, 'corpus-bar-track');
      const dW = Math.min(((impact.direct_dependents || 0) / impact.corpus_package_count) * 100, 100);
      const tW = Math.min((Math.max((impact.transitive_dependents || 0) - (impact.direct_dependents || 0), 0) / impact.corpus_package_count) * 100, 100 - dW);
      const dBar = node('div', null, 'corpus-bar-direct'); dBar.style.width = `${dW}%`;
      const tBar = node('div', null, 'corpus-bar-trans'); tBar.style.width = `${tW}%`;
      track.append(dBar, tBar);
      wrap.append(head, track);
      card.append(wrap);
    }

    card.append(
      node('p', `${human(impact.graph_kind)} · As of ${shortDate(impact.graph_as_of)}`),
      node('p', `Sources: ${impact.source_ids.join(', ')}`)
    );
    if (impact.missing_reason) card.append(node('p', human(impact.missing_reason), 'detail-note'));
  }

  card.append(node('div', null, 'card-divider'), node('p', 'Impact is independent of cessation probability. Counts describe this corpus, not the entire ecosystem.'));
}

// ============================================================================
// Tabs & Details
// ============================================================================
const tabs = [...document.querySelectorAll('[role=tab]')];
function selectTab(tab, focus = false) {
  selectedTab = tab.dataset.tab;
  for (const item of tabs) {
    const selected = item === tab;
    item.setAttribute('aria-selected', selected);
    item.tabIndex = selected ? 0 : -1;
  }
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
    event.preventDefault();
    selectTab(tabs[index], true);
  });
}

function renderDetails() {
  const detail = $('details');
  detail.replaceChildren();
  ({ evidence: renderEvidence, coverage: renderCoverage, sources: renderSources, model: renderModel })[selectedTab](detail);
}

function heading(parent, title, subtitle) {
  const head = node('div', null, 'detail-heading');
  const text = node('div');
  text.append(node('h2', title), node('p', subtitle));
  head.append(text);
  parent.append(head);
}

function renderEvidence(parent) {
  parent.replaceChildren();
  heading(parent, 'The evidence behind the report', 'Citations retain their source, quote offsets and role in the analysis.');
  if (!report.evidence.length) {
    parent.append(node('p', 'No admissible evidence was returned for this report.', 'empty-message'));
  } else {
    const toolbar = node('div', null, 'evidence-toolbar');
    const filterRow = node('div', null, 'evidence-filters');
    const filters = [
      { id: 'all', label: `All (${report.evidence.length})` },
      { id: 'feature', label: `Feature (${report.evidence.filter((e) => e.use === 'feature').length})` },
      { id: 'report_only', label: `Report only (${report.evidence.filter((e) => e.use === 'report_only').length})` },
      { id: 'agent', label: `Agent (${report.evidence.filter((e) => Boolean(e.agent)).length})` },
      { id: 'external', label: `External (${report.evidence.filter((e) => e.origin === 'external').length})` },
    ];

    for (const f of filters) {
      const chip = node('button', f.label, `filter-chip ${evidenceFilter === f.id ? 'active' : ''}`.trim());
      chip.type = 'button';
      chip.addEventListener('click', () => {
        evidenceFilter = f.id;
        renderEvidence(parent);
      });
      filterRow.append(chip);
    }

    const searchInput = node('input', null, 'evidence-search');
    searchInput.type = 'search';
    searchInput.placeholder = 'Filter claims or quotes…';
    searchInput.value = evidenceQuery;
    searchInput.addEventListener('input', (e) => {
      evidenceQuery = e.target.value;
      renderEvidenceList(listContainer);
    });

    toolbar.append(filterRow, searchInput);
    parent.append(toolbar);

    const listContainer = node('div');
    parent.append(listContainer);
    renderEvidenceList(listContainer);
  }

  if (report.official_status) {
    const official = report.official_status;
    parent.append(
      node('div', null, 'card-divider'),
      node('h3', 'Official status verification'),
      row('Announced', date(official.announced_at)),
      row('Verified', date(official.verified_at)),
      row('Verifier', official.verifier),
      row('Ledger revision', official.ledger_revision),
      row('Evidence IDs', official.evidence_ids.join(', '))
    );
  }
}

function renderEvidenceList(container) {
  container.replaceChildren();
  const q = evidenceQuery.trim().toLowerCase();
  const filtered = report.evidence.filter((item) => {
    if (evidenceFilter === 'feature' && item.use !== 'feature') return false;
    if (evidenceFilter === 'report_only' && item.use !== 'report_only') return false;
    if (evidenceFilter === 'agent' && !item.agent) return false;
    if (evidenceFilter === 'external' && item.origin !== 'external') return false;
    if (q) {
      const hay = `${item.claim} ${item.signal_type} ${item.citations.map((c) => c.quote).join(' ')}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (!filtered.length) {
    container.append(node('p', 'No evidence items match the active filter.', 'empty-message'));
    return;
  }

  for (const evidence of filtered) {
    const item = node('article', null, `evidence-item origin-${evidence.origin || 'core'}`);
    const tags = node('div', null, 'evidence-tags');
    tags.append(
      pill(human(evidence.origin), evidence.origin === 'agent' ? 'accent' : ''),
      pill(human(evidence.signal_type), 'neutral'),
      pill(human(evidence.use), evidence.use === 'report_only' ? 'warning' : 'good'),
      pill(human(evidence.context), 'neutral')
    );
    item.append(tags, node('div', evidence.claim, 'claim'));

    for (const citation of evidence.citations) {
      const source = report.sources.find((s) => s.source_id === citation.source_id);
      const block = node('div', null, 'citation');
      block.append(node('blockquote', citation.quote));

      const meta = node('div', null, 'citation-meta');
      const sourceInfo = node('div');
      sourceInfo.append(
        source ? link(source.source_url, `${source.source} · ${citation.source_id}`) : node('span', citation.source_id),
        node('span', ` · Offsets ${citation.start_offset}–${citation.end_offset} · Event ${date(evidence.event_at)}`)
      );
      const copyBtn = node('button', 'Copy quote', 'copy-quote-btn');
      copyBtn.type = 'button';
      copyBtn.addEventListener('click', () => copyText(citation.quote, copyBtn));
      meta.append(sourceInfo, copyBtn);
      block.append(meta);
      item.append(block);
    }

    if (evidence.exclusion_reason) item.append(node('p', `Report only: ${human(evidence.exclusion_reason)}`, 'detail-note'));
    if (evidence.uncertainty) item.append(node('p', `Uncertainty: ${evidence.uncertainty}`, 'detail-note'));
    if (evidence.agent) {
      item.append(node('p', `Agent: ${evidence.agent.agent_id} · ${evidence.agent.model_version} · prompt ${evidence.agent.prompt_version}`, 'long-value'));
    }
    container.append(item);
  }
}

function renderCoverage(parent) {
  heading(parent, 'Coverage & missingness', 'Unknown counts remain unknown. Completeness and historical validity are separate checks.');
  if (!report.coverage.length) {
    parent.append(node('p', 'No coverage records returned.', 'empty-message'));
    return;
  }

  const total = report.coverage.length;
  const complete = report.coverage.filter((c) => c.status === 'complete').length;
  const partial = report.coverage.filter((c) => c.status === 'partial').length;
  const other = total - complete - partial;

  const summary = node('div', null, 'coverage-summary');
  const kpis = node('div', null, 'coverage-kpis');
  kpis.append(
    pill(`${complete} Complete`, 'good'),
    pill(`${partial} Partial`, partial ? 'warning' : 'neutral'),
    pill(`${total} Stages checked`, 'neutral')
  );
  const segBar = node('div', null, 'coverage-seg-bar');
  if (complete) { const s = node('div', null, 'seg-complete'); s.style.width = `${(complete / total) * 100}%`; segBar.append(s); }
  if (partial) { const s = node('div', null, 'seg-partial'); s.style.width = `${(partial / total) * 100}%`; segBar.append(s); }
  if (other) { const s = node('div', null, 'seg-other'); s.style.width = `${(other / total) * 100}%`; segBar.append(s); }
  summary.append(kpis, segBar);
  parent.append(summary);

  const wrap = node('div', null, 'table-scroll');
  const table = node('table');
  const head = node('thead');
  const tr = node('tr');
  for (const text of ['Stage / source', 'Status', 'Source census', 'History', 'Observed', 'Processed / eligible']) {
    tr.append(node('th', text));
  }
  head.append(tr);
  table.append(head);

  const body = node('tbody');
  for (const cov of report.coverage) {
    const rowEl = node('tr');
    const first = node('td', cov.coverage_id);
    first.append(node('small', `${cov.stage} · ${cov.source_ids.join(', ') || 'No sources'}`));
    if (cov.window) first.append(node('small', `${date(cov.window.start_at)} – ${date(cov.window.end_at)}`));
    if (cov.missing_reason) first.append(node('small', human(cov.missing_reason)));

    const tdStatus = node('td'); tdStatus.append(pill(human(cov.status), cov.status === 'complete' ? 'good' : cov.status === 'partial' ? 'warning' : 'neutral'));
    const tdCensus = node('td'); tdCensus.append(pill(human(cov.source_coverage), cov.source_coverage === 'complete' ? 'good' : cov.source_coverage === 'partial' ? 'warning' : 'neutral'));
    const tdHist = node('td'); tdHist.append(pill(human(cov.historical_validity), cov.historical_validity === 'verified' ? 'good' : 'neutral'));

    rowEl.append(
      first,
      tdStatus,
      tdCensus,
      tdHist,
      node('td', count(cov.records_observed)),
      node('td', `${count(cov.processed_documents)} / ${count(cov.eligible_documents)}`)
    );
    body.append(rowEl);
  }
  table.append(body);
  wrap.append(table);
  parent.append(wrap);
}

function renderSources(parent) {
  heading(parent, 'Source provenance & cutoff', 'Retrieval time alone does not establish historical availability.');

  const banner = node('div', null, 'cutoff-banner');
  const left = node('div');
  left.append(node('span', 'Strict analysis cutoff: '), node('strong', date(report.cutoff_at)));
  const right = node('div');
  right.append(node('span', 'Forecast horizon end: '), node('strong', date(report.horizon_end_at)));
  banner.append(left, right);
  parent.append(banner);

  if (!report.sources.length) {
    parent.append(node('p', 'No source records returned.', 'empty-message'));
    return;
  }

  const grid = node('div', null, 'sources-grid');
  for (const source of report.sources) {
    const item = node('article', null, 'source-item');
    const title = link(source.source_url, `${source.source_id} · ${source.source}`);
    title.className = 'source-title';
    item.append(
      title,
      row('Available at', date(source.available_at)),
      row('Retrieved at', date(source.retrieved_at)),
      row('Valid at', date(source.valid_at)),
      row('Temporal basis', human(source.temporal_basis)),
      row('Revision', source.revision || 'Unknown'),
      row('Raw records', source.raw_record_ids.join(', ') || 'None recorded'),
      row('Missing fields', source.missing_fields.join(', ') || 'None recorded')
    );
    if (source.content_sha256) {
      const shaRow = node('div', null, 'long-value');
      shaRow.append(node('span', `SHA-256: ${source.content_sha256.slice(0, 18)}…`));
      const copySha = node('button', 'Copy SHA', 'copy-mini-btn');
      copySha.type = 'button';
      copySha.addEventListener('click', () => copyText(source.content_sha256, copySha));
      shaRow.append(copySha);
      item.append(shaRow);
    } else {
      item.append(node('p', 'SHA-256: Unknown', 'long-value'));
    }
    grid.append(item);
  }
  parent.append(grid);
}

function renderModel(parent) {
  heading(parent, 'Model context & limitations', 'Use the report within its declared population, time boundary and evidence coverage.');

  if (report.result_status === 'predicted') {
    if (report.prediction && report.core_prediction) {
      const deltaCard = node('div', null, 'model-delta-card');
      const p1 = report.prediction.probability * 100;
      const p0 = report.core_prediction.probability * 100;
      const diff = p1 - p0;
      const sign = diff >= 0 ? '+' : '';

      const head = node('div', null, 'model-delta-header');
      head.append(
        node('strong', 'ENHANCED vs CORE Baseline Comparison'),
        pill(`${sign}${diff.toFixed(1)} pp delta`, diff > 0 ? 'warning' : 'good')
      );
      deltaCard.append(head);

      const addBar = (label, pct, cls) => {
        const r = node('div', null, 'delta-bar-row');
        const track = node('div', null, 'delta-track');
        const fill = node('div', null, `delta-fill ${cls}`);
        fill.style.width = `${Math.min(Math.max(pct, 0), 100).toFixed(1)}%`;
        track.append(fill);
        r.append(node('span', label), track, node('span', `${pct.toFixed(1)}%`));
        deltaCard.append(r);
      };
      addBar('Selected (ENHANCED)', p1, 'primary');
      addBar('CORE Baseline', p0, 'baseline');
      parent.append(deltaCard);
    }

    const grid = node('div', null, 'model-cards-grid');
    for (const [label, prediction] of [['Selected prediction', report.prediction], ['CORE baseline', report.core_prediction]]) {
      if (!prediction) continue;
      const m = prediction.model;
      const card = node('div', null, 'model-subcard');
      card.append(
        node('h3', label),
        row('Model', `${m.model_id} / ${m.model_version}`),
        row('Feature set', m.feature_set),
        row('Calibration', m.calibration_version),
        row('Risk thresholds', m.risk_threshold_version),
        row('Training pop.', m.training_population),
        row('Calibration pop.', m.calibration_population),
        row('Frozen at', date(m.frozen_at)),
        row('Probability', `${(prediction.probability * 100).toFixed(1)}%`)
      );
      grid.append(card);
    }
    parent.append(grid);
  } else {
    parent.append(node('p', 'This report does not contain a model prediction.'));
  }

  parent.append(
    row('Applicability policy', report.applicability.policy_version),
    row('Analysis run', report.analysis_run_id)
  );
  if (report.backtest) parent.append(node('p', JSON.stringify(report.backtest), 'long-value'));

  const limitations = [...report.applicability.reasons, ...report.applicability.limitations, ...report.limitations];
  if (limitations.length) {
    parent.append(node('div', null, 'card-divider'), node('h3', 'Declared limitations'));
    const list = node('ul', null, 'limitations');
    for (const limitation of new Set(limitations)) list.append(node('li', limitation));
    parent.append(list);
  }
}

// ============================================================================
// Safe Syntax-Highlighted JSON Inspector Drawer (Zero innerHTML!)
// ============================================================================
function renderHighlightedJson(container, value, indent = 0) {
  const pad = '  '.repeat(indent);
  const nextPad = '  '.repeat(indent + 1);
  if (value === null) {
    container.append(node('span', 'null', 'jl'));
  } else if (typeof value === 'boolean') {
    container.append(node('span', String(value), 'jb'));
  } else if (typeof value === 'number') {
    container.append(node('span', String(value), 'jn'));
  } else if (typeof value === 'string') {
    container.append(node('span', JSON.stringify(value), 'js'));
  } else if (Array.isArray(value)) {
    if (!value.length) { container.append('[]'); return; }
    container.append('[\n');
    value.forEach((item, i) => {
      container.append(nextPad);
      renderHighlightedJson(container, item, indent + 1);
      container.append(i < value.length - 1 ? ',\n' : '\n');
    });
    container.append(`${pad}]`);
  } else if (typeof value === 'object') {
    const entries = Object.entries(value);
    if (!entries.length) { container.append('{}'); return; }
    container.append('{\n');
    entries.forEach(([k, v], i) => {
      container.append(nextPad, node('span', JSON.stringify(k), 'jk'), ': ');
      renderHighlightedJson(container, v, indent + 1);
      container.append(i < entries.length - 1 ? ',\n' : '\n');
    });
    container.append(`${pad}}`);
  }
}

function openJsonDrawer() {
  if (!report) return;
  const viewer = $('json-viewer');
  viewer.replaceChildren();
  renderHighlightedJson(viewer, report);
  $('drawer-subtitle').textContent = `${report.package_id} · ${report.analysis_run_id}`;
  $('json-drawer-backdrop').hidden = false;
  $('json-drawer').hidden = false;
}

function closeJsonDrawer() {
  $('json-drawer-backdrop').hidden = true;
  $('json-drawer').hidden = true;
}

$('inspect-json')?.addEventListener('click', openJsonDrawer);
$('sidebar-json-btn')?.addEventListener('click', openJsonDrawer);
$('drawer-close')?.addEventListener('click', closeJsonDrawer);
$('json-drawer-backdrop')?.addEventListener('click', closeJsonDrawer);
$('drawer-copy')?.addEventListener('click', (e) => {
  if (report) copyText(JSON.stringify(report, null, 2), e.currentTarget);
});

$('download').addEventListener('click', () => {
  if (!report) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2) + '\n'], { type: 'application/json' }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `reposeer-${report.analysis_run_id}.json`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

$('copy-json')?.addEventListener('click', (e) => {
  if (report) copyText(JSON.stringify(report, null, 2), e.currentTarget);
});

// ============================================================================
// Initial API Connection
// ============================================================================
async function connect() {
  try {
    await api('/api/health');
    $('connection').textContent = 'API connected';
    $('connection').className = 'connection online';
    const { scenarios } = await api('/api/scenarios');
    const pills = $('scenario-pills');
    pills.replaceChildren();
    if (!scenarios.length) {
      pills.append(node('span', 'Examples not enabled on API server', 'muted'));
    } else {
      scenarioLabels.clear();
      for (const scenario of scenarios) {
        scenarioLabels.set(scenario.id, scenario.label);
        const btn = node('button', scenario.label, 'preset-chip');
        btn.type = 'button';
        btn.dataset.scenario = scenario.id;
        pills.append(btn);
      }
    }
  } catch {
    $('connection').textContent = 'API offline';
    $('connection').className = 'connection offline';
    $('scenario-pills')?.replaceChildren(node('span', 'API offline', 'muted'));
    setPipeline('submit', 'Start the backend API to analyze a package or load example reports.', 'error');
  }
}
connect();
