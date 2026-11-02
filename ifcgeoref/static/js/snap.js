/**
 * Spatial index over the model's feature edges (edges.bin: float32 x1 y1 z1 x2 y2 z2,
 * glTF frame) so a snap search only looks at edges near the cursor's hit point.
 */
export class EdgeIndex {
  constructor(segments) {
    this.seg = segments;
    this.count = Math.floor(segments.length / 6);
    let min = [Infinity, Infinity, Infinity];
    let max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < this.count * 6; i += 3) {
      for (let k = 0; k < 3; k += 1) {
        const v = segments[i + k];
        if (v < min[k]) min[k] = v;
        if (v > max[k]) max[k] = v;
      }
    }
    const size = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2], 1);
    this.cell = Math.min(Math.max(size / 96, 0.05), 2);
    this.cells = new Map();
    this.stamp = new Uint32Array(this.count);
    this.tick = 0;
    for (let i = 0; i < this.count; i += 1) this.insert(i);
  }

  key(x, y, z) {
    return `${Math.floor(x / this.cell)},${Math.floor(y / this.cell)},${Math.floor(z / this.cell)}`;
  }

  insert(i) {
    const s = this.seg;
    const o = i * 6;
    const len = Math.hypot(s[o + 3] - s[o], s[o + 4] - s[o + 1], s[o + 5] - s[o + 2]);
    const steps = Math.max(1, Math.ceil(len / (this.cell * 0.5)));
    let last = null;
    for (let k = 0; k <= steps; k += 1) {
      const t = k / steps;
      const key = this.key(s[o] + (s[o + 3] - s[o]) * t, s[o + 1] + (s[o + 4] - s[o + 1]) * t, s[o + 2] + (s[o + 5] - s[o + 2]) * t);
      if (key === last) continue;
      last = key;
      let list = this.cells.get(key);
      if (!list) { list = []; this.cells.set(key, list); }
      if (list[list.length - 1] !== i) list.push(i);
    }
  }

  /** Call fn(index) once for each segment that may lie within r of p. */
  query(p, r, fn) {
    this.tick += 1;
    if (this.tick > 0xfffffff0) { this.stamp.fill(0); this.tick = 1; }
    const c = this.cell;
    const x0 = Math.floor((p.x - r) / c); const x1 = Math.floor((p.x + r) / c);
    const y0 = Math.floor((p.y - r) / c); const y1 = Math.floor((p.y + r) / c);
    const z0 = Math.floor((p.z - r) / c); const z1 = Math.floor((p.z + r) / c);
    if ((x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1) > 4096) return; // radius far too large: skip
    for (let x = x0; x <= x1; x += 1) {
      for (let y = y0; y <= y1; y += 1) {
        for (let z = z0; z <= z1; z += 1) {
          const list = this.cells.get(`${x},${y},${z}`);
          if (!list) continue;
          for (const i of list) {
            if (this.stamp[i] === this.tick) continue;
            this.stamp[i] = this.tick;
            fn(i);
          }
        }
      }
    }
  }
}
