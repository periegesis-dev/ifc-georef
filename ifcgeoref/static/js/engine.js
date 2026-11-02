/**
 * Client-side copy of solver.py, for an instant preview while the user drags.
 * The server's answer always replaces these values; keep both files in step.
 *
 *   E = scale * (cos r * x - sin r * y) + eastings
 *   N = scale * (sin r * x + cos r * y) + northings
 *   H = scale * z + orthogonal_height
 *
 * r (rotation_deg) is anticlockwise from grid east, as written to IfcMapConversion.
 * The UI shows the clockwise angle (cwFromCcw), like the GeoDataHub 2D tool.
 */
const MIN_ROTATION_BASE_M = 1.0;
const EARTH_RADIUS = 6378137;

export const UNIT_FACTORS = { mm: 0.001, cm: 0.01, m: 1, in: 0.0254, ft: 0.3048 };

export function normDeg(deg) {
  let d = deg % 360;
  if (d <= -180) d += 360;
  else if (d > 180) d -= 360;
  return d;
}

export const cwFromCcw = (ccw) => normDeg(-ccw);
export const ccwFromCw = (cw) => normDeg(-cw);

function spreadM(points, unitScale, crsUnitM) {
  if (points.length < 2) return 0;
  const cx = points.reduce((s, p) => s + p.local[0], 0) / points.length;
  const cy = points.reduce((s, p) => s + p.local[1], 0) / points.length;
  let max = 0;
  for (const p of points) max = Math.max(max, Math.hypot(p.local[0] - cx, p.local[1] - cy));
  return max * 2 * unitScale * crsUnitM;
}

/** Same inputs and results as solver.solve (no warnings). Angles in degrees. */
export function solve(points, opts) {
  const {
    unitScale, rotationFallbackDeg, crsUnitM = 1, scaleMode = 'fixed', scaleValue = null,
    scaleSource = null, rotationDeg = null, height = null, anchor = null,
  } = opts;
  if (!points.length && !anchor) return null;
  const n = points.length;
  let clx = 0; let cly = 0; let ctx = 0; let cty = 0;
  if (n) {
    for (const p of points) { clx += p.local[0]; cly += p.local[1]; ctx += p.target[0]; cty += p.target[1]; }
    clx /= n; cly /= n; ctx /= n; cty /= n;
  }
  const P = points.map((p) => [p.local[0] - clx, p.local[1] - cly]);
  const Q = points.map((p) => [p.target[0] - ctx, p.target[1] - cty]);
  const canFit = n >= 2 && spreadM(points, unitScale, crsUnitM) >= MIN_ROTATION_BASE_M;

  let rot;
  let rotationSource;
  if (rotationDeg != null) { rot = rotationDeg * Math.PI / 180; rotationSource = 'manual'; } else if (canFit) {
    let a = 0; let b = 0;
    P.forEach((p, i) => { a += p[0] * Q[i][0] + p[1] * Q[i][1]; b += p[0] * Q[i][1] - p[1] * Q[i][0]; });
    rot = Math.atan2(b, a); rotationSource = 'points';
  } else { rot = rotationFallbackDeg * Math.PI / 180; rotationSource = 'true_north'; }

  let scale = scaleValue || unitScale;
  let source = scaleSource || (scaleValue ? 'manual' : 'units');
  if (scaleMode === 'free' && canFit) {
    const c = Math.cos(rot); const s = Math.sin(rot);
    let denom = 0; let numer = 0;
    P.forEach((p, i) => {
      denom += p[0] * p[0] + p[1] * p[1];
      numer += c * (p[0] * Q[i][0] + p[1] * Q[i][1]) + s * (p[0] * Q[i][1] - p[1] * Q[i][0]);
    });
    if (denom > 0 && numer > 0) { scale = numer / denom; source = 'points'; }
  }

  const c = Math.cos(rot); const s = Math.sin(rot);
  let e0; let n0;
  if (anchor) {
    const [ax, ay] = anchor.local;
    e0 = anchor.map[0] - scale * (c * ax - s * ay);
    n0 = anchor.map[1] - scale * (s * ax + c * ay);
  } else {
    e0 = ctx - scale * (c * clx - s * cly);
    n0 = cty - scale * (s * clx + c * cly);
  }

  const heights = points.filter((p) => p.target.length > 2 && p.target[2] != null);
  let h0;
  let heightSource;
  if (height != null) { h0 = height; heightSource = 'manual'; } else if (heights.length) {
    h0 = heights.reduce((sum, p) => sum + (p.target[2] - scale * (p.local[2] || 0)), 0) / heights.length;
    heightSource = 'points';
  } else { h0 = 0; heightSource = 'default'; }

  const deg = normDeg(rot * 180 / Math.PI);
  const residuals = points.map((p) => {
    const fe = scale * (c * p.local[0] - s * p.local[1]) + e0;
    const fn = scale * (s * p.local[0] + c * p.local[1]) + n0;
    const dx = (fe - p.target[0]) * crsUnitM; const dy = (fn - p.target[1]) * crsUnitM;
    return { label: p.label, dx, dy, horizontal: Math.hypot(dx, dy) };
  });
  const rms = residuals.length
    ? Math.sqrt(residuals.reduce((sum, r) => sum + r.horizontal ** 2, 0) / residuals.length) : null;
  return {
    eastings: e0, northings: n0, orthogonal_height: h0, rotation_deg: deg, rotation_cw_deg: cwFromCcw(deg),
    x_axis_abscissa: Math.cos(deg * Math.PI / 180), x_axis_ordinate: Math.sin(deg * Math.PI / 180), scale,
    rotation_source: rotationSource, scale_source: source, height_source: heightSource,
    position_source: anchor ? 'manual' : 'points', can_fit_rotation: canFit, residuals, rms_horizontal_m: rms,
  };
}

