import { api, ApiError, downloadUrl } from './api.js';
import { combine, eta, renderProgress } from './progress.js';
import { $, $$, bytes, debounce, escapeHtml, fixed, grouped, parseNumber, toast } from './util.js';
import { createMap, defaultBasemap, pinIcon } from './map.js';
import { pickFromList } from './dialogs.js';
import {
  CONFIG, crsUnitSymbol, defaultPlacement, hideError, session, showError, site, siteHasLocation, state,
  syncPointIds,
} from './store.js';
import {
  cancelPicking, enterPlace, onPlaceStatus, payload, placementKey, resetManualPlacement, resetPlace, setupPlace,
  update3d, views,
} from './place.js';

const L = window.L;
const STEPS = ['upload', 'crs', 'place', 'save'];
const PREPARE_LABELS = ['Reading the IFC file', 'Reading units, site and georeferencing',
  'Building the 3D preview', 'Packing the 3D model and footprint'];

/* ===== launch parameters, session and resume ============================== */

function readLaunchParams() {
  const url = new URL(window.location.href);
  const params = url.searchParams;
  const projectId = params.get('project_id');
  const token = params.get('token');
  if (projectId) session.set('georef.project', projectId);
  if (token) session.set('georef.token', token);
  const launch = {};
  const epsg = parseInt(params.get('epsg'), 10);
  if (Number.isFinite(epsg)) launch.epsg = epsg;
  const lat = parseNumber(params.get('lat'));
  const lon = parseNumber(params.get('lon'));
  if (lat != null && lon != null) launch.location = { lat, lon };
  const keys = ['project_id', 'token', 'epsg', 'lat', 'lon'];
  if (keys.some((k) => params.has(k))) {
    keys.forEach((k) => params.delete(k));
    const qs = params.toString();
    // Keep the login token out of the address bar, history and copied links.
    window.history.replaceState(null, '', url.pathname + (qs ? `?${qs}` : '') + url.hash);
    session.set('georef.launch', launch);
  }
  state.launch = Object.keys(launch).length ? launch : session.get('georef.launch', {});
  state.project = { id: session.get('georef.project'), token: session.get('georef.token') };
}

function listenForParentAuth() {
  window.addEventListener('message', (event) => {
    if (!(CONFIG.allowedOrigins || []).includes(event.origin)) return;
    const data = event.data || {};
    if (data.type !== 'ifc-georef:auth') return;
    if (data.projectId) session.set('georef.project', String(data.projectId));
    if (data.token) session.set('georef.token', String(data.token));
    state.project = { id: session.get('georef.project'), token: session.get('georef.token') };
    renderProjectBadge();
  });
}

function renderProjectBadge() {
  const badge = $('#project-badge');
  if (state.project.id) {
    badge.hidden = false;
    badge.className = 'project-badge linked';
    badge.textContent = `Project ${state.project.id}`;
    badge.title = state.project.token ? 'Opened from GeoDataHub' : 'No login token was passed to this page';
  } else {
    badge.hidden = true;
  }
  $('#project-open').hidden = !state.project.id;
}

