import { escapeHtml, parseNumber } from './util.js';

/** Promise-based modal dialogs on one native <dialog id="modal">. */
function open(html, wire) {
  const dialog = document.getElementById('modal');
  return new Promise((resolve) => {
    dialog.innerHTML = `<form method="dialog" class="modal-body">${html}</form>`;
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      if (dialog.open) dialog.close();
      resolve(value);
    };
    // Esc or programmatic close = cancel. The "close" event is queued, so when one dialog opens right
    // after another, the previous close arrives while this one is already open: ignore it.
    dialog.onclose = () => { if (!dialog.open) done(null); };
    wire(dialog, done);
    dialog.showModal();
    const first = dialog.querySelector('[autofocus]') || dialog.querySelector('input, button');
    if (first) {
      first.focus();
      if (first.select) first.select();
    }
  });
}

/** Ask for a number. Resolves to the number, or null when cancelled. */
export function promptNumber({ title, label, value = '', suffix = '', hint = '', okLabel = 'Apply', min = null }) {
  return open(`
    <h3>${escapeHtml(title)}</h3>
    <label class="modal-field">${escapeHtml(label)}
      <span class="input-suffix"><input name="value" type="text" inputmode="decimal" autocomplete="off" autofocus
        value="${escapeHtml(value)}"><span>${escapeHtml(suffix)}</span></span></label>
    ${hint ? `<p class="muted small">${escapeHtml(hint)}</p>` : ''}
    <p class="modal-error" hidden></p>
    <div class="actions"><button class="btn ghost" value="cancel" type="button" data-cancel>Cancel</button>
      <button class="btn primary" value="ok">${escapeHtml(okLabel)}</button></div>`, (dialog, done) => {
    const form = dialog.querySelector('form');
    dialog.querySelector('[data-cancel]').addEventListener('click', () => done(null));
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const number = parseNumber(form.elements.value.value);
      const error = dialog.querySelector('.modal-error');
      if (number == null || (min != null && number <= min)) {
        error.textContent = min != null ? `Enter a number greater than ${min}.` : 'Enter a number.';
        error.hidden = false;
        return;
      }
      done(number);
    });
  });
}

/** Offer choices: options = [{ label, value, detail, primary }]. Resolves to the value, or null. */
export function choose({ title, message, options }) {
  return open(`
    <h3>${escapeHtml(title)}</h3>
    <p>${message}</p>
    <div class="modal-choices">${options.map((o, i) => `
      <button class="btn ${o.primary ? 'primary' : 'ghost'} choice-btn" type="button" data-i="${i}">
        <b>${escapeHtml(o.label)}</b>${o.detail ? `<small>${escapeHtml(o.detail)}</small>` : ''}</button>`).join('')}
    </div>
    <div class="actions"><button class="btn ghost" type="button" data-cancel>Cancel</button></div>`, (dialog, done) => {
    dialog.querySelector('[data-cancel]').addEventListener('click', () => done(null));
    dialog.querySelectorAll('[data-i]').forEach((b) => b.addEventListener('click', () => done(options[Number(b.dataset.i)].value)));
  });
}

/** Pick one item of a list. items = [{ label, detail, value }]. */
export function pickFromList({ title, message = '', items, empty = 'Nothing to show.' }) {
  return open(`
    <h3>${escapeHtml(title)}</h3>
    ${message ? `<p class="muted">${message}</p>` : ''}
    <ul class="modal-list">${items.length ? items.map((it, i) => `
      <li><button type="button" data-i="${i}"><b>${escapeHtml(it.label)}</b>
        <small>${escapeHtml(it.detail || '')}</small></button></li>`).join('') : `<li class="muted">${escapeHtml(empty)}</li>`}
    </ul>
    <div class="actions"><button class="btn ghost" type="button" data-cancel>Cancel</button></div>`, (dialog, done) => {
    dialog.querySelector('[data-cancel]').addEventListener('click', () => done(null));
    dialog.querySelectorAll('[data-i]').forEach((b) => b.addEventListener('click', () => done(items[Number(b.dataset.i)].value)));
  });
}
