/**
 * Step 3: placement. Control points, manual placement (drag / rotate / scale on the map),
 * scale sources, undo/redo, live preview (engine.js) and the authoritative server solve.
 *
 * Manual placement is an *anchor* (one model point pinned to one map position), so moving,
 * rotating and scaling all keep the anchored point fixed: the footprint centre for
 * rotate/scale, the first point for scale-by-distance.
 */
import { api, edgesUrl, modelUrl } from './api.js';
import { combine } from './progress.js';
import { $, $$, escapeHtml, fixed, parseNumber, storage, toast } from './util.js';
import { cadastralLayer, createMap, defaultBasemap, inSwitzerland, pinIcon } from './map.js';
import * as engine from './engine.js';
import { History } from './history.js';
import { choose, promptNumber } from './dialogs.js';
import { PlacementHandles } from './handles.js';
import {
  CONFIG, complete, crsUnitMetres, crsUnitSymbol, defaultPlacement, localDigits, newPoint, site,
  siteHasLocation, state, syncPointIds, unitMetres,
} from './store.js';

const L = window.L;
const prefs = storage('local');
const ROTATION_STEPS = { 0.01: 0.01, 0.1: 0.1, 1: 1, 10: 5 };
const ASK_SCALE_TOLERANCE = 0.001; // ask before replacing a deliberate scale by more than 0.1 %
const SOURCE_NAMES = {
  units: 'from the file units', file: 'from the file', distance: 'from a distance', manual: 'typed by hand',
  points: 'fitted from the points',
};

const P = () => state.place;
let hooks = { goTo() {}, unlock() {}, save() {} };

let viewer = null;
let viewerError = null;
let modelLoaded = false;
let modelLoading = false;
let placeMap = null;
let pointLayer = null;
let axesLayer = null;
let handles = null;
let cadastral = null;
let solveTimer = null;
let solveSeq = 0;
let gmCache = { result: null, gm: null };
let drag = null;   // manual drag in progress
let live = null;   // client-side solution while the server solve is pending
const history = new History(50);
const editing = new Set();
export const views = { place: { mode: '2d', map3d: null }, save: { mode: '2d', map3d: null } };

/* ===== context ======================================================================= */

function ctx() {
  return state.result ? state.result.client : null;
}

function mercator() {
  if (gmCache.result !== state.result) {
    const c = ctx();
    gmCache = { result: state.result, gm: c && c.grid_to_mercator ? new engine.GridMercator(c.grid_to_mercator) : null };
  }
  return gmCache.gm;
}

function current() {
  return live || (state.result && state.result.solution) || null;
}

function declaredUnitScale() {
  return unitMetres() / crsUnitMetres();
}

function unitScaleNow() {
  const o = P().unitOverride;
  return (o ? engine.UNIT_FACTORS[o] : unitMetres()) / crsUnitMetres();
}

function unitSymbolNow() {
  return P().unitOverride || (state.meta ? state.meta.unit.symbol : 'm');
}