function setJob(id) {
  state.jobId = id;
  const hash = `#job=${id}`;
  if (window.location.hash !== hash) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${hash}`);
  }
}

function saveUiState() {
  if (!state.jobId) return;
  session.set(`georef.ui.${state.jobId}`, {
    step: state.step, maxStep: state.maxStep, location: state.location,
    crsCode: state.crs ? state.crs.code : null, place: state.place, placeTouched: state.placeTouched,
    prefilledFor: state.prefilledFor, writtenKey: state.writtenKey, uploadedAs: state.uploadedAs,
  });
}

function restoreUiState(jobId) {
  const saved = session.get(`georef.ui.${jobId}`);
  if (!saved) return null;
  Object.assign(state, {
    maxStep: saved.maxStep || 0, location: saved.location || null,
    place: { ...defaultPlacement(), ...(saved.place || {}) }, placeTouched: Boolean(saved.placeTouched),
    prefilledFor: saved.prefilledFor || null, writtenKey: saved.writtenKey || null,
    uploadedAs: saved.uploadedAs || null, restored: true,
  });
  if (saved.crsCode) state.crs = { code: saved.crsCode };
  syncPointIds();
  return saved;
}

async function resume(jobId) {
  try {
    const status = await api.status(jobId);
    setJob(jobId);
    const saved = restoreUiState(jobId);
    state.upload = { progress: 1, startedAt: null, endedAt: null, file: null, active: false };
    applyStatus(status);
    startPolling();
    const wanted = saved && saved.step ? STEPS.indexOf(saved.step) : 0;
    goTo(STEPS[Math.min(wanted, state.maxStep)] || 'upload');
  } catch (e) {
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    goTo('upload');
    if (e.status === 404) toast('Your previous session has expired. Please upload the file again.');
  }
}

function restart() {
  if (state.upload.active) return;
  if (state.jobId) session.remove(`georef.ui.${state.jobId}`);
  window.location.href = window.location.pathname + window.location.search;
}

/* ===== navigation ========================================================== */

function unlock(step) {
  state.maxStep = Math.max(state.maxStep, STEPS.indexOf(step));
  renderStepper();
}

function renderStepper() {
  $$('#stepper li').forEach((li, i) => {
    li.classList.toggle('active', STEPS[i] === state.step);
    li.classList.toggle('done', i <= state.maxStep && STEPS[i] !== state.step);
  });
}

function goTo(step) {
  const index = STEPS.indexOf(step);
  if (index < 0 || index > state.maxStep) return;
  cancelPicking();
  state.step = step;
  STEPS.forEach((s) => { $(`#step-${s}`).hidden = s !== step; });
  $('#filebar').hidden = step === 'upload' || !state.meta;
  // Placement is a one-screen workspace: controls and views always visible together.
  document.body.classList.toggle('workspace', step === 'place');
  renderStepper();
  if (step === 'upload') renderUploadProgress();
  if (step === 'crs') enterCrs();
  if (step === 'place') enterPlace();
  if (step === 'save') enterSave();
  saveUiState();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ===== polling ===================================================================== */

let pollTimer = null;
let polling = false;
let pollEpoch = 0;

/** Called after every action that starts a task (upload, save, project upload...). */
function startPolling() {
  pollEpoch += 1; // a status requested before this action is stale: it must not stop the polling
  if (!pollTimer) pollTimer = setInterval(poll, 700);
  poll();
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

async function poll() {
  if (polling || !state.jobId) return;
  polling = true;
  const epoch = pollEpoch;
  try {
    const status = await api.status(state.jobId);
    if (epoch !== pollEpoch) return; // the next tick polls again with the current state
    applyStatus(status);
    const tasks = status.tasks || {};
    const busy = Object.values(tasks).some((t) => t.state === 'queued' || t.state === 'running')
      || (!status.meta && !tasks.prepare && !Object.values(tasks).some((t) => t.state === 'error'));
    if (!busy) stopPolling();
  } catch (e) {
    if (e.status === 404) {
      stopPolling();
      toast('This session has expired. Please upload the file again.', 'error', 8000);
    } else if (!(e instanceof ApiError)) {
      console.error(e); // a rendering bug, not a network problem: do not hide it
    }
  } finally {
    polling = false;
  }
}

function applyStatus(status) {
  const hadMeta = Boolean(state.meta);
  state.status = status;
  if (status.meta) state.meta = status.meta;
  if (status.geometry) state.geometry = status.geometry;
  if (state.step === 'upload') renderUploadProgress();
  const prepare = status.tasks && status.tasks.prepare;
  if (prepare && prepare.state === 'error' && !state.meta) showError('#upload-error', prepare.error);
  if (!hadMeta && state.meta) onMetaReady();
  if (state.step === 'place') onPlaceStatus();
  if (state.step === 'save') {
    renderSaveTask();
    renderProjectTask();
  }
}

/* ===== step 1: upload ============================================================== */

function setupUpload() {
  $('#max-upload').textContent = CONFIG.maxUploadMb;
  $('#project-open-btn').addEventListener('click', openFromProject);
  const zone = $('#dropzone');
  const input = $('#file-input');
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) startUpload(input.files[0]);
    input.value = '';
  });
  ['dragenter', 'dragover'].forEach((type) => zone.addEventListener(type, (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach((type) => zone.addEventListener(type, (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
  }));
  zone.addEventListener('drop', (e) => {
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (file) startUpload(file);
  });
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      input.click();
    }
  });
  window.addEventListener('beforeunload', (e) => {
    if (state.upload.active) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

async function startUpload(file) {
  if (state.upload.active) return;
  hideError('#upload-error');
  const name = file.name.toLowerCase();
  if (!name.endsWith('.ifc') && !name.endsWith('.ifczip')) {
    showError('#upload-error', 'Please choose an .ifc or .ifczip file.');
    return;
  }
  if (file.size > CONFIG.maxUploadMb * 1024 * 1024) {
    showError('#upload-error', `The file is ${bytes(file.size)}; the limit is ${CONFIG.maxUploadMb} MB.`);
    return;
  }
  resetForNewFile();
  state.upload = { progress: 0, startedAt: Date.now(), endedAt: null, file, active: true };
  $('#upload-summary').hidden = true;
  $('#dropzone').classList.add('compact');
  renderStepper();
  renderUploadProgress();
  try {
    const result = await api.upload(file, (fraction) => {
      state.upload.progress = fraction;
      renderUploadProgress();
    });
    state.upload.active = false;
    state.upload.progress = 1;
    state.upload.endedAt = Date.now();
    setJob(result.job_id);
    applyStatus(result);
    startPolling();
  } catch (e) {
    state.upload.active = false;
    $('#upload-progress').hidden = true;
    showError('#upload-error', e.message);
  }
}

/** R7c: open an IFC file stored in the GeoDataHub project; the server fetches it. */
async function openFromProject() {
  if (state.upload.active) return;
  hideError('#upload-error');
  const button = $('#project-open-btn');
  button.disabled = true;
  let files;
  try {
    files = (await api.projectFiles(state.project.id, state.project.token)).files || [];
  } catch (e) {
    showError('#upload-error', e.message);
    return;
  } finally {
    button.disabled = false;
  }
  const choice = await pickFromList({
    title: `Open from project ${state.project.id}`,
    message: 'IFC files stored in the GeoDataHub project. The file is copied to this tool; the original is not changed.',
    items: files.map((f) => ({
      label: f.filename,
      detail: [bytes(f.size), f.last_modified ? new Date(f.last_modified).toLocaleString() : ''].filter(Boolean).join(' · '),
      value: f,
    })),
    empty: 'This project has no IFC files yet.',
  });
  if (!choice) return;
  resetForNewFile();
  state.upload = { progress: 1, startedAt: Date.now(), endedAt: Date.now(), file: null, active: false };
  $('#upload-summary').hidden = true;
  $('#dropzone').classList.add('compact');
  renderStepper();
  try {
    const result = await api.openFromProject(state.project.id, choice.s3_key, state.project.token);
    setJob(result.job_id);
    applyStatus(result);
    startPolling();
  } catch (e) {
    $('#upload-progress').hidden = true;
    showError('#upload-error', e.message);
  }
}

/** A new file on the same page starts a fresh session (the project link is kept). */
function resetForNewFile() {
  if (state.jobId) session.remove(`georef.ui.${state.jobId}`);
  window.history.replaceState(null, '', window.location.pathname + window.location.search);
  stopPolling();
  resetPlace();
  if (locMarker) {
    locMarker.remove();
    locMarker = null;
  }
  ['#done-panel', '#save-progress', '#project-progress', '#project-result', '#save-error']
    .forEach((selector) => { $(selector).hidden = true; });
  $('#save-actions').hidden = false;
  Object.assign(state, {
    jobId: null, status: null, meta: null, geometry: null, maxStep: 0, location: null, suggestions: [],
    extras: [], crs: null, place: defaultPlacement(), placeTouched: false, prefilledFor: null, result: null,
    restored: false, writtenKey: null, uploadedAs: null, allowOutside: false, mapFitted: false,
    saveStartedAt: null, uploadStartedAt: null,
  });
  syncPointIds();
}

function renderUploadProgress() {
  const panel = $('#upload-progress');
  const up = state.upload;
  const tasks = state.status && state.status.tasks ? state.status.tasks : {};
  const prepare = tasks.prepare || null;
  const fetchTask = tasks.fetch || null; // file opened from the GeoDataHub project
  if (!up.file && !prepare && !fetchTask) {
    panel.hidden = true;
    return;
  }
  let first;
  let firstProgress;
  if (fetchTask) {
    first = { ...fetchTask.steps[0], label: 'Downloading the file from the project' };
    firstProgress = fetchTask.state === 'done' ? 1 : fetchTask.progress;
  } else {
    first = {
      label: up.file ? `Uploading ${up.file.name}` : 'Uploading the file',
      state: up.progress >= 1 || !up.file ? 'done' : 'running',
      progress: up.progress,
      detail: up.file && up.progress < 1 ? `${bytes(up.file.size * up.progress)} of ${bytes(up.file.size)}` : null,
      started_at: up.startedAt ? up.startedAt / 1000 : null,
      ended_at: up.endedAt ? up.endedAt / 1000 : null,
    };
    firstProgress = up.progress;
  }
  const steps = [first].concat(prepare ? prepare.steps : PREPARE_LABELS.map((label) => ({ label, state: 'pending' })));
  const progress = combine([
    { weight: up.file || fetchTask ? 15 : 0, progress: firstProgress },
    { weight: 85, progress: prepare ? prepare.progress : 0 },
  ]);
  const startedAt = up.startedAt || (fetchTask && fetchTask.created_at ? fetchTask.created_at * 1000 : null)
    || (prepare && prepare.created_at ? prepare.created_at * 1000 : null);
  const failed = [fetchTask, prepare].find((t) => t && t.state === 'error');
  renderProgress(panel, {
    title: 'Preparing your model',
    progress,
    eta: eta(startedAt, progress),
    steps,
    error: failed ? failed.error : null,
    waiting: prepare && prepare.state === 'queued' ? 'Waiting for a free worker…' : null,
  });
}

function georefText(g) {
  if (!g || !g.present) return 'Not georeferenced yet';
  const crs = g.epsg ? `EPSG:${g.epsg}` : (g.crs_name || 'unknown system');
  return `Already georeferenced in ${crs} (E ${fixed(g.eastings, 2)}, N ${fixed(g.northings, 2)})`;
}

function onMetaReady() {
  const m = state.meta;
  renderFileBar();
  renderUploadSummary();
  if (!m.supported) {
    showError('#upload-error', `This file cannot be georeferenced. ${(m.warnings || []).join(' ')}`);
    return;
  }
  if (!state.location) {
    const g = m.georef || {};
    if (g.present && g.latitude != null) state.location = { lat: g.latitude, lon: g.longitude, source: 'georef' };
    else if (siteHasLocation()) state.location = { lat: site().latitude, lon: site().longitude, source: 'ifc' };
    else if (state.launch.location) state.location = { ...state.launch.location, source: 'link' };
  }
  if (!state.restored) state.place.useSite = siteHasLocation();
  unlock('crs');
  if (state.step === 'upload' && !state.restored) {
    setTimeout(() => { if (state.step === 'upload') goTo('crs'); }, 1200);
  }
}

function renderUploadSummary() {
  const m = state.meta;
  const s = m.site || {};
  const location = s.has_location
    ? `${fixed(s.latitude, 6)}°, ${fixed(s.longitude, 6)}°${s.elevation_m != null ? ` · elevation ${fixed(s.elevation_m, 2)} m` : ''}`
    : 'Not stored in the file. You can set it in the next step.';
  const app = m.header && m.header.application ? ` · from ${escapeHtml(m.header.application)}` : '';
  const warnings = (m.warnings || []).length
    ? `<div class="alert warn"><ul>${m.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul></div>` : '';
  const box = $('#upload-summary');
  box.hidden = false;
  box.innerHTML = `
    <div class="summary">
      <dl>
        <dt>File</dt><dd>${escapeHtml(m.file ? m.file.name : '')} (${bytes(m.file ? m.file.size : 0)})</dd>
        <dt>Format</dt><dd>${escapeHtml(m.schema)}${app}</dd>
        <dt>Units</dt><dd>${escapeHtml(m.unit.name.toLowerCase())} (1 ${escapeHtml(m.unit.symbol)} = ${m.unit.metres} m)</dd>
        <dt>Content</dt><dd>${grouped(m.element_count)} elements · ${m.storeys.length} storeys</dd>
        <dt>Location</dt><dd>${location}</dd>
        <dt>Georeferencing</dt><dd>${escapeHtml(georefText(m.georef))}</dd>
      </dl>
      ${warnings}
      ${m.supported ? '<div class="actions"><button class="btn primary" type="button" data-go="crs">Continue</button></div>' : ''}
    </div>`;
}

function renderFileBar() {
  const m = state.meta;
  $('#filebar-name').textContent = m.file ? m.file.name : 'model.ifc';
  const output = state.status && state.status.output;
  let georefChip;
  if (output && output.crs) georefChip = `<span class="chip ok">Georeferenced copy ready · ${escapeHtml(output.crs.id)}</span>`;
  else if (m.georef && m.georef.present) georefChip = `<span class="chip ok">Georeferenced${m.georef.epsg ? ` · EPSG:${m.georef.epsg}` : ''}</span>`;
  else georefChip = '<span class="chip warn">Not georeferenced</span>';
  const chips = [
    `<span class="chip">${escapeHtml(m.schema)}</span>`,
    `<span class="chip">Units: ${escapeHtml(m.unit.symbol)}</span>`,
    `<span class="chip">${grouped(m.element_count)} elements</span>`,
    georefChip,
  ];
  $('#filebar-chips').innerHTML = chips.join('');
}

/* ===== step 2: coordinate system ============================================================ */

let locMap = null;
let locMarker = null;
let suggestSeq = 0;
const crsCache = new Map();

function setupCrs() {
  $('#crs-next').addEventListener('click', async () => {
    if (!state.crs || !state.crs.projected) return;
    unlock('place');
    goTo('place');
  });

  $('#place-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const query = $('#place-search').value.trim();
    const list = $('#place-results');
    if (!query) return;
    list.hidden = false;
    list.innerHTML = '<li>Searching…</li>';
    try {
      const url = `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=6&q=${encodeURIComponent(query)}`;
      const response = await fetch(url, { headers: { 'Accept-Language': navigator.language || 'en' } });
      const places = await response.json();
      if (!places.length) {
        list.innerHTML = '<li>No places found. Try a different spelling, or click the map.</li>';
        return;
      }
      list.innerHTML = places.map((p, i) => `<li data-i="${i}">${escapeHtml(p.display_name)}</li>`).join('');
      $$('li', list).forEach((li) => li.addEventListener('click', () => {
        const place = places[Number(li.dataset.i)];
        list.hidden = true;
        setLocation(Number(place.lat), Number(place.lon), 'search');
        locMap.map.setView([Number(place.lat), Number(place.lon)], 17);
      }));
    } catch (err) {
      list.innerHTML = '<li>Address search is not available right now. Click the map instead.</li>';
    }
  });

  const input = $('#crs-search');
  const results = $('#crs-results');
  let items = [];
  let active = -1;
  const render = () => {
    results.hidden = !items.length;
    results.innerHTML = items.map((r, i) => {
      const cover = r.covers_location === true ? ' · covers the building' : r.covers_location === false ? ' · outside the building location' : '';
      return `<li data-i="${i}" class="${i === active ? 'active' : ''}">EPSG:${r.code} · ${escapeHtml(r.name)}
        <small>${escapeHtml(r.area ? r.area.name : '')} · ${escapeHtml(r.unit || '')}${cover}</small></li>`;
    }).join('');
    $$('li', results).forEach((li) => li.addEventListener('mousedown', (e) => {
      e.preventDefault();
      choose(items[Number(li.dataset.i)]);
    }));
  };
  const choose = (item) => {
    if (!item) return;
    input.value = '';
    items = [];
    render();
    crsCache.set(cacheKey(item.code), item);
    if (!state.suggestions.some((s) => s.code === item.code) && !state.extras.some((s) => s.code === item.code)) {
      state.extras.unshift({ ...item, badge: 'Search', reason: 'Chosen from the search' });
    }
    selectCrs(item.code, true);
  };
  const search = debounce(async () => {
    const text = input.value.trim();
    if (text.length < 2) {
      items = [];
      render();
      return;
    }
    try {
      const loc = state.location || {};
      const res = await api.searchCrs(text, loc.lat, loc.lon);
      items = res.results.filter((r) => r.projected);
      active = items.length ? 0 : -1;
      if (!items.length) {
        results.hidden = false;
        results.innerHTML = '<li>No projected coordinate system matches.</li>';
        return;
      }
      render();
    } catch (e) {
      toast(e.message, 'error');
    }
  }, 250);
  input.addEventListener('input', search);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = Math.max(0, Math.min(items.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
      render();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      choose(items[active]);
    } else if (e.key === 'Escape') {
      items = [];
      render();
    }
  });
  input.addEventListener('blur', () => setTimeout(() => { results.hidden = true; }, 150));
}

