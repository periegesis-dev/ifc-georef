const L = window.L;

const SWISS_BOUNDS = [[45.6, 5.7], [48.1, 10.8]];

export const BASEMAPS = [
  {
    id: 'osm', label: 'Map (OpenStreetMap)',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    options: { maxNativeZoom: 19, maxZoom: 22, attribution: '&copy; OpenStreetMap contributors' },
  },
  {
    id: 'swissimage', label: 'Aerial – Switzerland (swisstopo)',
    url: 'https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current/3857/{z}/{x}/{y}.jpeg',
    options: { maxNativeZoom: 19, maxZoom: 22, bounds: SWISS_BOUNDS, attribution: '&copy; swisstopo' },
  },
  {
    id: 'swisstopo', label: 'Map – Switzerland (swisstopo)',
    url: 'https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg',
    options: { maxNativeZoom: 18, maxZoom: 22, bounds: SWISS_BOUNDS, attribution: '&copy; swisstopo' },
  },
  {
    id: 'esri', label: 'Aerial – worldwide (Esri)',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    options: { maxNativeZoom: 19, maxZoom: 22, attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics' },
  },
];

export function inSwitzerland(lat, lon) {
  return lat != null && lon != null && lat > 45.8 && lat < 47.9 && lon > 5.9 && lon < 10.6;
}

export function defaultBasemap(lat, lon) {
  return inSwitzerland(lat, lon) ? 'swissimage' : 'esri';
}

/** Leaflet map with switchable base layers. */
export function createMap(element, { basemap = 'osm', center = [46.8, 8.2], zoom = 7, select = null } = {}) {
  const map = L.map(element, { zoomControl: true, attributionControl: true }).setView(center, zoom);
  const layers = {};
  for (const b of BASEMAPS) layers[b.id] = L.tileLayer(b.url, b.options);
  let current = null;

  function setBasemap(id) {
    const next = layers[id] || layers.osm;
    if (current === next) return;
    if (current) map.removeLayer(current);
    next.addTo(map);
    next.bringToBack();
    current = next;
    if (select) select.value = id in layers ? id : 'osm';
  }

  if (select) {
    select.innerHTML = BASEMAPS.map((b) => `<option value="${b.id}">${b.label}</option>`).join('');
    select.addEventListener('change', () => setBasemap(select.value));
  }
  setBasemap(basemap);
  // Leaflet needs a size refresh when its container was hidden while created.
  const observer = new ResizeObserver(() => map.invalidateSize());
  observer.observe(element);
  return { map, setBasemap };
}

/** Swiss cadastral map (parcel boundaries) as a transparent overlay, as used in GeoDataHub. */
export function cadastralLayer() {
  return L.tileLayer.wms('https://wms.geo.admin.ch/', {
    layers: 'ch.kantone.cadastralwebmap-farbe', format: 'image/png', transparent: true, version: '1.3.0',
    maxZoom: 22, attribution: 'Cadastral map &copy; swisstopo, cantons',
  });
}

export function pinIcon(text, kind = '') {
  return L.divIcon({
    className: '',
    html: `<div class="pin ${kind}"><span>${text}</span></div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 29], // tip of the rotated pin
  });
}