/** Footprint centre (file units): the pivot for manual rotation and scaling. */
function pivotLocal() {
  const c = ctx();
  if (c && c.pivot_local) return c.pivot_local;
  const g = state.geometry;
  const outline = g && (P().footprintMode === 'all' ? g.footprint_all_m || g.footprint_m : g.footprint_m);
  if (outline && outline.length) {
    const xs = outline.map((p) => p[0] / unitMetres());
    const ys = outline.map((p) => p[1] / unitMetres());
    return [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
  }
  const b = state.meta && state.meta.placement_bounds;
  return b ? [b.center[0], b.center[1]] : [0, 0];
}

function snapshot() {
  return JSON.parse(JSON.stringify(P()));
}

function scaleLabel(sol) {
  if (!sol) return '';
  if (sol.scale_source === 'units') {
    return `File units (${unitSymbolNow()} → ${Number(unitScaleNow().toPrecision(10))})${P().unitOverride ? ' (unit override)' : ''}`;
  }
  return { file: 'From file', points: 'Fitted from points', distance: 'From distance', manual: 'Manual' }[sol.scale_source]
    || sol.scale_source;
}

/** Same inputs as the server solve, computed in the browser for an instant preview. */
function localSolve() {
  const r = state.result;
  if (!r) return null;
  const p = P();
  const pts = [];
  const sitePoint = r.points.find((x) => x.source === 'site');
  if (p.useSite && sitePoint) pts.push({ label: sitePoint.label, local: sitePoint.local, target: sitePoint.target });
  p.points.filter(complete).forEach((x) => pts.push({
    label: x.label, local: [x.local[0], x.local[1], x.local[2] || 0], target: [x.target[0], x.target[1], x.target[2]],
  }));
  return engine.solve(pts, {
    unitScale: unitScaleNow(), rotationFallbackDeg: r.client.rotation_fallback_deg, crsUnitM: crsUnitMetres(),
    scaleMode: p.scale.mode, scaleValue: p.scale.source === 'units' && p.scale.mode !== 'free' ? null : p.scale.value,
    scaleSource: p.scale.source, rotationDeg: p.rotation,
    height: p.height, anchor: p.anchor,
  });
}

export function payload() {
  const p = P();
  return {
    epsg: state.crs.code,
    use_site: Boolean(p.useSite && siteHasLocation()),
    points: p.points.filter(complete).map((x) => ({
      label: x.label,
      local: [x.local[0], x.local[1], x.local[2] == null ? 0 : x.local[2]],
      target: [x.target[0], x.target[1], x.target[2] == null ? null : x.target[2]],
    })),
    location: state.location ? { lat: state.location.lat, lon: state.location.lon } : null,
    scale: p.scale.mode === 'free'
      ? { mode: 'free', value: p.scale.value, source: p.scale.source }
      : { mode: 'fixed', value: p.scale.source === 'units' ? null : p.scale.value, source: p.scale.source },
    unit_override: p.unitOverride,
    rotation_deg: p.rotation,
    height: p.height,
    anchor: p.anchor,
    footprint_mode: p.footprintMode,
    allow_outside: state.allowOutside,
  };
}

export function placementKey() {
  return state.crs ? JSON.stringify(payload()) : null;
}

/* ===== changes, history ================================================================= */

function commit(mutate, delay = 0) {
  history.push(snapshot());
  mutate();
  state.placeTouched = true;
  changed(delay);
}

function changed(delay = 0) {
  live = localSolve();
  renderAll();
  scheduleSolve(delay);
  hooks.save();
}

function undo() {
  const prev = history.undo(snapshot());
  if (!prev) return;
  state.place = prev;
  syncPointIds();
  cancelPicking(true);
  state.placeTouched = true;
  changed(0);
}

function redo() {
  const next = history.redo(snapshot());
  if (!next) return;
  state.place = next;
  syncPointIds();
  cancelPicking(true);
  state.placeTouched = true;
  changed(0);
}

/** Current map position of a model point (file units). */
function mapOf(localXY) {
  const sol = current();
  if (!sol) return null;
  const [e, n] = engine.apply(sol, [localXY[0], localXY[1], 0]);
  return [e, n];
}

/** Pin a model point at its current map position (it stays put when rotating / scaling). */
function pinAt(localXY) {
  const m = mapOf(localXY);
  if (m) P().anchor = { local: [localXY[0], localXY[1]], map: m };
}

function ensurePin() {
  if (!P().anchor) pinAt(pivotLocal());
}

/* ===== setup ================================================================================ */

export function setupPlace(h) {
  hooks = { ...hooks, ...h };
  $('#add-point').addEventListener('click', () => beginPick(null, 'model'));
  $('#place-hand').addEventListener('click', placeByHand);
  $('#pick-cancel').addEventListener('click', () => cancelPicking());
  $('#undo-btn').addEventListener('click', undo);
  $('#redo-btn').addEventListener('click', redo);
  document.addEventListener('keydown', onKey);
  $('#snap-radius').addEventListener('input', (e) => setSnapRadius(e.target.value));
  $('#view-top').addEventListener('click', () => viewer && viewer.topView());
  $('#view-fit').addEventListener('click', () => viewer && viewer.fit());
  $('#map-fit').addEventListener('click', () => fitPreview(true));
  $('#layers-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    $('#layers-panel').hidden = !$('#layers-panel').hidden;
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#layers-panel') && !e.target.closest('#layers-btn')) $('#layers-panel').hidden = true;
  });
  $('#cadastral-toggle').addEventListener('change', (e) => setCadastral(e.target.checked));
  $('#fill-opacity').addEventListener('input', (e) => {
    state.fillOpacity = Number(e.target.value) / 100;
    prefs.set('georef.fill', state.fillOpacity);
    renderPreview();
  });
  $('#footprint-all').addEventListener('change', (e) => commit(() => { P().footprintMode = e.target.checked ? 'all' : 'building'; }));
  $('#use-site').addEventListener('change', (e) => commit(() => { P().useSite = e.target.checked; }));
  $('#paste-toggle').addEventListener('click', () => {
    $('#paste-box').hidden = !$('#paste-box').hidden;
    if (!$('#paste-box').hidden) $('#paste-text').focus();
  });
  $('#paste-close').addEventListener('click', () => { $('#paste-box').hidden = true; });
  $('#paste-add').addEventListener('click', addPastedPoints);
  $('#type-point').addEventListener('click', () => {
    cancelPicking();
    commit(() => {
      const point = newPoint();
      P().points.push(point);
      editing.add(point.id);
    });
    const first = $(`#points-body .pt[data-index="${P().points.length - 1}"] input`);
    if (first) first.focus();
  });
  $('#points-body').addEventListener('change', onPointInput);
  $('#points-body').addEventListener('click', onPointButton);
  $('#manual-chips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-chip]');
    if (!b) return;
    if (b.dataset.chip === 'rotation') commit(() => { P().rotation = null; });
    if (b.dataset.chip === 'position') commit(() => { P().anchor = null; });
  });

  // tabs
  $$('.tab').forEach((t) => t.addEventListener('click', () => {
    $$('.tab').forEach((x) => x.classList.toggle('active', x === t));
    $$('.tab-panel').forEach((p) => { p.hidden = p.dataset.panel !== t.dataset.tab; });
  }));

  // move & rotate
  $$('[data-nudge]').forEach((b) => b.addEventListener('click', () => nudge(b.dataset.nudge)));
  $('#rotation-input').addEventListener('change', (e) => {
    const v = parseNumber(e.target.value);
    if (v == null) return;
    commit(() => { pinAt(pivotLocal()); P().rotation = engine.ccwFromCw(Math.round(v * 100) / 100); });
  });
  $('#rotation-auto').addEventListener('click', () => commit(() => { P().rotation = null; }));
  for (const [id, axis] of [['#move-de', 0], ['#move-dn', 1], ['#move-dh', 2]]) {
    $(id).addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const v = parseNumber(e.target.value);
      if (v == null || !current()) return;
      commit(() => {
        if (axis === 2) {
          P().height = current().orthogonal_height + v;
          P().heightSource = 'manual';
        } else {
          ensurePin();
          P().anchor.map[axis] += v;
        }
      });
      e.target.value = '';
    });
  }
  $('#reset-adjust').addEventListener('click', () => commit(() => {
    Object.assign(P(), { anchor: null, rotation: null, height: null, heightSource: 'auto',
      scale: { mode: 'fixed', value: null, source: 'units' }, unitOverride: null });
  }));

  // scale
  $('#unit-override').addEventListener('change', (e) => setUnitOverride(e.target.value || null));
  $('#scale-input').addEventListener('change', async (e) => {
    const v = parseNumber(e.target.value);
    if (v == null || v <= 0) { renderControls(); return; }
    if (await confirmScale(v, 'manual') !== 'new') { renderControls(); return; }
    commit(() => { pinAt(pivotLocal()); P().scale = { mode: 'fixed', value: v, source: 'manual' }; });
  });
  $('#scale-distance').addEventListener('click', startDistance);
  $('#free-scale').addEventListener('change', (e) => toggleFreeScale(e.target.checked));
  $('#scale-unlock').addEventListener('change', (e) => {
    if (handles) handles.setScaleEnabled(e.target.checked);
    toast(e.target.checked ? 'Scale unlocked: drag a corner handle of the frame to resize the model.'
      : 'Scale locked: the model keeps its scale while you move and rotate it.');
  });

  // height
  $('#height-input').addEventListener('change', (e) => {
    const v = parseNumber(e.target.value);
    if (v == null) return;
    commit(() => { P().height = v; P().heightSource = 'manual'; });
  });
  $('#height-auto').addEventListener('click', () => commit(() => { P().height = null; P().heightSource = 'auto'; }));
  $('#height-up').addEventListener('click', () => stepHeight(1));
  $('#height-down').addEventListener('click', () => stepHeight(-1));
  $('#use-ground').addEventListener('click', useGroundHeight);
  $('#use-file-height').addEventListener('click', useFileHeight);

  $('#allow-outside').addEventListener('change', (e) => {
    state.allowOutside = e.target.checked;
    scheduleSolve(0);
  });
  $('#place-next').addEventListener('click', () => {
    hooks.unlock('save');
    hooks.goTo('save');
  });
  $$('.seg-btn').forEach((b) => b.addEventListener('click', () => setView(b.dataset.target, b.dataset.view)));
  $('#basemap').addEventListener('change', () => {
    if (views.place.map3d) views.place.map3d.setBasemap($('#basemap').value);
  });
}

function onKey(e) {
  if (state.step !== 'place') return;
  if (e.key === 'Escape') {
    cancelPicking();
    return;
  }
  const t = e.target;
  // Ctrl+Z inside a text field undoes the typing; on checkboxes, sliders and buttons it undoes the placement.
  const typing = t && (t.tagName === 'TEXTAREA' || t.isContentEditable
    || (t.tagName === 'INPUT' && !['checkbox', 'radio', 'range', 'button'].includes(t.type)));
  if (typing) return;
  if (document.querySelector('#modal[open]')) return;
  const mod = e.ctrlKey || e.metaKey;
  const key = e.key.toLowerCase();
  if (mod && key === 'z') {
    e.preventDefault();
    if (e.shiftKey) redo(); else undo();
  } else if (mod && key === 'y') {
    e.preventDefault();
    redo();
  }
}

/* ===== entering the step ===================================================================== */

