# Brief: bring 3D IFC georeferencing up to the 2D DXF tool

**For:** the ifc-georef developer.
**Scope:** changes in this repo (`ifc-georef`, current branch `feature/dockerize-ifcgeoref`, commit c119f8a, the step-based wizard).
**Reference:** the 2D DXF georeferencing tool in the GeoDataHub frontend (`geodata-frontend`).
**Date:** 2026-11-01.

GeoDataHub users already georeference DXF drawings on the 2D map. They expect the same toolset when they georeference an IFC model here. This brief lists every place where the 3D tool falls short of the 2D one, says what to build, and gives a check for each item. It also lists what the 3D tool already does better, so none of that gets lost.

File references:
- 3D: paths relative to `ifc-georef/ifcgeoref/`.
- 2D: paths relative to `geodata-frontend/components/Home/` (`MAP` = `Map.js`, `PLC` = `MapComponents/dxfPlacement.js`).
- In the frontend, edit the `.js` files, not the `.ts` twins: Next loads the `.js` versions at runtime.

---

## 1. How the 2D tool behaves (the reference)

1. **Import.** Map tools → Import file → DXF.
   - A drawing with no stored georeference is dropped at the current map centre, unrotated, scaled to about 55% of the view (`PLC:638-655`).
   - The move/rotate/resize mode opens straight away. The user can place the drawing entirely by hand, with no control points.
2. **Move.** Drag the drawing (`PLC:716-737`). The transform is applied live on the map.
3. **Rotate.**
   - Drag the rotate handle that sits above the frame. It pivots around the centre of the drawing's bounding box (`PLC:707-741`).
   - Double-clicking the handle opens a "Rotate drawing" dialog with an absolute angle in degrees. **Positive is clockwise**, and 0 means the orientation as uploaded (`PLC:392-406`, `MAP:7557-7609`).
4. **Scale.**
   - **Corner handles:** drag for uniform scale (`PLC:742-763`).
   - **Scale by distance:** pick two snapped points on the drawing, then type the true length in metres. The first point stays fixed and the rotation is kept (`PLC:948-988`, dialog `MAP:7507-7556`).
   - **Choosing the scale:** the tool remembers where the scale came from ("calibrate" = by distance, "georef" = by points). If a new operation would change the scale, it asks "Which scale do you want to keep?" (`PLC:1129-1138`, `MAP:7610-7656`).
5. **Georeference by points.**
   - Pick a point on the drawing, with snapping, then type its Easting/Northing. Repeat for 2 or more points.
   - The solve is a least-squares 2D similarity fit for scale, rotation and translation (`PLC:1108-1183`). Afterwards the tool returns to move mode for fine-tuning.
6. **Snapping.**
   - **Targets:** vertices, edges and line intersections across the whole drawing, using an OpenLayers Snap interaction (`PLC:259-264, 1653-1712`).
   - **Radius:** adjustable from 0 to 50 px, where 0 turns snapping off; the default is 12 px (`EntityEditorPanel.tsx:227-245`).
   - **Where it applies:** snapping is used when picking control points and when picking the two points for scale by distance.
7. **Uploaded layers panel.** For each layer: visibility, save to project, re-enter move mode to re-edit, zoom to layer, export, remove (`SymbologyPanel.tsx`).
8. **Saving and re-editing.**
   - The georeference is stored with the file in the project.
   - "Load from project" reopens it, and the user can keep editing from where they left off (`projectSave.ts`, `projectGisLoader.js`).

## 2. What the 3D tool already does better (keep it)

- Residuals and RMS per point, with colour coding (`solver.py:123-148`, `main.js:1060-1066`). The 2D tool shows none.
- Targets can be picked on the map, typed or pasted. The 2D tool only accepts typed targets.
- CRS suggestions from the location, a projected-only check and a coverage check.
- Correct units for SI and conversion-based lengths such as feet and inches (`model_info.py:51-77`).
- A fitted height (OrthogonalHeight), and single-point placement from TrueNorth plus grid convergence.
- A real-mesh 3D picker, a 2D footprint preview and a 3D map preview.
- The output is standard IfcMapConversion / IFC2X3 ePSet, read back and verified after writing (`tasks.py:141-152`).