function cacheKey(code) {
  const loc = state.location;
  return loc ? `${code}@${loc.lat.toFixed(4)},${loc.lon.toFixed(4)}` : `${code}`;
}

async function crsInfo(code) {
  const key = cacheKey(code);
  if (crsCache.has(key)) return crsCache.get(key);
  try {
    const loc = state.location || {};
    const info = await api.crs(code, loc.lat, loc.lon);
    crsCache.set(key, info);
    return info;
  } catch (e) {
    return null;
  }
}

function enterCrs() {
  if (!locMap) {
    locMap = createMap($('#loc-map'), { basemap: 'osm' });
    locMap.map.on('click', (e) => setLocation(e.latlng.lat, e.latlng.lng, 'map'));
  }
  $('#crs-intro').textContent = 'Pick the map grid the coordinates should be in. The best match for the building location is selected for you.';
  renderLocation(true);
  loadSuggestions();
}

function setLocation(lat, lon, source) {
  state.location = { lat, lon, source };
  renderLocation(false);
  loadSuggestions();
  saveUiState();
}

function renderLocation(fit) {
  const loc = state.location;
  const text = $('#loc-source');
  if (!loc) {
    text.textContent = 'The file does not say where the building is. Search for the address or click the map.';
    if (locMarker) {
      locMarker.remove();
      locMarker = null;
    }
    return;
  }
  const sources = {
    ifc: 'Read from the IFC file (IfcSite)', georef: 'From the georeferencing in the IFC file',
    link: 'Passed in by GeoDataHub',
    map: 'Set on the map', search: 'From the address search',
  };
  text.innerHTML = `${sources[loc.source] || 'Location'}: <b>${fixed(loc.lat, 6)}°, ${fixed(loc.lon, 6)}°</b>`;
  if (!locMarker) {
    locMarker = L.marker([loc.lat, loc.lon], { draggable: true, icon: pinIcon('', 'loc') }).addTo(locMap.map);
    locMarker.on('dragend', () => {
      const p = locMarker.getLatLng();
      setLocation(p.lat, p.lng, 'map');
    });
  } else {
    locMarker.setLatLng([loc.lat, loc.lon]);
  }
  if (fit) locMap.map.setView([loc.lat, loc.lon], 16);
}