export async function enterPlace() {
  const loc = state.location;
  if (!placeMap) {
    placeMap = createMap($('#place-map'), {
      basemap: defaultBasemap(loc && loc.lat, loc && loc.lon), select: $('#basemap'),
      center: loc ? [loc.lat, loc.lon] : [46.8, 8.2], zoom: loc ? 18 : 7,
    });
    axesLayer = L.layerGroup().addTo(placeMap.map);
    pointLayer = L.layerGroup().addTo(placeMap.map);
    handles = new PlacementHandles(placeMap.map, {
      onStart: dragStart, onMove: dragMove, onEnd: dragEnd, onCancel: dragCancel, onRotateDblClick: openAngleDialog,
    });
    handles.setScaleEnabled($('#scale-unlock').checked);
    placeMap.map.on('click', onMapClick);
    const swiss = inSwitzerland(loc && loc.lat, loc && loc.lon);
    $('#cadastral-row').hidden = !swiss;
    const savedCadastral = prefs.get('georef.cadastral', null);
    $('#cadastral-toggle').checked = swiss && (savedCadastral == null ? true : savedCadastral);
    setCadastral($('#cadastral-toggle').checked, false);
    state.fillOpacity = prefs.get('georef.fill', state.fillOpacity);
    $('#fill-opacity').value = Math.round(state.fillOpacity * 100);
  }
  if (!viewer && !viewerError) {
    try {
      const mod = await import('./viewer.js');
      viewer = new mod.ModelViewer($('#viewer'), { onPick: onModelPick });
      setSnapRadius(prefs.get('georef.snap', mod.DEFAULT_SNAP_PX));
    } catch (e) {
      viewerError = 'The 3D view could not be started in this browser (WebGL or network problem). '
        + 'You can still type the model coordinates and place the model by hand on the map.';
      console.error(e);
    }
  }
  maybePrefill();
  skipFarSite();
  $('#height-unit').textContent = crsUnitSymbol();
  $$('.crs-unit').forEach((el) => { el.textContent = crsUnitSymbol(); });
  const s = site();
  $('#use-site').disabled = !siteHasLocation();
  $('#use-site-detail').textContent = siteHasLocation()
    ? `${fixed(s.latitude, 6)}°, ${fixed(s.longitude, 6)}°${s.elevation_m != null ? ` · elevation ${fixed(s.elevation_m, 2)} m` : ''}`
    : 'No location is stored in this file.';
  $('#use-site-row').classList.toggle('muted', !siteHasLocation());
  const declared = state.meta.unit.symbol;
  $('#unit-override').innerHTML = `<option value="">File: ${escapeHtml(declared)}</option>`
    + ['mm', 'cm', 'm', 'in', 'ft'].map((u) => `<option value="${u}">${u}</option>`).join('');
  $('#footprint-row').hidden = !(state.geometry && state.geometry.has_context);
  renderViewerStatus();
  maybeLoadModel();
  live = localSolve();
  renderAll();
  scheduleSolve(0);
}

/** R7a/R7d: restore the placement stored in the file whenever its CRS is the selected one. */
function maybePrefill() {
  const code = state.crs && state.crs.code;
  if (!code || state.prefilledFor === code || state.placeTouched) return;
  const { gdh } = state.meta;
  const g = state.meta.georef || {};
  const note = $('#existing-note');
  if (gdh && Number(gdh.epsg) === code) {
    state.place = placementFromGdh(gdh);
    note.textContent = `Restored the placement saved with this file: ${state.place.points.length} control point(s), `
      + `scale ${SOURCE_NAMES[(gdh.scale || {}).source] || SOURCE_NAMES.units}. Adjust it or continue to save again.`;
  } else if (g.present && g.epsg === code && g.eastings != null) {
    state.place = {
      ...defaultPlacement(),
      anchor: { local: [0, 0], map: [g.eastings, g.northings] },
      rotation: g.rotation_deg || 0,
      scale: { mode: 'fixed', value: g.scale, source: 'file' },
      height: g.orthogonal_height || 0,
      heightSource: 'file',
    };
    note.textContent = `Loaded the georeferencing stored in the file (EPSG:${code}), including its scale `
      + `(${Number(g.scale.toPrecision(8))}). Adjust it or continue to save again.`;
  } else {
    return;
  }
  note.hidden = false;
  syncPointIds();
  state.prefilledFor = code;
  history.clear();
}

function placementFromGdh(gdh) {
  const scale = gdh.scale || {};
  const height = gdh.height || {};
  const rotation = gdh.rotation || {};
  return {
    ...defaultPlacement(),
    points: (gdh.points || []).map((p) => newPoint({
      label: p.label, local: p.local, target: p.target, hSource: p.target && p.target[2] != null ? 'user' : null,
    })),
    useSite: Boolean(gdh.use_site) && siteHasLocation(),
    rotation: rotation.override_deg == null ? null : rotation.override_deg,
    anchor: gdh.anchor || null,
    scale: scale.mode === 'free'
      ? { mode: 'free', value: scale.value, source: 'points' }
      : { mode: 'fixed', value: scale.source === 'units' ? null : scale.value, source: scale.source || 'units' },
    unitOverride: scale.unit_override || null,
    height: height.override == null ? null : height.override,
    heightSource: height.override == null ? 'auto' : 'manual',
    footprintMode: gdh.footprint_mode || 'building',
  };
}

/** The coordinate system changed: map positions and angles of the old one no longer apply. */
export function resetManualPlacement() {
  const p = P();
  const had = p.anchor || p.rotation != null || p.height != null;
  Object.assign(p, { anchor: null, rotation: null, height: null, heightSource: 'auto' });
  if (p.scale.source === 'file') p.scale = { mode: 'fixed', value: null, source: 'units' };
  state.prefilledFor = null; // back on the file's CRS, the stored placement is loaded again (unless edited)
  state.mapFitted = false;
  history.clear();
  if (had) toast('The manual position, rotation and height were reset because the coordinate system changed.');
}

/** A new file on the same page. */
export function resetPlace() {
  modelLoaded = false;
  modelLoading = false;
  if (viewer) viewer.unload();
  if (pointLayer) {
    pointLayer.clearLayers();
    axesLayer.clearLayers();
  }
  if (handles) handles.set(null);
  history.clear();
  editing.clear();
  live = null;
  Object.values(views).forEach((v) => { if (v.map3d) v.map3d.framed = false; });
  $('#existing-note').hidden = true;
}

export function onPlaceStatus() {
  renderViewerStatus();
  maybeLoadModel();
  $('#footprint-row').hidden = !(state.geometry && state.geometry.has_context);
}

/* ===== 3D model ==================================================================================== */

function setSnapRadius(px) {
  const value = Math.max(0, Math.min(50, Number(px) || 0));
  $('#snap-radius').value = value;
  $('#snap-radius-value').textContent = value ? `${value} px` : 'off';
  prefs.set('georef.snap', value);
  if (viewer) viewer.setSnapRadius(value);
}

