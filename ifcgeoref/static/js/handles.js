import { latLngToMerc } from './engine.js';

const L = window.L;

/**
 * Direct manipulation of the placed footprint on the Leaflet map (like the GeoDataHub
 * 2D tool): drag the footprint to move, drag the round handle to rotate (Shift snaps to
 * 15°, double-click types an angle), drag the corner handles to scale (when unlocked).
 * Positions are reported in Web Mercator metres; Esc cancels a drag in progress.
 */
export class PlacementHandles {
  constructor(map, callbacks) {
    this.map = map;
    this.cb = callbacks;
    this.enabled = true;
    this.scaleEnabled = false;
    this.active = null;
    this.group = L.layerGroup().addTo(map);

    this.frame = L.polygon([], { color: '#111827', weight: 1, dashArray: '4 4', fill: false, interactive: false });
    this.stem = L.polyline([], { color: '#111827', weight: 1.5, interactive: false });
    this.footprint = L.polygon([], {
      color: '#93a70a', weight: 2, fillColor: '#b0c70d', fillOpacity: 0.28, className: 'fp-move',
      bubblingMouseEvents: false,
    });
    this.rotate = L.marker([0, 0], {
      icon: L.divIcon({ className: '', html: '<div class="handle rotate" title="Drag to rotate · Shift = 15° steps · double-click to type an angle">&#10227;</div>',
        iconSize: [24, 24], iconAnchor: [12, 12] }),
      keyboard: false, bubblingMouseEvents: false, zIndexOffset: 1000,
    });
    this.corners = [0, 1, 2, 3].map(() => L.marker([0, 0], {
      icon: L.divIcon({ className: '', html: '<div class="handle corner" title="Drag to scale"></div>', iconSize: [12, 12], iconAnchor: [6, 6] }),
      keyboard: false, bubblingMouseEvents: false, zIndexOffset: 900,
    }));

    this.footprint.on('mousedown', (e) => this.start('move', e));
    this.rotate.on('mousedown', (e) => this.start('rotate', e));
    this.rotate.on('dblclick', (e) => {
      L.DomEvent.stop(e);
      if (this.cb.onRotateDblClick) this.cb.onRotateDblClick();
    });
    this.corners.forEach((m) => m.on('mousedown', (e) => this.start('scale', e)));

    this.onDocMove = (e) => this.move(e);
    this.onDocUp = (e) => this.end(e);
    this.onKey = (e) => {
      if (e.key === 'Escape' && this.active) {
        e.preventDefault();
        e.stopImmediatePropagation();
        this.cancel();
      }
    };
  }

  /** geo: { footprint, frame (4 corners), frameTop, rotate, fillOpacity } as [lat, lng] */
  set(geo) {
    this.geo = geo;
    if (!geo || !this.enabled) {
      this.group.clearLayers();
      return;
    }
    this.footprint.setLatLngs(geo.footprint);
    this.footprint.setStyle({ fillOpacity: geo.fillOpacity });
    this.frame.setLatLngs(geo.frame);
    this.stem.setLatLngs([geo.frameTop, geo.rotate]);
    this.rotate.setLatLng(geo.rotate);
    this.corners.forEach((m, i) => m.setLatLng(geo.frame[i]));
    const wanted = [this.frame, this.stem, this.footprint, this.rotate, ...(this.scaleEnabled ? this.corners : [])];
    for (const layer of [this.frame, this.stem, this.footprint, this.rotate, ...this.corners]) {
      const on = wanted.includes(layer);
      if (on && !this.group.hasLayer(layer)) this.group.addLayer(layer);
      if (!on && this.group.hasLayer(layer)) this.group.removeLayer(layer);
    }
  }

  setEnabled(on) {
    this.enabled = Boolean(on);
    if (!this.enabled && this.active) this.cancel();
    this.set(this.geo);
  }

  setScaleEnabled(on) {
    this.scaleEnabled = Boolean(on);
    this.set(this.geo);
  }

  mercOf(event) {
    const latlng = this.map.mouseEventToLatLng(event);
    return latLngToMerc(latlng.lat, latlng.lng);
  }

  start(kind, e) {
    if (!this.enabled || this.active || (e.originalEvent && e.originalEvent.button !== 0)) return;
    L.DomEvent.stop(e.originalEvent);
    this.map.dragging.disable(); // the footprint takes priority over panning
    this.active = kind;
    this.map.getContainer().classList.add(`handle-${kind}`);
    document.addEventListener('mousemove', this.onDocMove);
    document.addEventListener('mouseup', this.onDocUp);
    document.addEventListener('keydown', this.onKey, true);
    this.cb.onStart(kind, this.mercOf(e.originalEvent));
  }

  move(e) {
    if (!this.active) return;
    e.preventDefault();
    this.cb.onMove(this.active, this.mercOf(e), { shift: e.shiftKey });
  }

  finish() {
    document.removeEventListener('mousemove', this.onDocMove);
    document.removeEventListener('mouseup', this.onDocUp);
    document.removeEventListener('keydown', this.onKey, true);
    this.map.getContainer().classList.remove(`handle-${this.active}`);
    this.map.dragging.enable();
    const kind = this.active;
    this.active = null;
    return kind;
  }

  end() {
    if (!this.active) return;
    this.cb.onEnd(this.finish());
  }

  cancel() {
    if (!this.active) return;
    this.cb.onCancel(this.finish());
  }
}
