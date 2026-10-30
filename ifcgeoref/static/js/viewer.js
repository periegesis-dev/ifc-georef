import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { acceleratedRaycast, computeBoundsTree, disposeBoundsTree } from 'three-mesh-bvh';

THREE.BufferGeometry.prototype.computeBoundsTree = computeBoundsTree;
THREE.BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree;
THREE.Mesh.prototype.raycast = acceleratedRaycast;

const SNAP_CORNER_PX = 14;
const SNAP_EDGE_PX = 8;
const SNAP_COLORS = { corner: 0x15803d, edge: 0xd97706, surface: 0x2563eb };

/**
 * 3D model view with point picking.
 * The glTF is Y-up and centred:  gl = (X - ox, Z - oz, -(Y - oy))  with IFC world X, Y, Z in metres.
 */
export class ModelViewer {
  constructor(container, { onPick } = {}) {
    this.container = container;
    this.onPick = onPick;
    this.offset = [0, 0, 0];
    this.meshes = [];
    this.model = null;
    this.picking = false;
    this.snap = true;
    this.hover = null;
    this.radius = 10;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0xf4f5f7);
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.05, 50000);
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.appendChild(this.renderer.domElement);

    this.labels = new CSS2DRenderer();
    this.labels.domElement.className = 'viewer-labels';
    container.appendChild(this.labels.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.screenSpacePanning = true;
    // Draw only when something changed; an idle model costs no CPU/GPU.
    this.needsRender = true;
    this.controls.addEventListener('change', () => this.requestRender());

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x9a9a9a, 2.0));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(0.5, 1, 0.3);
    this.scene.add(sun);

    const dot = new THREE.SphereGeometry(1, 16, 12);
    this.cursor = new THREE.Mesh(dot, new THREE.MeshBasicMaterial({ color: SNAP_COLORS.corner, depthTest: false }));
    this.cursor.renderOrder = 1000;
    this.cursor.visible = false;
    this.scene.add(this.cursor);
    this.markerGeometry = dot;
    this.markerMaterial = new THREE.MeshBasicMaterial({ color: 0x111827, depthTest: false });
    this.siteMaterial = new THREE.MeshBasicMaterial({ color: 0x1d4ed8, depthTest: false });
    this.markers = new THREE.Group();
    this.scene.add(this.markers);
    this.helpers = new THREE.Group();
    this.scene.add(this.helpers);

    this.raycaster = new THREE.Raycaster();
    this.raycaster.firstHitOnly = true;
    this.pointer = new THREE.Vector2();
    this.down = null;
    this.pendingHover = null;

    const dom = this.renderer.domElement;
    dom.addEventListener('pointerdown', (e) => { this.down = { x: e.clientX, y: e.clientY }; });
    dom.addEventListener('pointerup', (e) => this.handleUp(e));
    dom.addEventListener('pointermove', (e) => { this.pendingHover = { x: e.clientX, y: e.clientY }; });
    dom.addEventListener('pointerleave', () => {
      this.pendingHover = null;
      this.hover = null;
      this.cursor.visible = false;
      this.requestRender();
    });

    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    const loop = () => {
      requestAnimationFrame(loop);
      this.frame();
    };
    requestAnimationFrame(loop);
  }

  resize() {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.labels.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  requestRender() {
    this.needsRender = true;
  }

  frame() {
    const moved = this.controls.update(); // true while the camera moves (incl. damping)
    if (this.pendingHover && this.picking) {
      this.updateHover(this.pendingHover.x, this.pendingHover.y);
      this.pendingHover = null;
      this.needsRender = true;
    }
    if (!moved && !this.needsRender) return;
    this.needsRender = false;
    const k = 0.007;
    for (const obj of [this.cursor, ...this.markers.children]) {
      if (obj.isMesh) obj.scale.setScalar(Math.max(this.camera.position.distanceTo(obj.position) * k, 1e-4));
    }
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }

  worldFromGl(v) {
    const [ox, oy, oz] = this.offset;
    return [v.x + ox, -v.z + oy, v.y + oz];
  }

  glFromWorld(p) {
    const [ox, oy, oz] = this.offset;
    return new THREE.Vector3(p[0] - ox, p[2] - oz, -(p[1] - oy));
  }

  async load(url, offset, onProgress, expectedBytes) {
    this.offset = offset || [0, 0, 0];
    const loader = new GLTFLoader();
    const gltf = await loader.loadAsync(url, (e) => {
      const total = expectedBytes || e.total;
      if (onProgress && total) onProgress(Math.min(e.loaded / total, 0.99));
    });
    if (this.model) this.scene.remove(this.model);
    this.model = gltf.scene;
    this.meshes = [];
    this.model.traverse((o) => {
      if (o.isMesh) {
        o.material.side = THREE.DoubleSide;
        if (!o.geometry.attributes.normal) o.material.flatShading = true; // the glTF omits normals
        o.material.needsUpdate = true;
        this.meshes.push(o);
      }
    });
    this.scene.add(this.model);
    this.addHelpers();
    this.fit();
    this.requestRender();
  }

  unload() {
    if (this.model) this.scene.remove(this.model);
    for (const mesh of this.meshes) {
      if (mesh.geometry.boundsTree) mesh.geometry.disposeBoundsTree();
      mesh.geometry.dispose();
    }
    this.model = null;
    this.meshes = [];
    this.helpers.clear();
    for (const child of [...this.markers.children]) this.markers.remove(child);
    this.setPicking(false);
    this.requestRender();
  }

  /** Build the picking acceleration structure (blocks briefly on very large models). */
  prepareBvh() {
    for (const mesh of this.meshes) mesh.geometry.computeBoundsTree();
  }

  addHelpers() {
    this.helpers.clear();
    const box = new THREE.Box3().setFromObject(this.model);
    const size = box.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.z, 1);
    const grid = new THREE.GridHelper(span * 2, 20, 0xc4c8cf, 0xe1e4e8);
    grid.position.set((box.min.x + box.max.x) / 2, box.min.y, (box.min.z + box.max.z) / 2);
    this.helpers.add(grid);
    // IFC origin axes: X red, Y green, Z blue
    const origin = this.glFromWorld([0, 0, 0]);
    const len = Math.max(span * 0.12, 1);
    const axes = [[[len, 0, 0], 0xdc2626], [[0, len, 0], 0x16a34a], [[0, 0, len], 0x2563eb]];
    for (const [dir, color] of axes) {
      const end = this.glFromWorld(dir);
      const geometry = new THREE.BufferGeometry().setFromPoints([origin, end]);
      const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({ color, depthTest: false }));
      line.renderOrder = 900;
      this.helpers.add(line);
    }
    const div = document.createElement('div');
    div.className = 'marker-label';
    div.textContent = 'origin';
    const label = new CSS2DObject(div);
    label.position.copy(origin);
    this.helpers.add(label);
  }

  fit() {
    if (!this.model) return;
    const sphere = new THREE.Box3().setFromObject(this.model).getBoundingSphere(new THREE.Sphere());
    const r = Math.max(sphere.radius, 1);
    this.radius = r;
    this.camera.near = r / 500;
    this.camera.far = r * 200;
    this.camera.updateProjectionMatrix();
    const dir = new THREE.Vector3(1, 0.85, 1.25).normalize();
    this.camera.position.copy(sphere.center).addScaledVector(dir, r * 2.3);
    this.controls.target.copy(sphere.center);
    this.controls.update();
    this.requestRender();
  }

  topView() {
    if (!this.model) return;
    const sphere = new THREE.Box3().setFromObject(this.model).getBoundingSphere(new THREE.Sphere());
    const r = Math.max(sphere.radius, 1);
    // A tiny offset towards +z keeps model north (+Y, i.e. -z) pointing up on screen.
    this.camera.position.set(sphere.center.x, sphere.center.y + r * 2.6, sphere.center.z + r * 0.002);
    this.controls.target.copy(sphere.center);
    this.controls.update();
    this.requestRender();
  }

  setSnap(on) {
    this.snap = Boolean(on);
  }

  setPicking(on) {
    this.picking = Boolean(on);
    this.container.classList.toggle('picking', this.picking);
    if (!this.picking) {
      this.cursor.visible = false;
      this.hover = null;
    }
    this.requestRender();
  }

  screenPoint(v, rect) {
    const p = v.clone().project(this.camera);
    return new THREE.Vector2((p.x + 1) / 2 * rect.width, (1 - p.y) / 2 * rect.height);
  }

  updateHover(clientX, clientY) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const sx = clientX - rect.left;
    const sy = clientY - rect.top;
    this.pointer.set((sx / rect.width) * 2 - 1, -(sy / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hit = this.raycaster.intersectObjects(this.meshes, false)[0];
    if (!hit) {
      this.hover = null;
      this.cursor.visible = false;
      return;
    }
    let point = hit.point.clone();
    let kind = 'surface';
    if (this.snap && hit.face) {
      const position = hit.object.geometry.attributes.position;
      const corners = [hit.face.a, hit.face.b, hit.face.c].map(
        (i) => new THREE.Vector3().fromBufferAttribute(position, i).applyMatrix4(hit.object.matrixWorld));
      const mouse = new THREE.Vector2(sx, sy);
      let best = null;
      let bestDistance = SNAP_CORNER_PX;
      for (const c of corners) {
        const d = this.screenPoint(c, rect).distanceTo(mouse);
        if (d < bestDistance) { bestDistance = d; best = c; }
      }
      if (best) {
        point = best;
        kind = 'corner';
      } else {
        bestDistance = SNAP_EDGE_PX;
        for (let i = 0; i < 3; i += 1) {
          const q = new THREE.Vector3();
          new THREE.Line3(corners[i], corners[(i + 1) % 3]).closestPointToPoint(hit.point, true, q);
          const d = this.screenPoint(q, rect).distanceTo(mouse);
          if (d < bestDistance) { bestDistance = d; best = q; }
        }
        if (best) {
          point = best;
          kind = 'edge';
        }
      }
    }
    this.hover = { point, kind };
    this.cursor.position.copy(point);
    this.cursor.material.color.setHex(SNAP_COLORS[kind]);
    this.cursor.visible = true;
  }

  handleUp(e) {
    const down = this.down;
    this.down = null;
    if (!this.picking || !down || e.button !== 0) return;
    if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) return; // that was a drag
    this.updateHover(e.clientX, e.clientY);
    if (this.hover && this.onPick) {
      this.onPick({ world: this.worldFromGl(this.hover.point), kind: this.hover.kind });
    }
  }

  /** markers: [{ label, world: [x, y, z] (IFC metres), kind: 'site' | 'point' }] */
  setMarkers(markers) {
    for (const child of [...this.markers.children]) this.markers.remove(child);
    for (const m of markers) {
      const position = this.glFromWorld(m.world);
      const sphere = new THREE.Mesh(this.markerGeometry, m.kind === 'site' ? this.siteMaterial : this.markerMaterial);
      sphere.position.copy(position);
      sphere.renderOrder = 999;
      const div = document.createElement('div');
      div.className = `marker-label${m.kind === 'site' ? ' site' : ''}`;
      div.textContent = m.label;
      const label = new CSS2DObject(div);
      label.position.copy(position);
      this.markers.add(sphere, label);
    }
    this.requestRender();
  }
}
