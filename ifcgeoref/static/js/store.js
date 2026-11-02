import { $, storage } from './util.js';

export const CONFIG = window.APP_CONFIG || {};
export const session = storage('session');

/** Everything the placement solve depends on; one snapshot = one undo step. */
export function defaultPlacement() {
  return {
    points: [],
    useSite: false,
    rotation: null,            // manual rotation, degrees anticlockwise from grid east (null = automatic)
    anchor: null,              // manual position: { local: [x, y] file units, map: [E, N] }
    height: null,              // manual origin height in map units (null = automatic)
    heightSource: 'auto',      // auto | manual | ground | file
    scale: { mode: 'fixed', value: null, source: 'units' }, // value null = file units
    unitOverride: null,        // mm | cm | m | in | ft when the file's unit is wrong
    footprintMode: 'building', // building | all (include site / terrain)
  };
}

export const state = {
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
  place: defaultPlacement(),
  placeTouched: false,
  prefilledFor: null,
  allowOutside: false,
  restored: false,
  result: null,
  solveError: null,
  picking: null,
  writtenKey: null,
  uploadedAs: null,
  saveStartedAt: null,
  uploadStartedAt: null,
  mapFitted: false,
  modelProgress: 0,
  fillOpacity: 0.28,
};

let nextPointId = 1;

export function syncPointIds() {
  nextPointId = state.place.points.reduce((m, p) => Math.max(m, p.id || 0), 0) + 1;
}

export function showError(selector, message) {
  const el = $(selector);
  el.textContent = message;
  el.hidden = false;
}

export function hideError(selector) {
  $(selector).hidden = true;
}

export function site() {
  return (state.meta && state.meta.site) || null;
}

export function siteHasLocation() {
  const s = site();
  return Boolean(s && s.has_location);
}

/** Metres per file unit as declared in the file. */
export function unitMetres() {
  return state.meta ? state.meta.unit.metres : 1;
}

export function crsUnitMetres() {
  return (state.crs && state.crs.unit_metres) || 1;
}

export function crsUnitSymbol() {
  const name = ((state.crs && state.crs.unit) || 'metre').toLowerCase();
  if (name.startsWith('metre') || name.startsWith('meter')) return 'm';
  if (name.includes('us survey')) return 'ftUS';
  if (name.includes('foot') || name.includes('feet')) return 'ft';
  return name;
}

export function localDigits() {
  const symbol = state.meta ? state.meta.unit.symbol : 'm';
  return symbol === 'mm' ? 1 : symbol === 'cm' ? 2 : 3;
}

export function complete(p) {
  return Boolean(p.local && p.target && p.local[0] != null && p.local[1] != null
    && p.target[0] != null && p.target[1] != null);
}

export function nextLabel() {
  const used = new Set(state.place.points.map((p) => p.label));
  let n = 1;
  while (used.has(`P${n}`)) n += 1;
  return `P${n}`;
}

export function newPoint(values = {}) {
  return {
    id: nextPointId++, label: values.label || nextLabel(), local: values.local || null,
    target: values.target || null, hSource: values.hSource || null, terrain: null,
  };
}