async function loadSuggestions() {
  const seq = ++suggestSeq;
  const box = $('#crs-suggestions');
  if (state.location) {
    box.innerHTML = '<p class="muted">Finding coordinate systems for this location…</p>';
    try {
      const res = await api.suggest(state.location.lat, state.location.lon);
      if (seq !== suggestSeq) return;
      state.suggestions = res.suggestions;
    } catch (e) {
      state.suggestions = [];
      toast(e.message, 'error');
    }
  } else {
    state.suggestions = [];
  }
  await addExtras();
  if (seq !== suggestSeq) return;
  renderSuggestions();
  await preselectCrs();
}

async function addExtras() {
  const georef = state.meta && state.meta.georef;
  const wanted = [];
  if (georef && georef.present && georef.epsg) wanted.push([georef.epsg, 'In the file', 'Used by the georeferencing already in the file']);
  if (state.launch.epsg) wanted.push([state.launch.epsg, 'Project', 'Coordinate system of the GeoDataHub project']);
  if (!state.suggestions.length) wanted.push([CONFIG.defaultEpsg, 'Default', 'Default coordinate system of this service']);
  const extras = state.extras.filter((e) => e.badge === 'Search');
  for (const [code, badge, reason] of wanted) {
    if (extras.some((e) => e.code === code)) continue;
    const info = await crsInfo(code);
    if (info && info.projected) extras.push({ ...info, badge, reason });
  }
  state.extras = extras;
}

