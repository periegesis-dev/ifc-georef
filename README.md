# IFC Georeferencing

Web application that places IFC (BIM) models on the map. It writes the georeferencing into the
file the standard way:

| Schema | What is written |
| --- | --- |
| IFC4, IFC4X3 | `IfcProjectedCRS` + `IfcMapConversion` on the model context |
| IFC2X3 | `ePSet_MapConversion` + `ePSet_ProjectedCRS` property sets on the `IfcSite` |

The original file is copied byte for byte; only the georeferencing records are added (or replaced
when the file is already georeferenced). Every result is read back and checked before it is
offered for download.

Based on [IfcGref](https://github.com/tudelft3d/ifcgref) by the TU Delft 3D geoinformation group (MIT).

## Workflow

1. **Upload** an `.ifc` or `.ifczip`. The file is read once, in the background, with progress for
   every step: units, site location, existing georeferencing and a 3D preview are extracted.
2. **Coordinate system.** The building location (from the file, from GeoDataHub, an address search or
   a click on the map) is used to suggest coordinate systems; the national grid is preselected and
   coverage is checked.
3. **Placement.** Use the location stored in the file and/or control points: click a corner on the 3D
   model (corners and edges snap), then the same spot on the map. Points can also be typed or pasted
   from Excel. The result updates live with the error of every point, a footprint preview on the map,
   and fine-tuning controls (move, rotate, height).
4. **Save.** The georeferenced copy is written and verified, then downloaded or uploaded straight into
   the GeoDataHub project.

Scale is fixed to the unit conversion by default (rigid placement); fitting the scale from the points
is an advanced option. With one point the rotation comes from the north direction stored in the file,
corrected for grid convergence of the chosen map projection.

## Run with Docker

```bash
cp .env.example .env        # optional, adjust API_BASE_URL etc.
docker compose up --build -d
```

Open <http://localhost:5002>. Job files live in the Docker volume `georef-data` and are removed after
`JOB_TTL_HOURS` (default 24 h).

Run the tests (also in Docker):

```bash
docker compose --profile test run --rm --build tests
```

## Run without Docker

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
python app.py               # http://127.0.0.1:5002
python -m pytest
```

## Opening from GeoDataHub

The frontend opens `/?project_id=<id>&token=<token>`. Both values are moved into session storage and
removed from the address bar right away (they are never logged). Optional parameters pre-fill the
form: `epsg=2056`, `lat=47.37&lon=8.54`.

A page that embeds this tool in an iframe can send the login instead of putting it in the URL:

```js
iframe.contentWindow.postMessage({ type: 'ifc-georef:auth', projectId, token }, 'https://<this-service>');
```

## Configuration

See [.env.example](.env.example). The most important settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_BASE_URL` | `http://localhost:8000` | GeoDataHub backend for "Upload to project" |
| `DEFAULT_EPSG` | `2056` | Fallback coordinate system |
| `CORS_ORIGINS` | GeoDataHub + localhost | Browser origins allowed to call the API / embed the page |
| `MAX_UPLOAD_MB` | `500` | Upload limit |
| `MAX_PARALLEL_JOBS` | `2` | IFC files processed at the same time |
| `ELEVATION_LOOKUP` | `all` | Ground height suggestions: `all`, `swisstopo` or `off` |

## API

| Endpoint | Purpose |
| --- | --- |
| `POST /api/jobs` | Upload a file (multipart `file`), starts processing |
| `GET /api/jobs/<id>` | Progress of every task, metadata, 3D/footprint info, result |
| `GET /api/jobs/<id>/model.glb` | 3D preview (binary glTF, gzip) |
| `POST /api/jobs/<id>/solve` | Compute the placement and a map preview (JSON body, see below) |
| `POST /api/jobs/<id>/georeference` | Write and verify the georeferenced file |
| `GET /api/jobs/<id>/download` | Download the result |
| `POST /api/jobs/<id>/upload` | Send the result to `API_BASE_URL/api/ifc/<project_id>/upload/` (`Authorization` header is forwarded) |
| `GET /api/crs/suggest?lat=&lon=` | Ranked EPSG suggestions for a location |
| `GET /api/crs/search?q=` | Search EPSG systems by code, name or area |
| `POST /api/analyze-georeferencing` | Legacy analysis endpoint used by the GeoDataHub 3D viewer |

Placement body: `{"epsg": 2056, "use_site": true, "points": [{"label": "P1", "local": [x, y, z],
"target": [e, n, h]}], "scale_mode": "fixed", "rotation_deg": null, "height": null, "offset": [0, 0, 0]}`.
Local coordinates are in the IFC file's length unit, targets in the units of the chosen system.

## Project layout

```
app.py                  entry point (gunicorn app:app / python app.py)
ifcgeoref/
  web.py                routes
  jobs.py, tasks.py     job storage, background processing, progress
  model_info.py         reads units, site, context, existing georeferencing
  geometry.py           3D preview (glTF) and footprint
  crs.py                EPSG details, suggestions, search, conversions
  solver.py             closed-form least-squares placement
  placement.py          request -> solution + map preview
  writer.py, step.py    writes georeferencing into the STEP file
  legacy.py             /api/analyze-georeferencing response
  templates/, static/   the single-page interface
tests/                  pytest suite (runs in Docker)
samples/                sample IFC files used by the tests
```