function renderViewerStatus() {
  const overlay = $('#viewer-overlay');
  const show = (html) => {
    overlay.hidden = false;
    overlay.innerHTML = `<div class="box">${html}</div>`;
  };
  if (viewerError) return show(escapeHtml(viewerError));
  if (state.geometry && state.geometry.empty) {
    return show('This file has no 3D geometry. Type the model coordinates of your control points, or place the model by hand on the map.');
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
      <p class="small muted" style="margin-top:8px">You can already place the model by hand or use the IFC site location.</p>`);
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
    if (state.geometry.edges) viewer.loadEdges(edgesUrl(state.jobId));
    update3d('place');
  } catch (e) {
    viewerError = `The 3D model could not be loaded: ${e.message}`;
  } finally {
    modelLoading = false;
    renderViewerStatus();
    updateViewerMarkers();
  }
}

function updateViewerMarkers() {
  if (!viewer) return;
  const unit = unitMetres();
  const markers = [];
  const s = site();
  if (P().useSite && siteHasLocation()) markers.push({ label: 'IFC site', world: s.origin.map((v) => v * unit), kind: 'site' });
  for (const p of P().points) {
    if (p.local && p.local[0] != null && p.local[1] != null) {
      markers.push({ label: p.label, world: [p.local[0] * unit, p.local[1] * unit, (p.local[2] || 0) * unit], kind: 'point' });
    }
  }
  const pk = state.picking;
  if (pk && pk.kind === 'distance') {
    pk.picks.forEach((q, i) => markers.push({ label: `D${i + 1}`, world: q.map((v) => v * unit), kind: 'measure' }));
  }
  viewer.setMarkers(markers);
}

/* ===== control points ===================================================================================== */

function residualFor(label) {
  const sol = current();
  return sol && sol.residuals ? sol.residuals.find((r) => r.label === label) : null;
}

function residualText(label) {
  const r = residualFor(label);
  const n = current() && current().residuals ? current().residuals.length : 0;
  if (!r || n < 2) return ['', ''];
  return [`${fixed(r.horizontal, 3)} m`, r.horizontal < 0.05 ? 'good' : r.horizontal < 0.5 ? 'fair' : 'bad'];
}

const hasLocal = (p) => Boolean(p.local && p.local[0] != null && p.local[1] != null);
const hasTarget = (p) => Boolean(p.target && p.target[0] != null && p.target[1] != null);

function pointStatus(p) {
  const model = hasLocal(p) ? '<span class="ok">model ✓</span>' : '<span class="todo">model ?</span>';
  const map = hasTarget(p) ? '<span class="ok">map ✓</span>' : '<span class="todo">map ?</span>';
  return `${model} · ${map}${p.hSource === 'terrain' ? ' · height from terrain' : ''}`;
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
  if ($('#points-body').contains(document.activeElement)) {
    updateResiduals();
    renderPointsHint();
    return;
  }
  const items = [];
  if (P().useSite && siteHasLocation()) {
    const sitePoint = state.result ? state.result.points.find((p) => p.source === 'site') : null;
    const t = sitePoint ? sitePoint.target : null;
    const [err, cls] = residualText('IFC site');
    items.push(`<li class="pt site" data-label="IFC site">
      <div class="pt-head"><span class="pt-label">Site</span>
        <span class="pt-status">from the IFC file</span><span class="pt-err ${cls}">${err}</span></div>
      ${t ? `<div class="pt-coords">Map ${fixed(t[0], 3)}, ${fixed(t[1], 3)}, h ${fixed(t[2], 2)} ${crsUnitSymbol()}</div>` : ''}
    </li>`);
  }
  P().points.forEach((p, i) => {
    const [err, cls] = residualText(p.label);
    const active = state.picking && state.picking.kind === 'point' && state.picking.index === i;
    const open = editing.has(p.id);
    items.push(`<li class="pt${active ? ' active' : ''}" data-label="${escapeHtml(p.label)}" data-index="${i}">
      <div class="pt-head">
        <span class="pt-label">${escapeHtml(p.label)}</span>
        <span class="pt-status">${pointStatus(p)}</span>
        <span class="pt-err ${cls}" title="Distance between the map point and where the model point lands">${err}</span>
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
  $('#use-site').checked = P().useSite && siteHasLocation();
  $('#use-site-detail').hidden = P().useSite && siteHasLocation();
  renderPointsHint();
}

function updateResiduals() {
  $$('#points-body .pt').forEach((li) => {
    const cell = $('.pt-err', li);
    if (!cell) return;
    const [text, cls] = residualText(li.dataset.label);
    cell.textContent = text;
    cell.className = `pt-err ${cls}`;
  });
}

function usableCount() {
  return P().points.filter(complete).length + (P().useSite && siteHasLocation() ? 1 : 0);
}

function renderPointsHint() {
  const usable = usableCount();
  const incomplete = P().points.filter((p) => !complete(p)).map((p) => p.label);
  let hint;
  if (!usable && !P().anchor) {
    hint = siteHasLocation()
      ? 'Use the location stored in the IFC file, click “Pick a point” (a corner on the model, then on the map), or “Place on map” to place the model by hand.'
      : 'Click “Place on map” and drag the model into place, or “Pick a point”: a corner on the model, then the same corner on the map.';
  } else if (!usable) {
    hint = 'Placed by hand. Drag the footprint to move it, the round handle to rotate it; add points for accuracy figures.';
  } else if (usable === 1) {
    hint = 'One point places the model. Add a second point far from the first to fix the rotation, or turn the model on the map.';
  } else {
    hint = `${usable} points used. The value next to each point is how far its map point is from where the model lands.`;
  }
  if (incomplete.length) hint += ` Still to complete: ${incomplete.join(', ')}.`;
  $('#points-hint').textContent = hint;
  $('#points-count').textContent = usable ? `(${usable})` : '';
}

function onPointButton(e) {
  const button = e.target.closest('button');
  if (!button) return;
  const index = Number(button.dataset.index);
  const point = P().points[index];
  if (!point) return;
  if (button.dataset.action === 'model') beginPick(index, 'model');
  if (button.dataset.action === 'map') beginPick(index, 'map');
  if (button.dataset.action === 'edit') {
    if (editing.has(point.id)) editing.delete(point.id); else editing.add(point.id);
    renderPoints();
  }
  if (button.dataset.action === 'delete') {
    editing.delete(point.id);
    commit(() => { P().points.splice(index, 1); });
  }
}

function onPointInput(e) {
  const input = e.target;
  if (!input.dataset || input.dataset.index == null) return;
  const index = Number(input.dataset.index);
  if (!P().points[index]) return;
  const k = Number(input.dataset.k);
  const value = parseNumber(input.value);
  const side = input.dataset.side;
  history.push(snapshot());
  const p = P().points[index];
  if (!p[side]) p[side] = [null, null, null];
  p[side][k] = value;
  if (side === 'target' && k === 2) p.hSource = value == null ? null : 'user';
  if (side === 'local' && k === 2 && p.hSource === 'terrain' && p.terrain != null) {
    p.target[2] = (p.terrain + (value || 0) * unitMetres()) / crsUnitMetres();
  }
  state.placeTouched = true;
  input.classList.toggle('empty', value == null);
  if (value != null) input.value = fixed(value, side === 'local' ? localDigits() : 3);
  const status = input.closest('.pt') && $('.pt-status', input.closest('.pt'));
  if (status) status.innerHTML = pointStatus(p);
  changed(0); // the list is not redrawn while a field has focus
}

function addPastedPoints() {
  const lines = $('#paste-text').value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const parsed = [];
  const skipped = [];
  for (const line of lines) {
    const sep = /[\t;]/.test(line) ? /[\t;]+/ : /\s+/.test(line) ? /\s+/ : /,/;
    const v = line.split(sep).map(parseNumber).filter((x) => x != null);
    if (v.length === 4) parsed.push({ local: [v[0], v[1], 0], target: [v[2], v[3], null] });
    else if (v.length === 5) parsed.push({ local: v.slice(0, 3), target: [v[3], v[4], null] });
    else if (v.length >= 6) parsed.push({ local: v.slice(0, 3), target: v.slice(3, 6) });
    else skipped.push(line);
  }
  if (parsed.length) {
    commit(() => parsed.forEach((x) => P().points.push(newPoint({ ...x, hSource: x.target[2] == null ? null : 'user' }))));
    $('#paste-text').value = skipped.join('\n');
  }
  toast(skipped.length ? `${parsed.length} point(s) added, ${skipped.length} line(s) not understood.` : `${parsed.length} point(s) added.`,
    skipped.length ? 'error' : 'success');
}

/* ===== picking (control points and scale-by-distance) =========================================================== */

function beginPick(index, stage) {
  if (!state.crs) return;
  cancelPicking();
  let pickStage = stage;
  if (pickStage === 'model' && !modelLoaded) {
    toast('The 3D model is not available. Pick the map point now and type the model coordinates.');
    pickStage = 'map';
  }
  history.push(snapshot());
  let pickIndex = index;
  let isNew = false;
  if (pickIndex == null) {
    P().points.push(newPoint());
    pickIndex = P().points.length - 1;
    isNew = true;
  }
  state.picking = { kind: 'point', index: pickIndex, stage: pickStage, isNew, changed: false };
  applyPickingMode();
  renderPoints();
}

function startDistance() {
  cancelPicking();
  if (!modelLoaded) {
    toast('Scale by distance needs the 3D model: pick two points on it.', 'error');
    return;
  }
  state.picking = { kind: 'distance', picks: [] };
  applyPickingMode();
}

function applyPickingMode() {
  const pk = state.picking;
  const onModel = Boolean(pk && (pk.kind === 'distance' || pk.stage === 'model'));
  const onMap = Boolean(pk && pk.kind === 'point' && pk.stage === 'map');
  if (onMap && views.place.mode === '3d') setView('place', '2d'); // map points are picked in 2D
  if (viewer) viewer.setPicking(onModel);
  $('#place-map').classList.toggle('picking', onMap);
  if (handles) handles.setEnabled(!onMap);
  $('#pick-banner').hidden = !pk;
  if (!pk) return;
  let text;
  if (pk.kind === 'distance') {
    text = `<b>Scale by distance</b> &middot; Click the ${pk.picks.length ? 'second' : 'first'} point on the <b>3D model</b> `
      + '(snaps to corners). You then type the true distance between them.';
  } else {
    const label = escapeHtml(P().points[pk.index].label);
    text = pk.stage === 'model'
      ? `<b>${label}</b> &middot; Click the point on the <b>3D model</b>. Corners (green), midpoints (purple) and edges (orange) snap.`
      : `<b>${label}</b> &middot; Now click the <b>same point on the map</b>. Zoom in close for precision.`;
  }
  $('#pick-text').innerHTML = text;
  updateViewerMarkers();
}

function finishPicking() {
  const pk = state.picking;
  state.picking = null;
  applyPickingMode();
  if (pk && pk.kind === 'point') {
    state.placeTouched = true;
    changed(0);
  }
}

export function cancelPicking(silent = false) {
  const pk = state.picking;
  if (!pk) return;
  state.picking = null;
  if (pk.kind === 'point') {
    const p = P().points[pk.index];
    if (pk.isNew && p && !p.local && !p.target) {
      P().points.splice(pk.index, 1);
      if (!silent) history.discard();
    } else if (!pk.changed && !silent) {
      history.discard();
    }
  }
  applyPickingMode();
  if (state.step === 'place') {
    renderPoints();
    updateViewerMarkers();
    if (pk.kind === 'point' && pk.changed) changed(0);
  }
}

function onModelPick(hit) {
  const pk = state.picking;
  if (!pk) return;
  const localPoint = hit.world.map((v) => v / unitMetres());
  if (pk.kind === 'distance') {
    pk.picks.push(localPoint);
    if (pk.picks.length < 2) {
      applyPickingMode();
      return;
    }
    finishDistance(pk.picks[0], pk.picks[1]);
    return;
  }
  if (pk.stage !== 'model') return;
  const p = P().points[pk.index];
  p.local = localPoint;
  pk.changed = true;
  if (p.hSource === 'terrain' && p.terrain != null) p.target[2] = (p.terrain + hit.world[2]) / crsUnitMetres();
  if (!hasTarget(p)) {
    pk.stage = 'map';
    applyPickingMode();
    renderPoints();
    updateViewerMarkers();
  } else {
    finishPicking();
  }
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
  if (!pk || pk.kind !== 'point' || pk.stage !== 'map') return;
  const p = P().points[pk.index];
  try {
    await setTargetFromLatLng(p, e.latlng);
    pk.changed = true;
    if (!hasLocal(p) && modelLoaded) {
      pk.stage = 'model';
      applyPickingMode();
      renderPoints();
    } else {
      finishPicking();
    }
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
    if (state.step === 'place') changed(0);
  } catch (e) {
    /* height stays empty; the user can type it */
  }
}

/* ===== manual placement: by hand, drag, rotate, scale ================================================== */

/** R1: put the model at the centre of the map view, no control points needed. */
async function placeByHand() {
  cancelPicking();
  if (!state.crs) return;
  let lat;
  let lon;
  if (views.place.mode === '2d' || !state.location) {
    const c = placeMap.map.getCenter();
    lat = c.lat;
    lon = c.lng;
  } else {
    ({ lat, lon } = state.location);
  }
  let grid;
  try {
    grid = (await api.toGrid(state.crs.code, [{ lat, lon }])).points[0];
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (!grid) {
    toast('This location cannot be expressed in the selected coordinate system.', 'error');
    return;
  }
  let ground = null;
  if (CONFIG.elevationLookup) {
    try { ground = (await api.elevation(lat, lon)).height_m; } catch (e) { ground = null; }
  }
  commit(() => {
    Object.assign(P(), {
      anchor: { local: pivotLocal(), map: [grid.e, grid.n] }, rotation: 0,
      scale: { mode: 'fixed', value: null, source: 'units' }, useSite: false,
    });
    if (ground != null) {
      P().height = ground / crsUnitMetres();
      P().heightSource = 'ground';
    }
  });
  state.mapFitted = true; // keep the view where the user put the model
  toast('Model placed at the centre of the map. Drag the footprint to move it and the round handle to rotate it.');
}

function distanceKm(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const a = Math.sin(((lat2 - lat1) * rad) / 2) ** 2
    + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(a));
}

/** The user moved the building location on step 2 far from the IfcSite: do not place it at the site. */
function skipFarSite() {
  const loc = state.location;
  if (state.placeTouched || state.prefilledFor || !P().useSite || !siteHasLocation() || !loc || loc.source === 'ifc') return;
  const s = site();
  if (distanceKm(s.latitude, s.longitude, loc.lat, loc.lon) < 2) return;
  P().useSite = false;
  if (placeMap) placeMap.map.setView([loc.lat, loc.lon], 18);
  toast('The location stored in the IFC file is far from the building location you set, so it is not used. '
    + 'Click “Place on map” to put the model there.', 'info', 9000);
}

function dragStart(kind, merc) {
  const sol = current();
  const gm = mercator();
  if (!sol || !gm) return;
  // Moving keeps an existing pin; rotating and scaling turn about the footprint centre.
  const pivot = pivotLocal();
  const anchor0 = kind === 'move' && P().anchor ? JSON.parse(JSON.stringify(P().anchor))
    : { local: [pivot[0], pivot[1]], map: mapOf(pivot) };
  drag = {
    kind, before: snapshot(), moved: false, merc0: merc, anchor0, theta0: sol.rotation_deg, scale0: sol.scale,
    pivotMerc: gm.toMerc(anchor0.map), grid0: gm.toGrid(merc),
  };
}

function dragMove(kind, merc, { shift }) {
  const gm = mercator();
  if (!drag || !gm) return;
  drag.moved = true;
  P().anchor = JSON.parse(JSON.stringify(drag.anchor0));
  if (kind === 'move') {
    const g = gm.toGrid(merc);
    P().anchor.map = [drag.anchor0.map[0] + g[0] - drag.grid0[0], drag.anchor0.map[1] + g[1] - drag.grid0[1]];
  } else if (kind === 'rotate') {
    const a0 = Math.atan2(drag.merc0[1] - drag.pivotMerc[1], drag.merc0[0] - drag.pivotMerc[0]);
    const a1 = Math.atan2(merc[1] - drag.pivotMerc[1], merc[0] - drag.pivotMerc[0]);
    let theta = drag.theta0 + ((a1 - a0) * 180) / Math.PI;
    if (shift) theta = engine.ccwFromCw(Math.round(engine.cwFromCcw(theta) / 15) * 15);
    P().rotation = engine.normDeg(theta);
  } else if (kind === 'scale') {
    const d0 = Math.hypot(drag.merc0[0] - drag.pivotMerc[0], drag.merc0[1] - drag.pivotMerc[1]);
    const d1 = Math.hypot(merc[0] - drag.pivotMerc[0], merc[1] - drag.pivotMerc[1]);
    if (d0 > 0) P().scale = { mode: 'fixed', value: (drag.scale0 * d1) / d0, source: 'manual' };
  }
  live = localSolve();
  renderPreview();
  renderResultPanel();
  renderControls();
}

function dragEnd() {
  if (!drag) return;
  const { moved, before } = drag;
  drag = null;
  if (!moved) return; // a click (or the first half of a double-click) changes nothing
  history.push(before);
  state.placeTouched = true;
  changed(250); // server solve ~250 ms after the last change
}

function dragCancel() {
  if (!drag) return;
  state.place = drag.before;
  drag = null;
  live = localSolve();
  renderAll();
}

async function openAngleDialog() {
  const sol = current();
  if (!sol) return;
  const value = await promptNumber({
    title: 'Rotate model', label: 'Rotation, clockwise', value: fixed(sol.rotation_cw_deg, 2), suffix: '°',
    hint: '0° = the model as authored (+X pointing east). Positive turns clockwise. Enter applies, Esc cancels.',
  });
  if (value == null) return;
  commit(() => {
    pinAt(pivotLocal());
    P().rotation = engine.ccwFromCw(Math.round(value * 100) / 100);
  });
}

function nudge(direction) {
  const sol = current();
  if (!sol) return;
  const step = Number($('#nudge-step').value);
  const distance = step / crsUnitMetres();
  commit(() => {
    if (direction === 'cw' || direction === 'ccw') {
      pinAt(pivotLocal());
      const delta = ROTATION_STEPS[step] || step;
      P().rotation = engine.normDeg(direction === 'cw' ? sol.rotation_deg - delta : sol.rotation_deg + delta);
    } else {
      ensurePin();
      const a = P().anchor.map;
      if (direction === 'n') a[1] += distance;
      if (direction === 's') a[1] -= distance;
      if (direction === 'e') a[0] += distance;
      if (direction === 'w') a[0] -= distance;
    }
  });
}

function stepHeight(sign) {
  const sol = current();
  if (!sol) return;
  const step = Number($('#nudge-step').value) / crsUnitMetres();
  commit(() => {
    P().height = sol.orthogonal_height + sign * step;
    P().heightSource = 'manual';
  });
}

function placementLatLng() {
  const sol = current();
  const gm = mercator();
  if (sol && gm) return gm.toLatLng(engine.apply(sol, [...pivotLocal(), 0]));
  return state.location ? [state.location.lat, state.location.lon] : null;
}

async function useGroundHeight() {
  const ll = placementLatLng();
  if (!ll) return;
  try {
    const res = await api.elevation(ll[0], ll[1]);
    if (res.height_m == null) {
      toast('No ground height is available for this location.', 'error');
      return;
    }
    commit(() => {
      P().height = res.height_m / crsUnitMetres();
      P().heightSource = 'ground';
    });
    toast(`Ground height ${fixed(res.height_m, 2)} m (${res.source}), assuming the model's ±0.00 sits on the terrain.`);
  } catch (e) {
    toast(e.message, 'error');
  }
}

function useFileHeight() {
  const s = site();
  const g = state.meta.georef || {};
  let value = null;
  if (g.present && g.orthogonal_height != null) value = g.orthogonal_height;
  else if (s && s.elevation_m != null) value = s.elevation_m / crsUnitMetres();
  if (value == null) {
    toast('The file stores no height for its origin or site.', 'error');
    return;
  }
  commit(() => {
    P().height = value;
    P().heightSource = 'file';
  });
}

/* ===== scale ================================================================================================= */

/** R4: ask before a new operation replaces a deliberately chosen scale by more than 0.1 %. */
async function confirmScale(newValue, newSource) {
  const sol = current();
  if (!sol) return 'new';
  const curSource = P().scale.mode === 'free' ? 'points' : P().scale.source;
  if (curSource === newSource) return 'new';
  const diff = Math.abs(newValue / sol.scale - 1);
  if (diff <= ASK_SCALE_TOLERANCE) return 'new';
  return choose({
    title: 'Which scale do you want to keep?',
    message: `The model already has a scale <b>${SOURCE_NAMES[curSource]}</b>. The new one differs by `
      + `<b>${(diff * 100).toFixed(2)} %</b>.`,
    options: [
      { label: `Keep current: ${Number(sol.scale.toPrecision(8))}`, detail: SOURCE_NAMES[curSource], value: 'keep' },
      { label: `Use new: ${Number(newValue.toPrecision(8))}`, detail: SOURCE_NAMES[newSource], value: 'new', primary: true },
    ],
  });
}

async function setUnitOverride(unit) {
  const newScale = (unit ? engine.UNIT_FACTORS[unit] : unitMetres()) / crsUnitMetres();
  let choice = 'new';
  if (P().scale.source !== 'units' || P().scale.mode === 'free') {
    choice = await confirmScale(newScale, 'units');
    if (choice == null) {
      renderControls();
      return;
    }
  }
  commit(() => {
    P().unitOverride = unit;
    if (choice === 'new') P().scale = { mode: 'fixed', value: null, source: 'units' };
  });
}

async function toggleFreeScale(on) {
  const sol = current();
  if (!sol) return;
  if (on) {
    const r = state.result;
    const pts = [];
    const sitePoint = r && r.points.find((x) => x.source === 'site');
    if (P().useSite && sitePoint) pts.push(sitePoint);
    P().points.filter(complete).forEach((x) => pts.push({ label: x.label, local: [x.local[0], x.local[1], x.local[2] || 0], target: x.target }));
    const fitted = engine.solve(pts, {
      unitScale: unitScaleNow(), rotationFallbackDeg: r.client.rotation_fallback_deg, crsUnitM: crsUnitMetres(),
      scaleMode: 'free', scaleValue: sol.scale, rotationDeg: P().rotation, anchor: P().anchor,
    });
    if (!fitted || fitted.scale_source !== 'points') {
      toast('Fitting the scale needs at least 2 control points that are well apart.', 'error');
      renderControls();
      return;
    }
    if (await confirmScale(fitted.scale, 'points') !== 'new') {
      renderControls(); // "Keep current": the points still fit position and rotation with this scale
      return;
    }
    commit(() => { P().scale = { mode: 'free', value: sol.scale, source: 'points' }; });
  } else {
    const choice = await confirmScale(unitScaleNow(), 'units');
    if (choice == null) {
      renderControls();
      return;
    }
    commit(() => {
      P().scale = choice === 'new' ? { mode: 'fixed', value: null, source: 'units' }
        : { mode: 'fixed', value: sol.scale, source: 'points' };
    });
  }
}

async function finishDistance(p1, p2) {
  state.picking = null;
  applyPickingMode();
  const sol = current();
  if (!sol) return;
  const dFile = Math.hypot(p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2]);
  if (!(dFile > 0)) {
    toast('The two points are at the same place. Pick two different points.', 'error');
    return;
  }
  const measured = dFile * sol.scale * crsUnitMetres();
  const distance = await promptNumber({
    title: 'Scale by distance', label: 'True distance between the two points', value: fixed(measured, 3), suffix: 'm',
    hint: `Model distance: ${fixed(dFile, localDigits())} ${state.meta.unit.symbol}. The first point stays where it is and the rotation is kept.`,
    min: 0,
  });
  updateViewerMarkers();
  if (distance == null) return;
  const scale = engine.scaleFromDistance(p1, p2, distance, crsUnitMetres());
  if (await confirmScale(scale, 'distance') !== 'new') return;
  commit(() => {
    const theta = current().rotation_deg;
    pinAt([p1[0], p1[1]]);
    P().rotation = theta;
    P().scale = { mode: 'fixed', value: scale, source: 'distance' };
  });
}