Don't break any of these while adding the items below.

## 3. Gap overview

| # | 2D capability | 3D today | Gap |
|---|---|---|---|
| G1 | Place by hand with no control points | Needs at least one point or the IFC site location (`placement.py:81`, `solver.py:50`) | **Missing** |
| G2 | Drag to move, live on the map | Step buttons only: N/S/E/W, 1 cm to 10 m (`index.html:141-148`, `main.js:1353-1360`) | **Missing** |
| G3 | Drag a rotate handle, live | Typed angle and step buttons only | **Missing** |
| G4 | Double-click → absolute angle dialog, clockwise-positive | Typed absolute angle, anticlockwise from Easting (`solver.py:3-9`) | Convention differs |
| G5 | Rotation pivots on the drawing centre | Pivots on the control-point centroid. With the "Existing" point that is the IFC origin, which can be far away (`solver.py:68-69, 102-104`) | Wrong pivot for manual rotation |
| G6 | Scale by distance (two snapped points + true length) | None | **Missing** |
| G7 | Corner-drag uniform scale | None | **Missing** (only behind an unlock, see R4) |
| G8 | Scale source tracking and a "which scale to keep?" choice | Units scale, or the "fit scale" checkbox (`main.js:917-920`); no source shown; manual unit override removed | **Missing** |
| G9 | Snapping across the whole drawing, adjustable radius, off at 0 | Only the hit triangle's own 3 vertices and 3 edges (triangulation diagonals included), fixed 14/8 px, on/off toggle (`viewer.js:11-13, 251-297`) | **Partial** |
| G10 | Snapping also used for scale-by-distance picks | No scale by distance | Follows G6 |
| G11 | Re-edit a saved georeference from where it was left | Re-upload restores origin and rotation but **not the scale**. The prefill fires only once and only if the CRS already matches (`main.js:982-997`) | **Partial, with bugs** |
| G12 | Load the file from the project, edit, save back | The user must download from GeoDataHub and re-upload here | **Missing** |
| G13 | Project map context: cadastral layer and other project layers | OSM / swisstopo imagery and map / Esri base maps only (`map.js:5-26`) | **Missing** cadastral overlay |
| G14 | Live transform during interaction | Every change re-solves on the server, then redraws | Partial: needs an instant client preview |

## 4. Requirements

Priorities: **Must** = needed for parity. **Should** = needed to work as well as 2D in practice. **Nice** = beyond parity.

### R1. Manual placement with no control points (Must, G1)
- On the Placement step, add **"Place on map"**. It drops the model's footprint at the current map centre, or at the address or site location if one is known, at the units scale with rotation 0.
- The user then refines it by hand (R2, R3, R5).
- **Solver:** accept zero control points when an explicit anchor is given. The anchor is the map position of the footprint centre (E, N); H comes from the height field or the ground height.
- Write the result the same way as a point-based solve.
- Show "Placed by hand, no accuracy figures" where the RMS normally appears.
- **Check:** a model with no IfcSite reference and no points can be placed, moved and rotated, then saved. The written IfcMapConversion puts the footprint where it was shown.

### R2. Direct manipulation on the 2D preview map (Must, G2, G3, G7, G14)
- **Handles:** draw an oriented frame around the footprint with:
  - a move area (drag inside the footprint);
  - a rotate handle above the frame, pivoting on the footprint centre;
  - corner handles for uniform scale, only active when scale is unlocked (R4).
- **Live preview:** the footprint, axes and pins update instantly on the client while dragging, using the same formula as `solver.py`. The server solve is debounced (about 250 ms after the last change), and the server's values replace the client ones when it answers.
- **Mapping drags to solver inputs:** convert each interaction into the existing parameters. A move becomes `offset`; a rotate becomes `rotation_override` plus the offset that keeps the pivot in place (see R5); a scale becomes the scale value with source "manual".
- **Esc** cancels the drag in progress. Hitting the footprint takes priority over panning the map; dragging empty map still pans.
- **Check:** dragging moves the footprint without lag; on release the server values match the preview within 1 cm and 0.01°.

