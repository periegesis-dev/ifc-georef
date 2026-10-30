import { api, downloadUrl, modelUrl } from './api.js';
import { combine, eta, renderProgress } from './progress.js';
import { $, $$, bytes, debounce, escapeHtml, fixed, grouped, parseNumber, storage, toast } from './util.js';
import { createMap, defaultBasemap, pinIcon } from './map.js';

const L = window.L;
const CONFIG = window.APP_CONFIG || {};
const STEPS = ['upload', 'crs', 'place', 'save'];
const session = storage('session');
const PREPARE_LABELS = ['Reading the IFC file', 'Reading units, site and georeferencing',
  'Building the 3D preview', 'Packing the 3D model and footprint'];
const ROTATION_STEPS = { 0.01: 0.01, 0.1: 0.1, 1: 1, 10: 5 };

const state = {
  step: 'upload',
  maxStep: 0,
  jobId: null,
  status: null,
  meta: null,
  geometry: null,
  launch: {},
  project: { id: null, token: null },
  upload: { progress: 0, startedAt: null, endedAt: null, file: null, active: false },
  location: null,
  suggestions: [],
  extras: [],
  crs: null,
  points: [],
  useSite: false,
  rotationMode: 'auto',
  rotation: null,
  heightMode: 'auto',
  height: null,
  freeScale: false,
  offset: [0, 0, 0],
  allowOutside: false,
  prefilled: false,
  restored: false,
  result: null,
  solveError: null,
  picking: null,
  writtenKey: null,
  saveStartedAt: null,
  uploadStartedAt: null,
  mapFitted: false,
  modelProgress: 0,
};
let nextPointId = 1;

/* ===== helpers ============================================================ */

function showError(selector, message) {
  const el = $(selector);
  el.textContent = message;
  el.hidden = false;
}

function hideError(selector) {
  $(selector).hidden = true;
}

function site() {
  return (state.meta && state.meta.site) || null;
}

function siteHasLocation() {
  const s = site();
  return Boolean(s && s.has_location);
}

function unitMetres() {
  return state.meta ? state.meta.unit.metres : 1;
}

function crsUnitMetres() {
  return (state.crs && state.crs.unit_metres) || 1;
}

function crsUnitSymbol() {
  const name = ((state.crs && state.crs.unit) || 'metre').toLowerCase();
  if (name.startsWith('metre') || name.startsWith('meter')) return 'm';
  if (name.includes('us survey')) return 'ftUS';
  if (name.includes('foot') || name.includes('feet')) return 'ft';
  return name;
}

function localDigits() {
  const symbol = state.meta ? state.meta.unit.symbol : 'm';
  return symbol === 'mm' ? 1 : symbol === 'cm' ? 2 : 3;
}

function complete(p) {
  return Boolean(p.local && p.target && p.local[0] != null && p.local[1] != null
    && p.target[0] != null && p.target[1] != null);
}

function nextLabel() {
  const used = new Set(state.points.map((p) => p.label));
  let n = 1;
  while (used.has(`P${n}`)) n += 1;
  return `P${n}`;
}

function newPoint(values = {}) {
  return {
    id: nextPointId++, label: values.label || nextLabel(), local: values.local || null,
    target: values.target || null, hSource: values.hSource || null, terrain: null,
  };
}

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
    crsCode: state.crs ? state.crs.code : null, points: state.points, useSite: state.useSite,
    rotationMode: state.rotationMode, rotation: state.rotation, heightMode: state.heightMode,
    height: state.height, freeScale: state.freeScale, offset: state.offset, prefilled: state.prefilled,
    writtenKey: state.writtenKey,
  });
}