/* ===== solve ================================================================================================= */

function scheduleSolve(delay = 250) {
  clearTimeout(solveTimer);
  solveTimer = setTimeout(runSolve, delay);
}

async function runSolve() {
  if (!state.crs || !state.meta || state.step !== 'place') return;
  const seq = ++solveSeq;
  const p = P();
  const hasInput = (p.useSite && siteHasLocation()) || p.points.some(complete) || p.anchor;
  if (!hasInput) {
    state.result = null;
    state.solveError = null;
    live = null;
  } else {
    try {
      const result = await api.solve(state.jobId, payload());
      if (seq !== solveSeq || drag) return;
      state.result = result;
      state.solveError = null;
      live = null;
    } catch (e) {
      if (seq !== solveSeq) return;
      state.result = null;
      state.solveError = e.message;
      live = null;
    }
  }
  renderAll();
  hooks.save();
}

/* ===== rendering ============================================================================================== */

function renderAll() {
  renderPoints();
  renderChips();
  renderResultPanel();
  renderControls();
  renderPreview();
  updateViewerMarkers();
  $('#undo-btn').disabled = !history.canUndo;
  $('#redo-btn').disabled = !history.canRedo;
}

function sourceText(value) {
  return { points: 'from points', true_north: 'from file north', manual: 'manual', units: 'file units',
    default: 'not set yet', ground: 'ground', file: 'from file' }[value] || value || '';
}