function crsChoices() {
  const seen = new Set();
  const list = [];
  for (const item of state.extras.concat(state.suggestions)) {
    if (seen.has(item.code)) continue;
    seen.add(item.code);
    list.push(item);
  }
  return list;
}

function renderSuggestions() {
  const box = $('#crs-suggestions');
  const choices = crsChoices();
  if (!choices.length) {
    box.innerHTML = '<p class="muted">Set the building location to get suggestions, or search below.</p>';
    return;
  }
  box.innerHTML = choices.map((c) => {
    const badges = [];
    if (c.badge) badges.push(`<span class="badge ${c.badge === 'In the file' ? 'file' : 'def'}">${escapeHtml(c.badge)}</span>`);
    if (c.recommended) badges.push('<span class="badge rec">Recommended</span>');
    const selected = state.crs && state.crs.code === c.code;
    return `<button type="button" class="choice${selected ? ' selected' : ''}" data-code="${c.code}">
      <span class="radio"></span>
      <span class="title">${escapeHtml(c.name)} <span class="muted">EPSG:${c.code}</span>${badges.join('')}</span>
      <span class="meta">${escapeHtml(c.reason || '')}${c.area ? ` · ${escapeHtml(c.area.name)}` : ''} · ${escapeHtml(c.unit || '')}</span>
    </button>`;
  }).join('');
  $$('.choice', box).forEach((b) => b.addEventListener('click', () => selectCrs(Number(b.dataset.code), true)));
}

