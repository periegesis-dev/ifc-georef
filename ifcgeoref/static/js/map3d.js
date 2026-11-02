import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { BASEMAPS } from './map.js';

const MAPLIBRE = 'https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl';
let maplibreLoading = null;

/** MapLibre is only needed for the 3D view, so it is loaded on first use. */
function loadMapLibre() {
  if (window.maplibregl) return Promise.resolve(window.maplibregl);
  if (!maplibreLoading) {
    maplibreLoading = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = `${MAPLIBRE}.css`;
      document.head.appendChild(css);
      const script = document.createElement('script');
      script.src = `${MAPLIBRE}.js`;
      script.onload = () => resolve(window.maplibregl);
      script.onerror = () => {
        maplibreLoading = null;
        reject(new Error('The 3D map library could not be loaded.'));
      };
      document.head.appendChild(script);
    });
  }
  return maplibreLoading;
}

function rasterSource(id) {
  const base = BASEMAPS.find((b) => b.id === id) || BASEMAPS[0];
  const source = {
    type: 'raster', tiles: [base.url], tileSize: 256,
    maxzoom: base.options.maxNativeZoom || 19, attribution: base.options.attribution,
  };
  if (base.options.bounds) {
    const [[south, west], [north, east]] = base.options.bounds;
    source.bounds = [west, south, east, north];
  }
  return source;
}

/**
 * The georeferenced model on a 3D web map.
 * placement = preview.map3d from /solve: { anchor: [lat, lon], heading_deg, scale_ratio, base_z_m }
 * The glTF is Y-up and centred on the anchor; it is stood on the ground (base_z_m).
 */
export class Map3DView {
  constructor(container) {
    this.container = container;
    this.map = null;
    this.placement = null;
    this.modelUrl = null;
    this.framed = false;
  }

  async init(basemap) {
    if (this.map) return;
    const maplibregl = await loadMapLibre();
    this.maplibregl = maplibregl;
    this.map = new maplibregl.Map({
      container: this.container,
      style: { version: 8, sources: { base: rasterSource(basemap) }, layers: [{ id: 'base', type: 'raster', source: 'base' }] },
      center: [8.2, 46.8], zoom: 7, pitch: 60, maxPitch: 80, antialias: true,
    });
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-left');
    await new Promise((resolve) => this.map.once('load', resolve));
    this.map.addLayer(this.modelLayer());
    new ResizeObserver(() => this.map.resize()).observe(this.container);
  }

  modelLayer() {
    const view = this;
    return {
      id: 'ifc-model',
      type: 'custom',
      renderingMode: '3d',
      onAdd(map, gl) {
        view.camera = new THREE.Camera();
        view.scene = new THREE.Scene();
        view.scene.add(new THREE.HemisphereLight(0xffffff, 0x9a9a9a, 2.0));
        const sun = new THREE.DirectionalLight(0xffffff, 1.4);
        sun.position.set(0.5, 1, 0.3);
        view.scene.add(sun);
        view.root = new THREE.Group();
        view.scene.add(view.root);
        view.renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl, antialias: true });
        view.renderer.autoClear = false;
      },
      render(gl, matrix) {
        const p = view.placement;
        if (!p || !view.root.children.length) return;
        const origin = view.maplibregl.MercatorCoordinate.fromLngLat([p.anchor[1], p.anchor[0]], 0);
        const s = origin.meterInMercatorCoordinateUnits() * (p.scale_ratio || 1);
        // glTF (x east, y up, -z north) -> Mercator (x east, y south, z up), then turn by the heading
        const transform = new THREE.Matrix4()
          .makeTranslation(origin.x, origin.y, origin.z)
          .scale(new THREE.Vector3(s, -s, s))
          .multiply(new THREE.Matrix4().makeRotationX(Math.PI / 2))
          .multiply(new THREE.Matrix4().makeRotationY(THREE.MathUtils.degToRad(p.heading_deg)));
        view.camera.projectionMatrix = new THREE.Matrix4().fromArray(matrix).multiply(transform);
        view.renderer.resetState();
        view.renderer.render(view.scene, view.camera);
      },
    };
  }

  setBasemap(id) {
    if (!this.map) return;
    this.map.removeLayer('base');
    this.map.removeSource('base');
    this.map.addSource('base', rasterSource(id));
    this.map.addLayer({ id: 'base', type: 'raster', source: 'base' }, 'ifc-model');
  }

  async setModel(url) {
    if (!this.map || this.modelUrl === url) return;
    this.modelUrl = url;
    const gltf = await new GLTFLoader().loadAsync(url);
    this.root.clear();
    gltf.scene.traverse((o) => {
      if (o.isMesh) {
        o.material.side = THREE.DoubleSide;
        if (!o.geometry.attributes.normal) o.material.flatShading = true;
        o.material.needsUpdate = true;
      }
    });
    this.root.add(gltf.scene);
    this.applyBase();
    this.map.triggerRepaint();
  }

  applyBase() {
    // stand the model on the map surface: its lowest point at height 0
    if (this.root && this.placement) this.root.position.y = -(this.placement.base_z_m || 0);
  }

  setPlacement(placement, bounds) {
    this.placement = placement;
    if (!this.map || !placement) return;
    this.applyBase();
    if (!this.framed) {
      this.framed = true;
      if (bounds) {
        this.map.fitBounds([[bounds[0][1], bounds[0][0]], [bounds[1][1], bounds[1][0]]],
          { padding: 80, pitch: 60, bearing: -20, maxZoom: 19.5, duration: 0 });
      } else {
        this.map.jumpTo({ center: [placement.anchor[1], placement.anchor[0]], zoom: 18, pitch: 60 });
      }
    }
    this.map.triggerRepaint();
  }

  refit(bounds) {
    this.framed = false;
    this.setPlacement(this.placement, bounds);
  }
}