function renderChips() {
  const p = P();
  const sol = current();
  const chips = [];
  if (p.rotation != null) {
    const fit = sol && sol.can_fit_rotation;
    chips.push(`<span class="chip-manual">Manual rotation is on (${fixed(engine.cwFromCcw(p.rotation), 2)}°)
      <button type="button" data-chip="rotation">${fit ? 'Use fitted rotation' : 'Use file north'}</button></span>`);
  }
  if (p.anchor && usableCount() > 0) {
    chips.push('<span class="chip-manual">Manual position is on <button type="button" data-chip="position">Use fitted position</button></span>');
  }
  $('#manual-chips').innerHTML = chips.join('');
}

function renderResultPanel() {
  const stats = $('#solution-stats');
  const warnings = $('#solution-warnings');
  const sol = current();
  const unit = crsUnitSymbol();
  if (!sol) {
    stats.innerHTML = `<dt>Status</dt><dd>${escapeHtml(state.solveError
      || 'Place the model by hand, add a control point, or use the location stored in the IFC file.')}</dd>`;
    warnings.innerHTML = '';
    $('#place-next').disabled = true;
    $('#allow-outside-row').hidden = true;
    return;
  }
  const heightSource = P().height != null && P().heightSource !== 'manual' ? P().heightSource : sol.height_source;
  let accuracy;
  if (!sol.residuals || !sol.residuals.length) accuracy = '<dt>Accuracy</dt><dd>Placed by hand, no accuracy figures</dd>';
  else if (sol.residuals.length < 2) accuracy = '';
  else accuracy = `<dt>Accuracy</dt><dd>${fixed(sol.rms_horizontal_m, 3)} m</dd>`;
  stats.innerHTML = `
    <dt>E / N</dt><dd>${fixed(sol.eastings, 3)} / ${fixed(sol.northings, 3)} ${unit}</dd>
    <dt>Height</dt><dd>${fixed(sol.orthogonal_height, 3)} ${unit} <small>${escapeHtml(sourceText(heightSource))}</small></dd>
    <dt>Rotation</dt><dd>${fixed(sol.rotation_cw_deg, 2)}° clockwise <small>${escapeHtml(sourceText(sol.rotation_source))}</small></dd>
    <dt>Scale</dt><dd>${Number(sol.scale.toPrecision(10))} <small>${escapeHtml(scaleLabel(sol))}</small></dd>
    ${accuracy}`;
  const server = state.result && state.result.solution;
  const list = server ? server.warnings : [];
  warnings.innerHTML = list.map((w) => `<li class="${w.level}">${escapeHtml(w.message)}</li>`).join('');
  const outside = list.some((w) => w.code === 'outside_area');
  $('#allow-outside-row').hidden = !outside;
  $('#allow-outside').checked = state.allowOutside;
  $('#place-next').disabled = !(state.result && state.result.can_save) || Boolean(live);
}