### R3. Move parity (Must, G2)
- Dragging comes from R2. Keep the step pad.
- Add typed **ΔE / ΔN / ΔH** fields in CRS units, applied on Enter.
- Add **up/down** step buttons for height, using the same step sizes.
- **Check:** typing ΔE = 2.5 moves E0 by exactly 2.5 map units.

### R4. Scale parity (Must, G6, G7, G8)
- **Scale panel.** Show the current Scale (IfcMapConversion `Scale`) and where it came from:
  - "File units (mm → 0.001)", "Fitted from points", "From distance" or "Manual".
- **Unit override.** Add a select (mm, cm, m, in, ft) for files whose unit metadata is wrong. The old override was removed in c119f8a; bring it back as a scale source.
- **Numeric factor** field (source "Manual").
- **Scale by distance.**
  - Button "Scale by distance": pick two points on the **3D model** with snapping (R6), then enter the true distance in metres.
  - Formula: `Scale = (D_m / crs_unit_m) / d_file`, where `d_file` is the model distance between the two picks in file units.
  - The first point stays fixed and rotation is unchanged (the 2D behaviour).
- **Choosing between sources.**
  - When a new operation would replace a scale from another source by more than 0.1%, ask which to keep. Show both values and the difference in %.
  - **"Keep current" must keep the current scale and still apply the rest of the operation.** For example, solving points with a fixed scale. Don't copy the 2D behaviour here: in 2D, "Keep current" throws the whole operation away (`PLC:1131-1138`).
- **Lock.** The default stays rigid (units scale). Corner-drag scaling (R2) only works after the user unlocks the scale. Warn when `|Scale / units scale − 1| > 1%`, as `solver.py:81-100` already does.
- **Check:** on the mm sample house, scale by distance between two wall corners 10.000 m apart gives Scale 0.001 within 0.5%. On the feet Duplex file, the panel shows "File units (ft → 0.3048)".

### R5. Rotation parity (Must, G3, G4, G5)
- **Rotate handle:** drag from R2. Shift snaps to 15° steps (Nice).
- **Angle dialog:** double-clicking the handle opens an absolute-angle dialog prefilled with the current value. Enter applies, Esc cancels. Accept 2 decimals and a minus sign. Keep the typed field too.
- **Convention:** match GeoDataHub 2D in the **UI**.
  - Show the angle as **clockwise-positive degrees**, where 0 = the model as authored (+X pointing east). That value is `φ = −θ`, normalised to (−180, 180].
  - Keep `θ` (anticlockwise from grid east, as written to `XAxisAbscissa/Ordinate`) in the API and the solver.
  - Label the field "Rotation (clockwise, °)", and make the step buttons follow the same sense.
- **Pivot:** manual rotation (drag, dialog, step buttons) pivots on the **footprint centre**, not the control-point centroid. After applying `rotation_override`, adjust `offset` so the footprint centre's map position does not change.
- **Check:**
  - Rotating 30° by drag, then opening the dialog, shows 30.00. The footprint centre's E/N stays within 1 mm.
  - With the "Existing" point at the IFC origin, rotating no longer swings the building around the origin.

### R6. Snapping parity in the 3D picker (Must, G9, G10)
- **What to snap to:** model **corners** (vertices of feature edges) and **feature edges** (nearest point on an edge) across all nearby geometry, not only the hit triangle.
  - Ignore triangulation diagonals. A feature edge is a mesh edge whose two faces meet at more than 20° (as `THREE.EdgesGeometry` does), or a boundary edge.
- **Where to compute it:** prefer the server, in `geometry.py`, next to the glb. For example, an extra `edges` line primitive or a binary segment list per job. Index it on the client (grid or BVH) so a pick only searches near the hit point.
  - The search radius in world units comes from the pixel radius at the hit depth.
