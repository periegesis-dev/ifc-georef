import { duration, escapeHtml } from './util.js';

const ICONS = { done: '✓', cached: '↺', skipped: '–', error: '!', pending: '', running: '' };

/** Overall progress (0..1) of several tasks, weighted. */
export function combine(parts) {
  const total = parts.reduce((s, p) => s + p.weight, 0) || 1;
  return parts.reduce((s, p) => s + p.weight * Math.min(Math.max(p.progress || 0, 0), 1), 0) / total;
}

/** Estimated seconds left from the elapsed time and progress so far. */
export function eta(startedAt, progress) {
  if (!startedAt || progress < 0.04 || progress >= 1) return null;
  const elapsed = (Date.now() - startedAt) / 1000;
  return elapsed / progress - elapsed;
}

function stepValue(step, now) {
  if (step.state === 'running') {
    const pct = Math.round((step.progress || 0) * 100);
    if (pct > 0) return `${pct} %`;
    return step.started_at ? duration(now - step.started_at) : '';
  }
  if (step.state === 'cached') return 'cached';
  if ((step.state === 'done' || step.state === 'error') && step.started_at && step.ended_at) {
    return duration(step.ended_at - step.started_at);
  }
  return '';
}

/**
 * Render a panel: { title, progress (0..1), eta (s), steps: [{label, state, progress, detail, started_at, ended_at}], error }
 */
export function renderProgress(container, model) {
  const now = Date.now() / 1000;
  const pct = Math.round((model.progress || 0) * 100);
  const failed = Boolean(model.error);
  let etaText = '';
  if (failed) etaText = 'Stopped';
  else if (pct >= 100) etaText = 'Done';
  else if (model.waiting) etaText = model.waiting;
  else if (model.eta != null) etaText = model.eta < 5 ? 'Almost done' : `About ${duration(model.eta)} left`;

  const steps = (model.steps || []).map((s) => {
    const showBar = s.state === 'running' && s.progress > 0;
    return `<li class="${s.state}">
      <span class="icon">${ICONS[s.state] || ''}</span>
      <span class="label">${escapeHtml(s.label)}</span>
      <span class="value">${stepValue(s, now)}</span>
      ${s.detail ? `<span class="detail">${escapeHtml(s.detail)}</span>` : ''}
      ${showBar ? `<div class="bar"><span style="width:${Math.round(s.progress * 100)}%"></span></div>` : ''}
    </li>`;
  }).join('');

  container.hidden = false;
  container.innerHTML = `
    <div class="progress-head">
      <strong>${escapeHtml(model.title)} &middot; ${pct} %</strong>
      <span class="eta">${escapeHtml(etaText)}</span>
    </div>
    <div class="bar big${failed ? ' error' : ''}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
      <span style="width:${pct}%"></span>
    </div>
    <ul class="steps">${steps}</ul>
    ${failed ? `<div class="progress-error">${escapeHtml(model.error)}</div>` : ''}`;
}

/** Steps of a server task (status.tasks.<name>) as panel steps. */
export function taskSteps(task) {
  return (task && task.steps) ? task.steps : [];
}