function renderControls() {
  const sol = current();
  const p = P();
  const set = (sel, value) => { if (document.activeElement !== $(sel)) $(sel).value = value; };
  set('#rotation-input', sol ? fixed(sol.rotation_cw_deg, 2) : '');
  set('#height-input', sol ? fixed(sol.orthogonal_height, 3) : '');
  set('#scale-input', sol ? String(Number(sol.scale.toPrecision(10))) : '');
  $('#rotation-auto').disabled = p.rotation == null;
  $('#height-auto').disabled = p.height == null;
  $('#rotation-input').classList.toggle('manual', p.rotation != null);
  $('#height-input').classList.toggle('manual', p.height != null);
  $('#unit-override').value = p.unitOverride || '';
  $('#free-scale').checked = p.scale.mode === 'free';
  $('#scale-value').textContent = sol ? String(Number(sol.scale.toPrecision(10))) : '–';
  $('#scale-source').textContent = sol ? scaleLabel(sol) : '';
  const notes = {
    ground: 'Ground height at the model position.',
    file: 'Height stored in the file.',
    manual: 'Set by hand.',
    auto: 'From the heights of the control points.',
  };
  $('#height-note').textContent = `${notes[p.height != null ? p.heightSource : 'auto'] || ''} `
    + 'Ground heights assume the model’s ±0.00 level sits on the terrain.';
  $('#footprint-all').checked = p.footprintMode === 'all';
}