export function apply(sol, [x, y, z = 0]) {
  const c = sol.x_axis_abscissa; const s = sol.x_axis_ordinate; const k = sol.scale;
  return [k * (c * x - s * y) + sol.eastings, k * (s * x + c * y) + sol.northings, k * z + sol.orthogonal_height];
}

export function scaleFromDistance(p1, p2, distanceM, crsUnitM) {
  const d = Math.hypot(p2[0] - p1[0], p2[1] - p1[1], (p2[2] || 0) - (p1[2] || 0));
  if (!(d > 0) || !(distanceM > 0)) return null;
  return (distanceM / crsUnitM) / d;
}

/* ---- map grid <-> Web Mercator <-> lat/lon ------------------------------------------ */

export class GridMercator {
  /** aff = client.grid_to_mercator: merc = ref_merc + J (grid - ref_grid) */
  constructor(aff) {
    this.ok = Boolean(aff);
    if (!aff) return;
    this.rg = aff.ref_grid; this.rm = aff.ref_merc; this.J = aff.jacobian;
    const [[a, b], [c, d]] = this.J;
    const det = a * d - b * c;
    this.inv = [[d / det, -b / det], [-c / det, a / det]];
    this.gammaDeg = Math.atan2(c, a) * 180 / Math.PI; // direction of grid east in Mercator
  }

  toMerc([e, n]) {
    const de = e - this.rg[0]; const dn = n - this.rg[1];
    return [this.rm[0] + this.J[0][0] * de + this.J[0][1] * dn, this.rm[1] + this.J[1][0] * de + this.J[1][1] * dn];
  }

  toGrid([x, y]) {
    const dx = x - this.rm[0]; const dy = y - this.rm[1];
    return [this.rg[0] + this.inv[0][0] * dx + this.inv[0][1] * dy, this.rg[1] + this.inv[1][0] * dx + this.inv[1][1] * dy];
  }

  toLatLng(grid) { return mercToLatLng(this.toMerc(grid)); }

  fromLatLng(lat, lng) { return this.toGrid(latLngToMerc(lat, lng)); }
}

export function mercToLatLng([x, y]) {
  return [(2 * Math.atan(Math.exp(y / EARTH_RADIUS)) - Math.PI / 2) * 180 / Math.PI, x / EARTH_RADIUS * 180 / Math.PI];
}

export function latLngToMerc(lat, lng) {
  const clamped = Math.max(Math.min(lat, 85.0511287798), -85.0511287798);
  return [EARTH_RADIUS * lng * Math.PI / 180,
    EARTH_RADIUS * Math.log(Math.tan(Math.PI / 4 + clamped * Math.PI / 360))];
}