- **Optional targets (Nice):** edge midpoints, and intersections of edges in plan.
- **Radius:** adjustable 0–50 px next to the snap toggle, where 0 = off, default 12 px (the same as 2D). It replaces the fixed 14 px / 8 px (`viewer.js:11-12`).
- **Indicators:** keep the existing corner, edge and surface markers.
- **Where snapping applies:** control-point picks and scale-by-distance picks.
- **Check:**
  - Clicking about 10 px from a wall corner snaps to the corner even when the hit triangle doesn't contain it.
  - Clicking in the middle of a rectangular face never offers its diagonal.
  - Snapping still feels instant on the Duplex sample (about 1.6 MB IFC).

### R7. Re-edit and project round trip (Must for a–c, Should for d; G11, G12)
**a. Restore everything on re-edit.**
- When the uploaded file is already georeferenced, restore E, N, H, rotation **and Scale** with source "From file". Today Scale falls back to the unit conversion (`main.js:986-990`).
- Prefill whenever the selected CRS matches the file's EPSG, including after the user changes the CRS back. Set `state.prefilled` only after the prefill actually happened (`main.js:983-986`).

**b. Open from project.**
- The page knows `project_id` and the token. Add "Open from project". It lists the project's IFC files from GeoDataHub: `GET {API_BASE_URL}/api/ifc/{project_id}/` with `Authorization: Token …`, the same call the frontend's BIM tab makes.
- The response is `ifc_files: [{filename, s3_key, size, last_modified, url}]`.
- Picking a file creates a job from its `url` on the server side, so the user never downloads and re-uploads.

**c. Save back.**
- Keep `POST /api/jobs/<id>/upload`.
- Show the target file name before uploading, and after success say which project and file it went to.
- Updating the existing file in place, as 2D does with `PUT /aws/project/{p}/gisfiles/`, needs a GeoDataHub backend endpoint for IFC. **List it as a dependency; don't fake it.**

**d. Keep the control points (Should).**
- Write the control points, scale source and rotation source into a non-standard property set, e.g. `GDH_Georeferencing` on IfcProject, as JSON text. Re-editing then restores the exact points.
- Never use the `Pset_` or `ePSet_` prefixes for it.

**Check:** georeference, save to project, Open from project, and the same points, scale and rotation reappear.

### R8. Map context for aligning (Should, G13)
- Add the cadastral map as an overlay for Swiss sites: WMS `https://wms.geo.admin.ch/`, layer `ch.kantone.cadastralwebmap-farbe`. GeoDataHub already uses this layer.
- Give it its own toggle and an opacity slider for the footprint fill.
- **Check:** with the overlay on, a footprint can be lined up with parcel boundaries by eye at zoom 19+.

### R9. Undo and redo of placement (Should)
- Undo and redo every placement change: move, rotate, scale, points added or removed, height. Ctrl/Cmd+Z, Ctrl+Shift+Z and Ctrl+Y, with 50 steps.
- The 2D tool only has undo for entity edits, not placement. Do better here.
- **Check:** three nudges and a rotation, then Ctrl+Z four times, returns to the exact starting values.

### R10. Fix rough edges found in this review (Should unless noted)
1. **Adding a control point while a manual rotation or move is active.**
   - Today the old manual values stay applied (`solver.py:68-69, 119-121`, `main.js:989, 1380`).
   - Show a clear "Manual rotation is on" chip.
   - When a new point would let rotation be fitted, offer "Use fitted rotation".