function restoreUiState(jobId) {
  const saved = session.get(`georef.ui.${jobId}`);
  if (!saved) return null;
  Object.assign(state, {
    maxStep: saved.maxStep || 0, location: saved.location || null, points: saved.points || [],
    useSite: Boolean(saved.useSite), rotationMode: saved.rotationMode || 'auto', rotation: saved.rotation,
    heightMode: saved.heightMode || 'auto', height: saved.height, freeScale: Boolean(saved.freeScale),
    offset: saved.offset || [0, 0, 0], prefilled: Boolean(saved.prefilled), writtenKey: saved.writtenKey || null,
    restored: true,
  });
  if (saved.crsCode) state.crs = { code: saved.crsCode };
  nextPointId = state.points.reduce((m, p) => Math.max(m, p.id || 0), 0) + 1;
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

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(poll, 700);
  poll();
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

async function poll() {
  if (polling || !state.jobId) return;
  polling = true;
  try {
    const status = await api.status(state.jobId);
    applyStatus(status);
    const busy = Object.values(status.tasks || {}).some((t) => t.state === 'queued' || t.state === 'running');
    if (!busy) stopPolling();
  } catch (e) {
    if (e.status === 404) {
      stopPolling();
      toast('This session has expired. Please upload the file again.', 'error', 8000);
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
  if (state.step === 'place') {
    renderViewerStatus();
    maybeLoadModel();
  }
  if (state.step === 'save') {
    renderSaveTask();
    renderProjectTask();
  }
}

/* ===== step 1: upload ============================================================== */

function setupUpload() {
  $('#max-upload').textContent = CONFIG.maxUploadMb;
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
  if (state.jobId) resetForNewFile();
  Object.assign(state, {
    jobId: null, status: null, meta: null, geometry: null, maxStep: 0, location: null, suggestions: [],
    extras: [], crs: null, points: [], result: null, prefilled: false, restored: false, writtenKey: null,
  });
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

/** A new file on the same page starts a fresh session (the project link is kept). */
function resetForNewFile() {
  session.remove(`georef.ui.${state.jobId}`);
  window.history.replaceState(null, '', window.location.pathname + window.location.search);
  stopPolling();
  modelLoaded = false;
  modelLoading = false;
  if (viewer) viewer.unload();
  if (previewLayer) {
    previewLayer.clearLayers();
    pointLayer.clearLayers();
  }
  if (locMarker) {
    locMarker.remove();
    locMarker = null;
  }
  ['#existing-note', '#done-panel', '#save-progress', '#project-progress', '#project-result', '#save-error']
    .forEach((selector) => { $(selector).hidden = true; });
  Object.values(views).forEach((view) => { if (view.map3d) view.map3d.framed = false; });
  $('#save-actions').hidden = false;
  Object.assign(state, {
    rotationMode: 'auto', rotation: null, heightMode: 'auto', height: null, freeScale: false,
    offset: [0, 0, 0], allowOutside: false, mapFitted: false, saveStartedAt: null, uploadStartedAt: null,
  });
}

function renderUploadProgress() {
  const panel = $('#upload-progress');
  const up = state.upload;
  const prepare = state.status && state.status.tasks ? state.status.tasks.prepare : null;
  if (!up.file && !prepare) {
    panel.hidden = true;
    return;
  }
  const uploadStep = {
    label: up.file ? `Uploading ${up.file.name}` : 'Uploading the file',
    state: up.progress >= 1 || !up.file ? 'done' : 'running',
    progress: up.progress,
    detail: up.file && up.progress < 1 ? `${bytes(up.file.size * up.progress)} of ${bytes(up.file.size)}` : null,
    started_at: up.startedAt ? up.startedAt / 1000 : null,
    ended_at: up.endedAt ? up.endedAt / 1000 : null,
  };
  const steps = [uploadStep].concat(prepare ? prepare.steps : PREPARE_LABELS.map((label) => ({ label, state: 'pending' })));
  const progress = combine([
    { weight: up.file ? 15 : 0, progress: up.progress },
    { weight: 85, progress: prepare ? prepare.progress : 0 },
  ]);
  const startedAt = up.startedAt || (prepare && prepare.created_at ? prepare.created_at * 1000 : null);
  renderProgress(panel, {
    title: 'Preparing your model',
    progress,
    eta: eta(startedAt, progress),
    steps,
    error: prepare && prepare.state === 'error' ? prepare.error : null,
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
    if (siteHasLocation()) state.location = { lat: site().latitude, lon: site().longitude, source: 'ifc' };
    else if (state.launch.location) state.location = { ...state.launch.location, source: 'link' };
  }
  if (!state.restored) state.useSite = siteHasLocation();
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
    ifc: 'Read from the IFC file (IfcSite)', link: 'Passed in by GeoDataHub',
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
  const points = state.points.filter((p) => p.target && p.target[0] != null && p.target[1] != null);
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

/* ===== step 3: placement ================================================================== */

let viewer = null;
let viewerError = null;
let modelLoaded = false;
let modelLoading = false;
let placeMap = null;
let previewLayer = null;
let pointLayer = null;
let solveTimer = null;
let solveSeq = 0;

function setupPlace() {
  $('#add-point').addEventListener('click', () => beginPick(null, 'model'));
  $('#pick-cancel').addEventListener('click', cancelPicking);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') cancelPicking(); });
  $('#snap-toggle').addEventListener('change', (e) => viewer && viewer.setSnap(e.target.checked));
  $('#view-top').addEventListener('click', () => viewer && viewer.topView());
  $('#view-fit').addEventListener('click', () => viewer && viewer.fit());
  $('#map-fit').addEventListener('click', () => fitPreview(true));
  $('#use-site').addEventListener('change', (e) => {
    state.useSite = e.target.checked;
    renderPoints();
    updateViewerMarkers();
    scheduleSolve(0);
  });
  $('#paste-toggle').addEventListener('click', () => {
    $('#paste-box').hidden = !$('#paste-box').hidden;
    if (!$('#paste-box').hidden) $('#paste-text').focus();
  });
  $('#paste-close').addEventListener('click', () => { $('#paste-box').hidden = true; });
  $('#paste-add').addEventListener('click', addPastedPoints);

  $('#points-body').addEventListener('change', onPointInput);
  $('#points-body').addEventListener('click', (e) => {
    const button = e.target.closest('button');
    if (!button) return;
    const index = Number(button.dataset.index);
    const point = state.points[index];
    if (button.dataset.action === 'model') beginPick(index, 'model');
    if (button.dataset.action === 'map') beginPick(index, 'map');
    if (button.dataset.action === 'edit' && point) {
      if (editing.has(point.id)) editing.delete(point.id);
      else editing.add(point.id);
      renderPoints();
    }
    if (button.dataset.action === 'delete' && point) {
      editing.delete(point.id);
      state.points.splice(index, 1);
      renderPoints();
      updateViewerMarkers();
      scheduleSolve(0);
    }
  });
  $('#type-point').addEventListener('click', () => {
    cancelPicking();
    const point = newPoint();
    state.points.push(point);
    editing.add(point.id);
    renderPoints();
    const first = $(`#points-body .pt[data-index="${state.points.length - 1}"] input`);
    if (first) first.focus();
  });

  // Typing a value switches to manual; "auto" goes back to the computed value.
  $('#rotation-auto').addEventListener('click', () => {
    state.rotationMode = 'auto';
    state.rotation = null;
    scheduleSolve(0);
  });
  $('#rotation-input').addEventListener('change', (e) => {
    const v = parseNumber(e.target.value);
    if (v == null) return;
    state.rotation = v;
    state.rotationMode = 'manual';
    scheduleSolve(0);
  });
  $('#height-auto').addEventListener('click', () => {
    state.heightMode = 'auto';
    state.height = null;
    scheduleSolve(0);
  });
  $('#height-input').addEventListener('change', (e) => {
    const v = parseNumber(e.target.value);
    if (v == null) return;
    state.height = v;
    state.heightMode = 'manual';
    scheduleSolve(0);
  });
  $('#free-scale').addEventListener('change', (e) => {
    state.freeScale = e.target.checked;
    scheduleSolve(0);
  });
  $('#allow-outside').addEventListener('change', (e) => {
    state.allowOutside = e.target.checked;
    scheduleSolve(0);
  });
  $$('[data-nudge]').forEach((b) => b.addEventListener('click', () => nudge(b.dataset.nudge)));
  $('#reset-adjust').addEventListener('click', () => {
    Object.assign(state, { offset: [0, 0, 0], rotationMode: 'auto', rotation: null, heightMode: 'auto', height: null, freeScale: false });
    scheduleSolve(0);
  });
  $('#place-next').addEventListener('click', () => {
    unlock('save');
    goTo('save');
  });
}

async function enterPlace() {
  if (!state.crs || !state.crs.name) {
    if (state.crs && state.crs.code) await selectCrs(state.crs.code, false);
    if (!state.crs || !state.crs.name) {
      goTo('crs');
      return;
    }
  }
  const loc = state.location;
  if (!placeMap) {
    placeMap = createMap($('#place-map'), {
      basemap: defaultBasemap(loc && loc.lat, loc && loc.lon), select: $('#basemap'),
      center: loc ? [loc.lat, loc.lon] : [46.8, 8.2], zoom: loc ? 18 : 7,
    });
    previewLayer = L.layerGroup().addTo(placeMap.map);
    pointLayer = L.layerGroup().addTo(placeMap.map);
    placeMap.map.on('click', onMapClick);
  }
  if (!viewer && !viewerError) {
    try {
      const mod = await import('./viewer.js');
      viewer = new mod.ModelViewer($('#viewer'), { onPick: onModelPick });
      viewer.setSnap($('#snap-toggle').checked);
    } catch (e) {
      viewerError = 'The 3D view could not be started in this browser (WebGL or network problem). '
        + 'You can still type the model coordinates in the table.';
      console.error(e);
    }
  }
  prefillPlacement();
  $('#height-unit').textContent = crsUnitSymbol();
  const s = site();
  $('#use-site').disabled = !siteHasLocation();
  $('#use-site').checked = state.useSite && siteHasLocation();
  $('#use-site-detail').textContent = siteHasLocation()
    ? `${fixed(s.latitude, 6)}°, ${fixed(s.longitude, 6)}°${s.elevation_m != null ? ` · elevation ${fixed(s.elevation_m, 2)} m` : ''}`
    : 'No location is stored in this file.';
  $('#use-site-row').classList.toggle('muted', !siteHasLocation());
  $('#free-scale').checked = state.freeScale;
  renderViewerStatus();
  maybeLoadModel();
  renderPoints();
  updateViewerMarkers();
  scheduleSolve(0);
}

function prefillPlacement() {
  if (state.prefilled) return;
  state.prefilled = true;
  const g = state.meta.georef;
  if (g && g.present && g.epsg && state.crs && g.epsg === state.crs.code && !state.points.length
      && g.eastings != null && g.northings != null) {
    state.points.push(newPoint({ label: 'Existing', local: [0, 0, 0], target: [g.eastings, g.northings, g.orthogonal_height || 0], hSource: 'file' }));
    state.rotationMode = 'manual';
    state.rotation = g.rotation_deg || 0;
    state.useSite = false;
    const note = $('#existing-note');
    note.hidden = false;
    note.textContent = `The georeferencing already stored in the file (EPSG:${g.epsg}) was loaded as the starting point. `
      + 'Adjust it if needed, or continue to save it again.';
  }
}

function renderViewerStatus() {
  const overlay = $('#viewer-overlay');
  const show = (html) => {
    overlay.hidden = false;
    overlay.innerHTML = `<div class="box">${html}</div>`;
  };
  if (viewerError) return show(escapeHtml(viewerError));
  if (state.geometry && state.geometry.empty) {
    return show('This file has no 3D geometry. Type the model coordinates of your control points in the table below.');
  }
  if (modelLoaded) {
    overlay.hidden = true;
    return null;
  }
  if (modelLoading) {
    const pct = Math.round(state.modelProgress * 100);
    return show(`<b>${state.modelProgress >= 0.99 ? 'Preparing point picking…' : 'Loading the 3D model…'}</b>
      <div class="bar" style="margin-top:10px"><span style="width:${pct}%"></span></div>`);
  }
  const prepare = state.status && state.status.tasks ? state.status.tasks.prepare : null;
  if (prepare && prepare.state === 'error') return show(`The 3D preview could not be built: ${escapeHtml(prepare.error)}`);
  if (!state.geometry) {
    const steps = prepare ? prepare.steps.filter((s) => s.key === 'geometry' || s.key === 'package') : [];
    const progress = steps.length ? combine(steps.map((s) => ({ weight: s.weight, progress: s.state === 'done' ? 1 : s.progress }))) : 0;
    return show(`<b>Building the 3D preview… ${Math.round(progress * 100)} %</b>
      <div class="bar" style="margin-top:10px"><span style="width:${Math.round(progress * 100)}%"></span></div>
      <p class="small muted" style="margin-top:8px">You can already type coordinates or use the IFC site location.</p>`);
  }
  return show('Loading…');
}

async function maybeLoadModel() {
  if (!viewer || modelLoaded || modelLoading || !state.geometry || state.geometry.empty) return;
  modelLoading = true;
  state.modelProgress = 0;
  renderViewerStatus();
  try {
    await viewer.load(modelUrl(state.jobId), state.geometry.offset_m, (f) => {
      state.modelProgress = f;
      renderViewerStatus();
    }, state.geometry.glb_bytes);
    state.modelProgress = 1;
    renderViewerStatus();
    await new Promise((resolve) => setTimeout(resolve, 40)); // let the overlay paint first
    viewer.prepareBvh();
    modelLoaded = true;
    update3d('place');
  } catch (e) {
    viewerError = `The 3D model could not be loaded: ${e.message}`;
  } finally {
    modelLoading = false;
    renderViewerStatus();
    updateViewerMarkers();
  }
}

function residualFor(label) {
  const res = state.result && state.result.solution.residuals;
  return res ? res.find((r) => r.label === label) : null;
}

function residualCell(label) {
  const r = residualFor(label);
  const n = state.result ? state.result.points.length : 0;
  if (!r || n < 2) return ['–', ''];
  const cls = r.horizontal < 0.05 ? 'good' : r.horizontal < 0.5 ? 'fair' : 'bad';
  return [fixed(r.horizontal, 3), cls];
}

const editing = new Set(); // ids of points whose coordinate editor is open

function hasLocal(p) {
  return Boolean(p.local && p.local[0] != null && p.local[1] != null);
}

function hasTarget(p) {
  return Boolean(p.target && p.target[0] != null && p.target[1] != null);
}

function pointStatus(p) {
  const model = hasLocal(p) ? '<span class="ok">model ✓</span>' : '<span class="todo">model ?</span>';
  const map = hasTarget(p) ? '<span class="ok">map ✓</span>' : '<span class="todo">map ?</span>';
  const height = p.hSource === 'terrain' ? ' · height from terrain' : '';
  return `${model} · ${map}${height}`;
}

function pointCoords(p) {
  const ld = localDigits();
  const l = p.local || [];
  const t = p.target || [];
  const model = hasLocal(p) ? `Model ${fixed(l[0], ld)}, ${fixed(l[1], ld)}, ${fixed(l[2] || 0, ld)} ${state.meta.unit.symbol}` : 'Model: not set';
  const map = hasTarget(p) ? `Map ${fixed(t[0], 3)}, ${fixed(t[1], 3)}${t[2] != null ? `, h ${fixed(t[2], 2)}` : ''} ${crsUnitSymbol()}` : 'Map: not set';
  return `${model}<br>${map}`;
}

function pointEditor(p, i) {
  const ld = localDigits();
  const local = p.local || [null, null, null];
  const target = p.target || [null, null, null];
  const input = (side, k, value, digits, placeholder, label) => `<input type="text" inputmode="decimal"
    data-index="${i}" data-side="${side}" data-k="${k}" value="${fixed(value, digits)}" placeholder="${placeholder}"
    class="${value == null ? 'empty' : ''}" aria-label="${escapeHtml(p.label)} ${label}">`;
  return `<div class="pt-edit">
    <span></span><span class="col">X</span><span class="col">Y</span><span class="col">Z</span>
    <span class="row">Model</span>${input('local', 0, local[0], ld, '', 'model X')}${input('local', 1, local[1], ld, '', 'model Y')}${input('local', 2, local[2], ld, '0', 'model Z')}
    <span></span><span class="col">Easting</span><span class="col">Northing</span><span class="col">Height</span>
    <span class="row">Map</span>${input('target', 0, target[0], 3, '', 'easting')}${input('target', 1, target[1], 3, '', 'northing')}${input('target', 2, target[2], 3, 'auto', 'height')}
  </div>`;
}

function renderPoints() {
  const items = [];
  if (state.useSite && siteHasLocation()) {
    const s = site();
    const sitePoint = state.result ? state.result.points.find((p) => p.source === 'site') : null;
    const t = sitePoint ? sitePoint.target : null;
    const [err, cls] = residualCell('IFC site');
    items.push(`<li class="pt site" data-label="IFC site">
      <div class="pt-head"><span class="pt-label">Site</span>
        <span class="pt-status">from the IFC file</span><span class="pt-err ${cls}">${err === '–' ? '' : `${err} m`}</span></div>
      ${t ? `<div class="pt-coords">Map ${fixed(t[0], 3)}, ${fixed(t[1], 3)}, h ${fixed(t[2], 2)} ${crsUnitSymbol()}</div>` : ''}
    </li>`);
  }
  state.points.forEach((p, i) => {
    const [err, cls] = residualCell(p.label);
    const active = state.picking && state.picking.index === i;
    const open = editing.has(p.id);
    items.push(`<li class="pt${active ? ' active' : ''}" data-label="${escapeHtml(p.label)}" data-index="${i}">
      <div class="pt-head">
        <span class="pt-label">${escapeHtml(p.label)}</span>
        <span class="pt-status">${pointStatus(p)}</span>
        <span class="pt-err ${cls}" title="Distance between the map point and where the model point lands">${err === '–' ? '' : `${err} m`}</span>
        <span class="pt-tools">
          <button class="icon-btn" type="button" data-action="model" data-index="${i}" title="Pick on the 3D model">◎</button>
          <button class="icon-btn" type="button" data-action="map" data-index="${i}" title="Pick on the map">⌖</button>
          <button class="icon-btn${open ? ' on' : ''}" type="button" data-action="edit" data-index="${i}" title="Edit coordinates">✎</button>
          <button class="icon-btn" type="button" data-action="delete" data-index="${i}" title="Remove">✕</button>
        </span>
      </div>
      ${open ? pointEditor(p, i) : `<div class="pt-coords">${pointCoords(p)}</div>`}
    </li>`);
  });
  $('#points-body').innerHTML = items.join('');
  // the Site card already shows the stored location; avoid saying it twice
  $('#use-site-detail').hidden = state.useSite && siteHasLocation();
  renderPointsHint();
}

function renderPointsHint() {
  const usable = state.points.filter(complete).length + (state.useSite && siteHasLocation() ? 1 : 0);
  const incomplete = state.points.filter((p) => !complete(p)).map((p) => p.label);
  let hint;
  if (!usable) {
    hint = siteHasLocation()
      ? 'Tick “Use the location stored in the IFC file”, or click “Pick a point”: a corner on the model, then the same corner on the map.'
      : 'Click “Pick a point”: a corner on the model, then the same corner on the map. Two points far apart also fix the rotation.';
  } else if (usable === 1) {
    hint = 'Placed with one point. Add a second point far from the first to fix the rotation, or turn the model with the pad.';
  } else {
    hint = `${usable} points used. The value next to each point is how far its map point is from where the model lands.`;
  }
  if (incomplete.length) hint += ` Still to complete: ${incomplete.join(', ')}.`;
  $('#points-hint').textContent = hint;
  $('#points-count').textContent = usable ? `${usable} in use` : '';
}

function updateResiduals() {
  $$('#points-body .pt').forEach((li) => {
    const cell = $('.pt-err', li);
    if (!cell) return;
    const [text, cls] = residualCell(li.dataset.label);
    cell.textContent = text === '–' ? '' : `${text} m`;
    cell.className = `pt-err ${cls}`;
  });
}

function onPointInput(e) {
  const input = e.target;
  if (!input.dataset || input.dataset.index == null) return;
  const p = state.points[Number(input.dataset.index)];
  if (!p) return;
  const k = Number(input.dataset.k);
  const value = parseNumber(input.value);
  const side = input.dataset.side;
  if (!p[side]) p[side] = [null, null, null];
  p[side][k] = value;
  if (side === 'target' && k === 2) p.hSource = value == null ? null : 'user';
  if (side === 'local' && k === 2 && p.hSource === 'terrain' && p.terrain != null) {
    p.target[2] = (p.terrain + (value || 0) * unitMetres()) / crsUnitMetres();
  }
  input.classList.toggle('empty', value == null);
  if (value != null) input.value = fixed(value, side === 'local' ? localDigits() : 3);
  // No list re-render here: focus may already be in the next field.
  const status = input.closest('.pt') && $('.pt-status', input.closest('.pt'));
  if (status) status.innerHTML = pointStatus(p);
  renderPointsHint();
  updateViewerMarkers();
  scheduleSolve(0);
}

function addPastedPoints() {
  const lines = $('#paste-text').value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  let added = 0;
  const skipped = [];
  for (const line of lines) {
    const sep = /[\t;]/.test(line) ? /[\t;]+/ : /\s+/.test(line) ? /\s+/ : /,/;
    const values = line.split(sep).map(parseNumber).filter((v) => v != null);
    let local;
    let target;
    if (values.length === 4) {
      local = [values[0], values[1], 0];
      target = [values[2], values[3], null];
    } else if (values.length === 5) {
      local = values.slice(0, 3);
      target = [values[3], values[4], null];
    } else if (values.length >= 6) {
      local = values.slice(0, 3);
      target = values.slice(3, 6);
    } else {
      skipped.push(line);
      continue;
    }
    state.points.push(newPoint({ local, target, hSource: target[2] == null ? null : 'user' }));
    added += 1;
  }
  if (added) {
    $('#paste-text').value = skipped.join('\n');
    renderPoints();
    updateViewerMarkers();
    scheduleSolve(0);
  }
  toast(skipped.length ? `${added} point(s) added, ${skipped.length} line(s) not understood.` : `${added} point(s) added.`,
    skipped.length ? 'error' : 'success');
}

function beginPick(index, stage) {
  if (!state.crs) return;
  let pickStage = stage;
  if (pickStage === 'model' && !modelLoaded) {
    toast('The 3D model is not available yet. Pick the map point now and type the model coordinates in the table.');
    pickStage = 'map';
  }
  let pickIndex = index;
  let isNew = false;
  if (pickIndex == null) {
    state.points.push(newPoint());
    pickIndex = state.points.length - 1;
    isNew = true;
  }
  state.picking = { index: pickIndex, stage: pickStage, isNew };
  applyPickingMode();
  renderPoints();
}

function applyPickingMode() {
  const pk = state.picking;
  if (pk && pk.stage === 'map' && views.place.mode === '3d') setView('place', '2d'); // map points are picked in 2D
  if (viewer) viewer.setPicking(Boolean(pk && pk.stage === 'model'));
  $('#place-map').classList.toggle('picking', Boolean(pk && pk.stage === 'map'));
  $('#pick-banner').hidden = !pk;
  if (!pk) return;
  const label = escapeHtml(state.points[pk.index].label);
  $('#pick-text').innerHTML = pk.stage === 'model'
    ? `<b>${label}</b> &middot; Click the point on the <b>3D model</b>. Corners (green) and edges (orange) snap automatically.`
    : `<b>${label}</b> &middot; Now click the <b>same point on the map</b>. Zoom in close for precision.`;
}

function finishPicking() {
  state.picking = null;
  applyPickingMode();
}

function cancelPicking() {
  const pk = state.picking;
  if (!pk) return;
  const p = state.points[pk.index];
  if (pk.isNew && p && !p.local && !p.target) state.points.splice(pk.index, 1);
  state.picking = null;
  applyPickingMode();
  if (state.step === 'place') {
    renderPoints();
    updateViewerMarkers();
  }
}

function onModelPick(hit) {
  const pk = state.picking;
  if (!pk || pk.stage !== 'model') return;
  const p = state.points[pk.index];
  p.local = hit.world.map((v) => v / unitMetres());
  if (p.hSource === 'terrain' && p.terrain != null) p.target[2] = (p.terrain + hit.world[2]) / crsUnitMetres();
  if (!p.target || p.target[0] == null) {
    state.picking = { ...pk, stage: 'map' };
    applyPickingMode();
  } else {
    finishPicking();
  }
  renderPoints();
  updateViewerMarkers();
  scheduleSolve(0);
}

async function setTargetFromLatLng(p, latlng) {
  const res = await api.toGrid(state.crs.code, [{ lat: latlng.lat, lon: latlng.lng }]);
  const g = res.points[0];
  if (!g) throw new Error('This location cannot be expressed in the selected coordinate system.');
  const keepHeight = p.target && p.target[2] != null && (p.hSource === 'user' || p.hSource === 'file');
  p.target = [g.e, g.n, keepHeight ? p.target[2] : null];
  if (!keepHeight) {
    p.hSource = null;
    fillHeight(p, latlng.lat, latlng.lng);
  }
}

async function onMapClick(e) {
  const pk = state.picking;
  if (!pk || pk.stage !== 'map') return;
  const p = state.points[pk.index];
  try {
    await setTargetFromLatLng(p, e.latlng);
    if (!p.local) {
      state.picking = { ...pk, stage: 'model' };
      if (!modelLoaded) finishPicking();
      applyPickingMode();
    } else {
      finishPicking();
    }
    renderPoints();
    scheduleSolve(0);
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function fillHeight(p, lat, lon) {
  if (!CONFIG.elevationLookup) return;
  try {
    const res = await api.elevation(lat, lon);
    if (res.height_m == null || p.hSource === 'user' || !p.target) return;
    p.terrain = res.height_m;
    const zMetres = p.local && p.local[2] != null ? p.local[2] * unitMetres() : 0;
    // Assumes the model's ±0.00 level sits on the terrain at this point.
    p.target[2] = (res.height_m + zMetres) / crsUnitMetres();
    p.hSource = 'terrain';
    if (state.step === 'place') {
      renderPoints();
      scheduleSolve(0);
    }
    toast(`Ground height ${fixed(res.height_m, 2)} m filled in (${res.source}). Edit it if the model's ±0.00 is not at ground level.`);
  } catch (e) {
    /* height stays empty; the user can type it */
  }
}

function nudge(direction) {
  if (!state.result) return;
  const step = Number($('#nudge-step').value);
  const metres = step / crsUnitMetres();
  if (direction === 'n') state.offset[1] += metres;
  if (direction === 's') state.offset[1] -= metres;
  if (direction === 'e') state.offset[0] += metres;
  if (direction === 'w') state.offset[0] -= metres;
  if (direction === 'cw' || direction === 'ccw') {
    const delta = ROTATION_STEPS[step] || step;
    state.rotation = state.result.solution.rotation_deg + (direction === 'ccw' ? delta : -delta);
    state.rotationMode = 'manual';
  }
  scheduleSolve(0);
}

function payload() {
  return {
    epsg: state.crs.code,
    use_site: Boolean(state.useSite && siteHasLocation()),
    points: state.points.filter(complete).map((p) => ({
      label: p.label,
      local: [p.local[0], p.local[1], p.local[2] == null ? 0 : p.local[2]],
      target: [p.target[0], p.target[1], p.target[2] == null ? null : p.target[2]],
    })),
    location: state.location ? { lat: state.location.lat, lon: state.location.lon } : null,
    scale_mode: state.freeScale ? 'free' : 'fixed',
    rotation_deg: state.rotationMode === 'manual' ? state.rotation : null,
    height: state.heightMode === 'manual' ? state.height : null,
    offset: state.offset,
    allow_outside: state.allowOutside,
  };
}

function scheduleSolve(delay = 250) {
  clearTimeout(solveTimer);
  solveTimer = setTimeout(runSolve, delay);
}

async function runSolve() {
  if (!state.crs || !state.meta || state.step !== 'place') return;
  const seq = ++solveSeq;
  const hasInput = (state.useSite && siteHasLocation()) || state.points.some(complete);
  if (!hasInput) {
    state.result = null;
    state.solveError = null;
  } else {
    try {
      const result = await api.solve(state.jobId, payload());
      if (seq !== solveSeq) return;
      state.result = result;
      state.solveError = null;
    } catch (e) {
      if (seq !== solveSeq) return;
      state.result = null;
      state.solveError = e.message;
    }
  }
  renderSolution();
  renderPreview();
  if ($('#points-body').contains(document.activeElement)) updateResiduals();
  else renderPoints();
  saveUiState();
}

function sourceText(kind, value) {
  const texts = {
    points: 'from points', true_north: 'from file north', manual: 'manual',
    units: 'unit conversion', site: 'from IFC site', default: 'not given',
  };
  return texts[value] || value || '';
}

function renderSolution() {
  const stats = $('#solution-stats');
  const warnings = $('#solution-warnings');
  const r = state.result;
  const unit = crsUnitSymbol();
  if (!r) {
    stats.innerHTML = `<dt>Status</dt><dd>${escapeHtml(state.solveError
      || 'Add a control point, or use the location stored in the IFC file, to see the result.')}</dd>`;
    warnings.innerHTML = '';
    $('#place-next').disabled = true;
    $('#allow-outside-row').hidden = true;
    return;
  }
  const s = r.solution;
  const m = state.meta.unit;
  // With one point the fit is exact by definition, so accuracy is only shown for 2+ points.
  const accuracy = r.points.length < 2 ? ''
    : `<dt>Accuracy</dt><dd>${fixed(s.rms_horizontal_m, 3)} m${s.rms_vertical_m != null ? ` <small>height ${fixed(s.rms_vertical_m, 3)} m</small>` : ''}</dd>`;
  stats.innerHTML = `
    <dt>E / N</dt><dd>${fixed(s.eastings, 3)} / ${fixed(s.northings, 3)} ${unit}</dd>
    <dt>Height</dt><dd>${fixed(s.orthogonal_height, 3)} ${unit} <small>${escapeHtml(sourceText('height', s.height_source))}</small></dd>
    <dt>Rotation</dt><dd>${fixed(s.rotation_deg, 4)}° <small>${escapeHtml(sourceText('rotation', s.rotation_source))}</small></dd>
    <dt>Scale</dt><dd>${Number(s.scale.toPrecision(10))} <small>${s.scale_source === 'units'
      ? `1 ${escapeHtml(m.symbol)} = ${Number(r.unit_scale.toPrecision(10))} ${unit}` : 'fitted from points'}</small></dd>
    ${accuracy}`;
  warnings.innerHTML = s.warnings.map((w) => `<li class="${w.level}">${escapeHtml(w.message)}</li>`).join('');
  const outside = s.warnings.some((w) => w.code === 'outside_area');
  $('#allow-outside-row').hidden = !outside;
  $('#allow-outside').checked = state.allowOutside;
  $('#place-next').disabled = !r.can_save;

  const manualRotation = state.rotationMode === 'manual';
  const manualHeight = state.heightMode === 'manual';
  $('#rotation-auto').disabled = !manualRotation;
  $('#height-auto').disabled = !manualHeight;
  $('#rotation-input').classList.toggle('manual', manualRotation);
  $('#height-input').classList.toggle('manual', manualHeight);
  const rotationInput = $('#rotation-input');
  const heightInput = $('#height-input');
  if (document.activeElement !== rotationInput) {
    rotationInput.value = fixed(state.rotationMode === 'manual' ? state.rotation : s.rotation_deg, 4);
  }
  if (document.activeElement !== heightInput) {
    heightInput.value = fixed(state.heightMode === 'manual' ? state.height : s.orthogonal_height, 3);
  }
  const usable = state.points.filter(complete).length + (state.useSite && siteHasLocation() ? 1 : 0);
  $('#free-scale-row').hidden = usable < 2 && !state.freeScale; // only meaningful with 2+ points
}

function renderPreview() {
  if (!previewLayer) return;
  previewLayer.clearLayers();
  pointLayer.clearLayers();
  const r = state.result;
  if (!r || !r.preview) return;
  const p = r.preview;
  if (p.footprint && p.footprint.length > 2) {
    L.polygon(p.footprint, { color: '#93a70a', weight: 2, fillColor: '#b0c70d', fillOpacity: 0.28, interactive: false }).addTo(previewLayer);
  }
  if (p.x_axis[0] && p.x_axis[1]) L.polyline(p.x_axis, { color: '#dc2626', weight: 3, interactive: false }).addTo(previewLayer);
  if (p.y_axis[0] && p.y_axis[1]) L.polyline(p.y_axis, { color: '#16a34a', weight: 3, interactive: false }).addTo(previewLayer);
  if (p.origin) {
    L.circleMarker(p.origin, { radius: 5, color: '#111827', fillColor: '#fff', fillOpacity: 1, weight: 2 })
      .bindTooltip('Model origin (0, 0, 0)').addTo(previewLayer);
  }
  for (const pt of p.points) {
    if (!pt.target) continue;
    if (pt.fitted) {
      L.polyline([pt.target, pt.fitted], { color: '#dc2626', weight: 1.5, dashArray: '4 3', interactive: false }).addTo(pointLayer);
      L.circleMarker(pt.fitted, { radius: 4, color: '#111827', weight: 1.5, fillOpacity: 0, interactive: false }).addTo(pointLayer);
    }
    const isSite = pt.source === 'site';
    const short = isSite ? 'S' : pt.label.replace(/^P/, '').slice(0, 2);
    const marker = L.marker(pt.target, { icon: pinIcon(escapeHtml(short), isSite ? 'site' : ''), draggable: !isSite });
    marker.bindTooltip(isSite ? 'IFC site location' : `${pt.label} – drag to adjust`);
    if (!isSite) {
      marker.on('dragend', async () => {
        const point = state.points.find((q) => q.label === pt.label);
        if (!point) return;
        try {
          await setTargetFromLatLng(point, marker.getLatLng());
          renderPoints();
          scheduleSolve(0);
        } catch (err) {
          toast(err.message, 'error');
        }
      });
    }
    marker.addTo(pointLayer);
  }
  fitPreview(false);
  update3d('place');
}

function fitPreview(force) {
  const r = state.result;
  if (!r || !r.preview || !r.preview.bounds) return;
  if (force && views.place.mode === '3d' && views.place.map3d) {
    views.place.map3d.refit(r.preview.bounds);
    return;
  }
  if (!placeMap) return;
  if (state.mapFitted && !force) return;
  state.mapFitted = true;
  placeMap.map.fitBounds(r.preview.bounds, { padding: [40, 40], maxZoom: 20 });
}

/* ----- 3D map view (placement and save steps) ----- */

const views = { place: { mode: '2d', map3d: null }, save: { mode: '2d', map3d: null } };

async function setView(target, mode) {
  const view = views[target];
  view.mode = mode;
  $$(`.seg-btn[data-target="${target}"]`).forEach((b) => b.classList.toggle('active', b.dataset.view === mode));
  $(`#${target}-map`).hidden = mode === '3d';
  $(`#${target}-map3d`).hidden = mode !== '3d';
  $(`#${target}-map3d-note`).hidden = mode !== '3d';
  if (mode !== '3d') return;
  if (target === 'place' && state.picking && state.picking.stage === 'map') cancelPicking();
  try {
    if (!view.map3d) {
      const mod = await import('./map3d.js');
      view.map3d = new mod.Map3DView($(`#${target}-map3d`));
      const loc = state.location;
      await view.map3d.init(target === 'place' ? $('#basemap').value : defaultBasemap(loc && loc.lat, loc && loc.lon));
    }
    await update3d(target);
  } catch (e) {
    console.error(e);
    toast(`The 3D map could not be shown: ${e.message}`, 'error');
    setView(target, '2d');
  }
}

async function update3d(target) {
  const view = views[target];
  if (!view.map3d || view.mode !== '3d') return;
  const r = state.result;
  const placement = r && r.preview ? r.preview.map3d : null;
  const note = $(`#${target}-map3d-note`);
  if (!placement || !state.geometry || state.geometry.empty) {
    note.textContent = state.geometry && state.geometry.empty
      ? 'This file has no 3D geometry to show.' : 'The 3D model appears here once it is placed.';
    return;
  }
  note.textContent = `The model stands on the map surface. Height of the model origin stored in the file: `
    + `${fixed(r.solution.orthogonal_height, 2)} ${crsUnitSymbol()} (${state.crs ? `EPSG:${state.crs.code}` : ''}).`;
  try {
    await view.map3d.setModel(modelUrl(state.jobId));
  } catch (e) {
    note.textContent = `The 3D model could not be loaded: ${e.message}`;
    return;
  }
  view.map3d.setPlacement(placement, r.preview.bounds);
}

function updateViewerMarkers() {
  if (!viewer) return;
  const unit = unitMetres();
  const markers = [];
  const s = site();
  if (state.useSite && siteHasLocation()) {
    markers.push({ label: 'IFC site', world: s.origin.map((v) => v * unit), kind: 'site' });
  }
  for (const p of state.points) {
    if (p.local && p.local[0] != null && p.local[1] != null) {
      markers.push({ label: p.label, world: [p.local[0] * unit, p.local[1] * unit, (p.local[2] || 0) * unit], kind: 'point' });
    }
  }
  viewer.setMarkers(markers);
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

function currentKey() {
  return state.crs ? JSON.stringify(payload()) : null;
}

function enterSave() {
  const r = state.result;
  if (!r) {
    goTo('place');
    return;
  }
  const s = r.solution;
  const unit = crsUnitSymbol();
  $('#save-summary').innerHTML = `
    <dt>File</dt><dd>${escapeHtml(state.meta.file ? state.meta.file.name : '')}</dd>
    <dt>Coordinate system</dt><dd>EPSG:${state.crs.code} · ${escapeHtml(state.crs.name)}</dd>
    <dt>Easting / Northing</dt><dd>${fixed(s.eastings, 3)} / ${fixed(s.northings, 3)} ${unit}</dd>
    <dt>Height</dt><dd>${fixed(s.orthogonal_height, 3)} ${unit}</dd>
    <dt>Rotation</dt><dd>${fixed(s.rotation_deg, 4)}°</dd>
    <dt>Scale</dt><dd>${Number(s.scale.toPrecision(10))}</dd>
    <dt>Control points</dt><dd>${r.points.length}${r.points.length > 1 ? ` · accuracy ${fixed(s.rms_horizontal_m, 3)} m` : ''}</dd>`;
  if (!saveMap) {
    const loc = state.location;
    saveMap = createMap($('#save-map'), { basemap: defaultBasemap(loc && loc.lat, loc && loc.lon) });
    saveLayer = L.layerGroup().addTo(saveMap.map);
  }
  saveLayer.clearLayers();
  const p = r.preview;
  if (p.footprint && p.footprint.length > 2) {
    L.polygon(p.footprint, { color: '#93a70a', weight: 2, fillColor: '#b0c70d', fillOpacity: 0.3 }).addTo(saveLayer);
  }
  if (p.origin) L.circleMarker(p.origin, { radius: 5, color: '#111827', fillColor: '#fff', fillOpacity: 1 }).addTo(saveLayer);
  setTimeout(() => {
    saveMap.map.invalidateSize();
    if (p.bounds) saveMap.map.fitBounds(p.bounds, { padding: [30, 30], maxZoom: 19 });
  }, 50);
  if (views.save.map3d) views.save.map3d.framed = false;
  update3d('save');
  const isCurrent = state.writtenKey && state.writtenKey === currentKey();
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
    const key = currentKey();
    const status = await api.georeference(state.jobId, payload());
    state.writtenKey = key;
    state.saveStartedAt = Date.now();
    state.uploadStartedAt = null;
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
  if (!task || state.step !== 'save' || state.writtenKey !== currentKey()) return;
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

function showDone() {
  const out = state.status.output;
  $('#save-actions').hidden = true;
  $('#done-panel').hidden = false;
  renderFileBar();
  $('#done-text').innerHTML = `<b>Done.</b> ${escapeHtml(out.filename)} (${bytes(out.bytes)}) was written and checked.`;
  const link = $('#download-btn');
  link.href = downloadUrl(state.jobId);
  link.setAttribute('download', out.filename);
  const button = $('#project-upload-btn');
  if (state.project.id) {
    button.hidden = false;
    button.textContent = `Upload to project ${state.project.id}`;
    $('#project-hint').textContent = state.project.token ? ''
      : 'No GeoDataHub login was passed to this page, so the project server may reject the upload.';
  } else {
    button.hidden = true;
    $('#project-hint').textContent = 'Open this tool from a GeoDataHub project to upload the file there directly.';
  }
}

async function uploadToProject() {
  $('#project-upload-btn').disabled = true;
  $('#project-result').hidden = true;
  try {
    const status = await api.uploadToProject(state.jobId, state.project.id, state.project.token);
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
    title: 'Uploading to the project',
    progress: task.progress,
    eta: eta(task.started_at ? task.started_at * 1000 : state.uploadStartedAt, task.progress),
    steps: task.steps,
    error: task.state === 'error' ? task.error : null,
    waiting: task.state === 'queued' ? 'Starting…' : null,
  });
  if (task.state === 'done') {
    showProjectResult(`Uploaded to project ${state.project.id}.`, 'success');
    $('#project-upload-btn').disabled = false;
    $('#project-upload-btn').textContent = 'Upload again';
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
  $$('.seg-btn').forEach((b) => b.addEventListener('click', () => setView(b.dataset.target, b.dataset.view)));
  $('#basemap').addEventListener('change', () => {
    if (views.place.map3d) views.place.map3d.setBasemap($('#basemap').value);
  });
}

function init() {
  readLaunchParams();
  listenForParentAuth();
  renderProjectBadge();
  setupNavigation();
  setupUpload();
  setupCrs();
  setupPlace();
  setupSave();
  const match = /job=([0-9a-f]{32})/.exec(window.location.hash);
  if (match) resume(match[1]);
  else goTo('upload');
}

init();