/** Footprint, frame, handles, axes and pins, from the current (live or server) solution. */
function renderPreview() {
  if (!placeMap) return;
  pointLayer.clearLayers();
  axesLayer.clearLayers();
  const sol = current();
  const c = ctx();
  const gm = mercator();
  if (!sol || !c || !gm || !gm.ok) {
    handles.set(null);
    return;
  }
  const toLL = (xyz) => gm.toLatLng(engine.apply(sol, xyz));
  const fp = c.footprint_local;
  let geo = null;
  if (fp && fp.length > 2) {
    const xs = fp.map((q) => q[0]);
    const ys = fp.map((q) => q[1]);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const reach = Math.max(x1 - x0, y1 - y0) * 0.22;
    geo = {
      footprint: fp.map((q) => toLL([q[0], q[1], 0])),
      frame: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((q) => toLL([q[0], q[1], 0])),
      frameTop: toLL([(x0 + x1) / 2, y1, 0]),
      rotate: toLL([(x0 + x1) / 2, y1 + reach, 0]),
      fillOpacity: state.fillOpacity,
    };
  }
  handles.set(geo);

  const axis = state.result.preview.axis_local;
  const origin = toLL([0, 0, 0]);
  L.polyline([origin, toLL([axis, 0, 0])], { color: '#dc2626', weight: 3, interactive: false }).addTo(axesLayer);
  L.polyline([origin, toLL([0, axis, 0])], { color: '#16a34a', weight: 3, interactive: false }).addTo(axesLayer);
  L.circleMarker(origin, { radius: 5, color: '#111827', fillColor: '#fff', fillOpacity: 1, weight: 2 })
    .bindTooltip('Model origin (0, 0, 0)').addTo(axesLayer);

  const pins = [];
  const sitePoint = state.result.points.find((x) => x.source === 'site');
  if (P().useSite && sitePoint) pins.push({ ...sitePoint, isSite: true });
  P().points.filter(complete).forEach((x) => pins.push({ label: x.label, local: x.local, target: x.target, point: x }));
  for (const pin of pins) {
    const target = gm.toLatLng(pin.target);
    const fitted = toLL([pin.local[0], pin.local[1], pin.local[2] || 0]);
    L.polyline([target, fitted], { color: '#dc2626', weight: 1.5, dashArray: '4 3', interactive: false }).addTo(pointLayer);
    L.circleMarker(fitted, { radius: 4, color: '#111827', weight: 1.5, fillOpacity: 0, interactive: false }).addTo(pointLayer);
    const short = pin.isSite ? 'S' : pin.label.replace(/^P/, '').slice(0, 2);
    const marker = L.marker(target, { icon: pinIcon(escapeHtml(short), pin.isSite ? 'site' : ''), draggable: !pin.isSite });
    marker.bindTooltip(pin.isSite ? 'IFC site location' : `${pin.label} – drag to adjust`);
    if (!pin.isSite) {
      let before = null;
      marker.on('dragstart', () => { before = snapshot(); });
      marker.on('dragend', async () => {
        try {
          history.push(before);
          await setTargetFromLatLng(pin.point, marker.getLatLng());
          state.placeTouched = true;
          changed(0);
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
  if (!placeMap || (state.mapFitted && !force)) return;
  state.mapFitted = true;
  placeMap.map.fitBounds(r.preview.bounds, { padding: [60, 60], maxZoom: 20 });
}

function setCadastral(on, remember = true) {
  if (!placeMap) return;
  if (!cadastral) cadastral = cadastralLayer();
  if (on && !placeMap.map.hasLayer(cadastral)) cadastral.addTo(placeMap.map);
  if (!on && placeMap.map.hasLayer(cadastral)) placeMap.map.removeLayer(cadastral);
  if (remember) prefs.set('georef.cadastral', on);
}

/* ===== 3D map views (placement and save steps) =================================================================== */

function map3dPlacement() {
  const sol = current();
  const r = state.result;
  const gm = mercator();
  const m3 = r && r.preview ? r.preview.map3d : null;
  if (!sol || !m3 || !gm || !gm.ok) return m3;
  const anchor = gm.toLatLng(engine.apply(sol, [m3.anchor_local[0], m3.anchor_local[1], 0]));
  return {
    anchor, heading_deg: sol.rotation_deg + gm.gammaDeg, scale_ratio: sol.scale / declaredUnitScale(),
    base_z_m: m3.base_z_m,
  };
}

export async function setView(target, mode) {
  const view = views[target];
  view.mode = mode;
  $$(`.seg-btn[data-target="${target}"]`).forEach((b) => b.classList.toggle('active', b.dataset.view === mode));
  $(`#${target}-map`).hidden = mode === '3d';
  $(`#${target}-map3d`).hidden = mode !== '3d';
  $(`#${target}-map3d-note`).hidden = mode !== '3d';
  if (target === 'place' && handles) handles.setEnabled(mode === '2d');
  if (mode !== '3d') return;
  if (target === 'place' && state.picking && state.picking.kind === 'point' && state.picking.stage === 'map') cancelPicking();
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

export async function update3d(target) {
  const view = views[target];
  if (!view.map3d || view.mode !== '3d') return;
  const placement = map3dPlacement();
  const note = $(`#${target}-map3d-note`);
  const sol = current();
  if (!placement || !state.geometry || state.geometry.empty || !sol) {
    note.textContent = state.geometry && state.geometry.empty
      ? 'This file has no 3D geometry to show.' : 'The 3D model appears here once it is placed.';
    return;
  }
  note.textContent = 'The model stands on the map surface. Height of the model origin stored in the file: '
    + `${fixed(sol.orthogonal_height, 2)} ${crsUnitSymbol()} (EPSG:${state.crs.code}).`;
  try {
    await view.map3d.setModel(modelUrl(state.jobId));
  } catch (e) {
    note.textContent = `The 3D model could not be loaded: ${e.message}`;
    return;
  }
  view.map3d.setPlacement(placement, state.result.preview.bounds);
}