2. **Footprint and preview include IfcSite / terrain geometry,** which can inflate the footprint (`geometry.py:19`). Leave IfcSite, IfcGeographicElement and terrain meshes out of the footprint by default, with a toggle.
3. **Write `MapUnit`** on IfcProjectedCRS from the CRS axis unit (`writer.py:31-40`). VerticalDatum can stay `$`, but show "No vertical datum written" in the summary.
4. **IfcMapConversionScaled:** read FactorX/Y/Z when present, and keep them when replacing the record (`model_info.py:117-143`, `writer.py:62-64`).
5. **`.ifczip`:** limit the total unpacked size (e.g. `MAX_UPLOAD_MB × 4`) and the number of entries before extracting (`jobs.py:111-118`).
6. **EPSG not in the CRS Name:** fall back to pyproj matching on the CRS parameters, or ask the user, instead of no prefill (`model_info.py:23`).
7. **Auto ground height** assumes the model's ±0.00 is on the terrain (`main.js:1339-1341`). Say so next to the height field, with a one-click "use ground height" / "use file height" choice.

### R11. API for the GeoDataHub 3D view (Must)
The GeoDataHub frontend only calls `POST /api/analyze-georeferencing` (via its proxy). Keep the response shape it already uses: `is_georeferenced`, and `bounding_box_world.min/max.{easting, northing, elevation}`. Then:
1. **Compute `bounding_box_world` from the geometry's extents in world coordinates,** not from product placement origins (`model_info.py:281-287`, `legacy.py:126-167`). Return the 4 oriented `corners` too; they're already in the shape.
2. **Remove the fixed `× 0.001` in `world_coordinates.three_js`** (`legacy.py:78-79`). Use the file's unit scale.
3. **Unknown CRS:** don't silently default to EPSG 2056 (`legacy.py:132`). Return `crs: null` plus an entry in `issues`.
4. **Add a `transform` object:**
   ```json
   "transform": {
     "epsg": 2056,
     "eastings": 2600000.0,
     "northings": 1200000.0,
     "orthogonal_height": 450.0,
     "rotation_deg_ccw_from_east": 12.5,
     "rotation_deg_clockwise": -12.5,
     "scale": 0.001,
     "file_unit_m": 0.001
   }
   ```
   The frontend will apply rotation and scale from this object.
**Check:** a pytest proves the analysis of a known output reproduces the written IfcMapConversion values exactly, and that the bbox matches the mesh extents within 1 cm.

## 5. Out of scope here (GeoDataHub frontend, for awareness)

These were found in the same review and will be fixed in the frontend, not in ifc-georef:
- **Loading BIM in the 3D view is currently broken.** Commit 96a5bb58 changed `lib/IFCLoader.js:16` to import `{ Loader }`, but line 20 still calls `new IFCLoader()`, which throws a ReferenceError.
- **The 3D view ignores the georeferenced rotation.** It applies a hard-coded `rotation.y = 0.25` and derives the scale from the bounding box (`ThreeDViewModal.js:1186-1187, 1410-1411`, `lib/ifcGeoreferencing.js:44-51`). R11 provides what it needs to place models exactly.
- **IFC placement in the 3D view is not saved with a 3D space,** and the model cannot be adjusted there.

## 6. Acceptance run (before handing back)

Run all of these in Docker against `http://localhost:5002`, opened as `/?project_id=<id>&token=<token>`:

1. **Duplex** (`Ifc2s3_Duplex_Electrical.ifc`, IFC2X3, feet):
   - Upload, choose EPSG 2056, then "Place on map" (R1).
   - Drag, rotate by handle, and set 30° in the dialog (R2, R5).
   - Save to project, then Open from project (R7b): the placement comes back (R7a).
2. **Sample house** (IFC4, mm):
   - Scale by distance on two snapped wall corners (R4, R6).
   - Add 3 control points by map pick. RMS is shown and corner snapping works away from the hit triangle (R6).
   - Undo the last 3 actions (R9).
3. **Already georeferenced file** (`121.ifc`, EPSG 32643): every parameter is restored, including Scale (R7a). `/api/analyze-georeferencing` returns the `transform` object and a geometry-based bbox (R11).
4. **pytest:** the existing suite passes, plus new tests for zero-point placement, scale by distance, the pivot-preserving rotation and the R11 shape.
5. **Hand back** a short list of what changed, any deviations from this brief with the reason, and anything that needs a GeoDataHub backend change (R7c).