async function preselectCrs() {
  if (state.crs && state.crs.code) {
    await selectCrs(state.crs.code, false);
    return;
  }
  const georef = state.meta && state.meta.georef;
  const recommended = state.suggestions.find((s) => s.recommended) || state.suggestions[0];
  const order = [state.launch.epsg, georef && georef.present ? georef.epsg : null,
    recommended ? recommended.code : null, CONFIG.defaultEpsg];
  for (const code of order) {
    if (!code) continue;
    const info = await crsInfo(code);
    if (info && info.projected && info.covers_location !== false) {
      await selectCrs(code, false);
      return;
    }
  }
  if (recommended) await selectCrs(recommended.code, false);
}

async function convertTargets(fromCrs, toCrs) {
  const points = state.place.points.filter((p) => p.target && p.target[0] != null && p.target[1] != null);
  if (!points.length || !fromCrs || !fromCrs.unit_metres) return;
  try {
    const wgs = await api.toWgs84(fromCrs.code, points.map((p) => ({ e: p.target[0], n: p.target[1] })));
    const grid = await api.toGrid(toCrs.code, wgs.points.map((p) => p || { lat: 0, lon: 0 }));
    points.forEach((p, i) => {
      const g = grid.points[i];
      if (!wgs.points[i] || !g) return;
      const h = p.target[2] == null ? null : p.target[2] * fromCrs.unit_metres / (toCrs.unit_metres || 1);
      p.target = [g.e, g.n, h];
    });
    toast(`Map coordinates of ${points.length} point(s) were converted to EPSG:${toCrs.code}.`);
  } catch (e) {
    toast(`Could not convert existing points: ${e.message}`, 'error');
  }
}

async function selectCrs(code, byUser) {
  const info = await crsInfo(code);
  if (!info) {
    toast(`EPSG:${code} could not be loaded.`, 'error');
    return;
  }
  const previous = state.crs && state.crs.name ? state.crs : null;
  const changed = !previous || previous.code !== info.code;
  if (changed && previous && byUser) await convertTargets(previous, info);
  state.crs = info;
  if (changed) {
    state.mapFitted = false;
    state.allowOutside = false;
    if (previous) resetManualPlacement();
  }
  renderSuggestions();
  renderSelectedCrs();
  $('#crs-next').disabled = !info.projected;
  saveUiState();
}

function renderSelectedCrs() {
  const c = state.crs;
  const box = $('#crs-selected');
  if (!c || !c.name) {
    box.innerHTML = '';
    return;
  }
  let check = '';
  let cls = 'info';
  if (!c.projected) {
    check = 'This system uses latitude/longitude. Choose a projected (map grid) system.';
    cls = 'error';
  } else if (c.covers_location === true) {
    check = '✓ Covers the building location.';
    cls = 'success';
  } else if (c.covers_location === false) {
    check = '⚠ The building lies outside the area of this system. Results would be distorted. Choose another one.';
    cls = 'warn';
  } else {
    check = 'Set the building location to check that this system covers it.';
  }
  box.innerHTML = `<div class="alert ${cls}"><b>EPSG:${c.code} · ${escapeHtml(c.name)}</b><br>
    Units: ${escapeHtml(c.unit || '')}${c.area ? ` · Area: ${escapeHtml(c.area.name)}` : ''}<br>${check}</div>`;
}

/* ===== step 4: save ==================================================================== */

let saveMap = null;
let saveLayer = null;

function setupSave() {
  $('#save-btn').addEventListener('click', save);
  $('#project-upload-btn').addEventListener('click', uploadToProject);
  $('#restart-btn').addEventListener('click', restart);
  $('#filebar-new').addEventListener('click', restart);
}

function enterSave() {
  const r = state.result;
  if (!r) {
    goTo('place');
    return;
  }
  const s = r.solution;
  const unit = crsUnitSymbol();
  const n = s.residuals ? s.residuals.length : 0;
  let points;
  if (!n) points = 'None (placed by hand, no accuracy figures)';
  else points = `${n}${n > 1 ? ` · accuracy ${fixed(s.rms_horizontal_m, 3)} m` : ''}`;
  $('#save-summary').innerHTML = `
    <dt>File</dt><dd>${escapeHtml(state.meta.file ? state.meta.file.name : '')}</dd>
    <dt>Coordinate system</dt><dd>EPSG:${state.crs.code} · ${escapeHtml(state.crs.name)}</dd>
    <dt>Map unit</dt><dd>${escapeHtml(state.crs.unit || 'metre')}</dd>
    <dt>Easting / Northing</dt><dd>${fixed(s.eastings, 3)} / ${fixed(s.northings, 3)} ${unit}</dd>
    <dt>Height</dt><dd>${fixed(s.orthogonal_height, 3)} ${unit}</dd>
    <dt>Vertical datum</dt><dd>No vertical datum written</dd>
    <dt>Rotation</dt><dd>${fixed(s.rotation_cw_deg, 2)}° clockwise <small class="muted">(${fixed(s.rotation_deg, 4)}° anticlockwise in the file)</small></dd>
    <dt>Scale</dt><dd>${Number(s.scale.toPrecision(10))} <small class="muted">${escapeHtml(s.scale_label || '')}</small></dd>
    <dt>Control points</dt><dd>${points}</dd>`;
  if (!saveMap) {
    const loc = state.location;
    saveMap = createMap($('#save-map'), { basemap: defaultBasemap(loc && loc.lat, loc && loc.lon) });
    saveLayer = L.layerGroup().addTo(saveMap.map);
  }
  saveLayer.clearLayers();
  const p = r.preview;
  if (p.footprint && p.footprint.length > 2) {
    L.polygon(p.footprint, { color: '#93a70a', weight: 2, fillColor: '#b0c70d', fillOpacity: state.fillOpacity }).addTo(saveLayer);
  }
  if (p.origin) L.circleMarker(p.origin, { radius: 5, color: '#111827', fillColor: '#fff', fillOpacity: 1 }).addTo(saveLayer);
  setTimeout(() => {
    saveMap.map.invalidateSize();
    if (p.bounds) saveMap.map.fitBounds(p.bounds, { padding: [30, 30], maxZoom: 19 });
  }, 50);
  if (views.save.map3d) views.save.map3d.framed = false;
  update3d('save');
  const isCurrent = state.writtenKey && state.writtenKey === placementKey();
  if (!isCurrent) {
    $('#done-panel').hidden = true;
    $('#save-actions').hidden = false;
    $('#save-progress').hidden = true;
    $('#save-btn').disabled = false;
  }
  hideError('#save-error');
  renderSaveTask();
  renderProjectTask();
}

async function save() {
  hideError('#save-error');
  $('#save-btn').disabled = true;
  try {
    const key = placementKey();
    const status = await api.georeference(state.jobId, payload());
    state.writtenKey = key;
    state.saveStartedAt = Date.now();
    state.uploadStartedAt = null;
    state.uploadedAs = null;
    $('#project-progress').hidden = true;
    $('#project-result').hidden = true;
    saveUiState();
    applyStatus(status);
    startPolling();
  } catch (e) {
    showError('#save-error', e.message);
    $('#save-btn').disabled = false;
  }
}

function renderSaveTask() {
  const task = state.status && state.status.tasks ? state.status.tasks.georeference : null;
  if (!task || state.step !== 'save' || state.writtenKey !== placementKey()) return;
  renderProgress($('#save-progress'), {
    title: 'Saving the georeferenced file',
    progress: task.progress,
    eta: eta(task.started_at ? task.started_at * 1000 : state.saveStartedAt, task.progress),
    steps: task.steps,
    error: task.state === 'error' ? task.error : null,
    waiting: task.state === 'queued' ? 'Waiting for a free worker…' : null,
  });
  if (task.state === 'error') $('#save-btn').disabled = false;
  if (task.state === 'done' && state.status.output) showDone();
}

function projectSource() {
  const job = state.status && state.status.job;
  return job && job.source && job.source.type === 'project' ? job.source : null;
}

function showDone() {
  const out = state.status.output;
  const wasHidden = $('#done-panel').hidden;
  $('#save-actions').hidden = true;
  $('#done-panel').hidden = false;
  renderFileBar();
  $('#done-text').innerHTML = `<b>Done.</b> ${escapeHtml(out.filename)} (${bytes(out.bytes)}) was written and checked.`;
  const link = $('#download-btn');
  link.href = downloadUrl(state.jobId);
  link.setAttribute('download', out.filename);
  const button = $('#project-upload-btn');
  const row = $('#project-save-row');
  if (state.project.id) {
    row.hidden = false;
    if (wasHidden || !$('#project-filename').value) $('#project-filename').value = state.uploadedAs || out.filename;
    button.textContent = `Save to project ${state.project.id}`;
    const source = projectSource();
    const notes = [];
    if (source) {
      notes.push(`Opened from this project. A new file is added next to the original (${source.s3_key.split('/').pop()}); `
        + 'replacing the original needs an update endpoint on the project server.');
    }
    if (!state.project.token) notes.push('No GeoDataHub login was passed to this page, so the project server may reject the upload.');
    $('#project-hint').textContent = notes.join(' ');
  } else {
    row.hidden = true;
    $('#project-hint').textContent = 'Open this tool from a GeoDataHub project to save the file there directly.';
  }
}

async function uploadToProject() {
  const filename = $('#project-filename').value.trim();
  if (!filename) {
    showProjectResult('Enter a file name.', 'error');
    return;
  }
  $('#project-upload-btn').disabled = true;
  $('#project-result').hidden = true;
  try {
    const status = await api.uploadToProject(state.jobId, state.project.id, state.project.token, filename);
    state.uploadStartedAt = Date.now();
    applyStatus(status);
    startPolling();
  } catch (e) {
    showProjectResult(e.message, 'error');
    $('#project-upload-btn').disabled = false;
  }
}

function showProjectResult(message, kind) {
  const box = $('#project-result');
  box.hidden = false;
  box.className = `alert ${kind}`;
  box.textContent = message;
}

function renderProjectTask() {
  const task = state.status && state.status.tasks ? state.status.tasks.upload : null;
  if (!task || !state.uploadStartedAt || state.step !== 'save') return;
  renderProgress($('#project-progress'), {
    title: 'Saving to the project',
    progress: task.progress,
    eta: eta(task.started_at ? task.started_at * 1000 : state.uploadStartedAt, task.progress),
    steps: task.steps,
    error: task.state === 'error' ? task.error : null,
    waiting: task.state === 'queued' ? 'Starting…' : null,
  });
  if (task.state === 'done') {
    const name = (task.result && task.result.filename) || $('#project-filename').value;
    if (state.uploadedAs !== name) {
      state.uploadedAs = name;
      saveUiState();
    }
    showProjectResult(`Uploaded to project ${state.project.id} as ${name}.`, 'success');
    $('#project-upload-btn').disabled = false;
    $('#project-upload-btn').textContent = 'Save again';
  } else if (task.state === 'error') {
    showProjectResult(task.error, 'error');
    $('#project-upload-btn').disabled = false;
    if (task.result && task.result.status === 401) {
      session.remove('georef.token');
      state.project.token = null;
    }
  }
}

/* ===== start ==================================================================================== */

function setupNavigation() {
  document.addEventListener('click', (e) => {
    const go = e.target.closest('[data-go]');
    if (go) goTo(go.dataset.go);
  });
  $$('#stepper li').forEach((li) => li.addEventListener('click', () => {
    if (li.classList.contains('done')) goTo(li.dataset.step);
  }));
}

function init() {
  readLaunchParams();
  listenForParentAuth();
  renderProjectBadge();
  setupNavigation();
  setupUpload();
  setupCrs();
  setupPlace({ goTo, unlock, save: saveUiState });
  setupSave();
  const match = /job=([0-9a-f]{32})/.exec(window.location.hash);
  if (match) resume(match[1]);
  else goTo('upload');
}

init();
