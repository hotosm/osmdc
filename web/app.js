import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4/+esm";
import * as h3 from "https://cdn.jsdelivr.net/npm/h3-js@4/+esm";
import * as duckdb from "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.29.0/+esm";

window.h3 = h3;
let MapboxOverlay, H3HexagonLayer;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`failed to load ${src}`));
    document.head.appendChild(script);
  });
}

const DATA_BASE =
  "https://huggingface.co/datasets/kshitijrajsharma/osm-completeness-tiles/resolve/main/";

const ROADS_ENABLED = true;

const el = (id) => document.getElementById(id);
const status = (msg) => {
  el("status").classList.remove("error");
  el("status").textContent = msg;
};
const error = (msg) => {
  el("status").classList.add("error");
  el("status").textContent = msg;
};

const COLOR_STOPS = [
  [0.0, [215, 48, 39]],
  [0.5, [254, 224, 139]],
  [1.0, [26, 152, 80]],
];

function ramp(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 1; i < COLOR_STOPS.length; i++) {
    const [p1, c1] = COLOR_STOPS[i - 1];
    const [p2, c2] = COLOR_STOPS[i];
    if (t <= p2) {
      const f = (t - p1) / (p2 - p1);
      return c1.map((c, k) => Math.round(c + (c2[k] - c) * f));
    }
  }
  return COLOR_STOPS.at(-1)[1];
}

let map, overlay, db, manifest, currentRows = [];
let gridVisible = true;
let busy = false;
let popSource = "kontur";
let coverageCells = [];
let loadedGeojson = null;
let currentParents = new Set();
let tsManifest = null;
let tsData = new Map();
let selectedYear = null;
const DEFAULT_YEAR = 2025;
// WorldPop interpolates between the c.2010 and c.2020 census rounds; later years are projected.
const LAST_CENSUS_YEAR = 2020;

const yearLabel = (year) => (year > LAST_CENSUS_YEAR ? `${year} (projected)` : `${year}`);

const popNames = () => (manifest.populations || []).map((s) => s.name);

function colFor(metric) {
  if (metric === "population") return `population_${popSource}`;
  if (metric === "gap_score") return `gap_score_${popSource}`;
  return metric;
}

function updateDataNote() {
  if (!manifest) return;
  el("dn-overture").textContent = `Overture ${manifest.overture_release || "-"}`;
  const active = (manifest.populations || []).find((s) => s.name === popSource);
  el("dn-population").textContent = active
    ? `${active.label}${active.date ? ` · ${active.date}` : ""}`
    : "-";
}

function setBusy(on) {
  busy = on;
  if (on) el("assess").disabled = true;
  el("assess").classList.toggle("busy", on);
  if (!on) updateAssessButton();
}

function updateAssessButton() {
  if (busy || !map) return;
  const tooBig = boundsAreaKm2(map.getBounds()) > 40000;
  el("assess").disabled = tooBig;
  el("assess").textContent = tooBig ? "Zoom in to assess" : "Assess current view";
}

const ESRI_TILES =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

function toggleSatellite(on) {
  map.setLayoutProperty("esri", "visibility", on ? "visible" : "none");
}

function toggleGrid(on) {
  gridVisible = on;
  render(currentRows);
}

async function initMap() {
  await loadScript("https://cdn.jsdelivr.net/npm/deck.gl@9/dist.min.js");
  ({ MapboxOverlay, H3HexagonLayer } = window.deck);
  map = new maplibregl.Map({
    container: "map",
    style: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    center: [85.32, 27.71],
    zoom: 10,
    hash: true,
  });
  overlay = new MapboxOverlay({ interleaved: false, layers: [] });
  map.addControl(overlay);
  await new Promise((resolve) => map.on("load", resolve));
  map.addSource("esri", {
    type: "raster",
    tiles: [ESRI_TILES],
    tileSize: 256,
    maxzoom: 19,
    attribution: "Imagery © Esri, Maxar",
  });
  map.addLayer({ id: "esri", type: "raster", source: "esri", layout: { visibility: "none" } });
  map.addSource("aoi", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "aoi-casing", type: "line", source: "aoi",
    paint: { "line-color": "#ffffff", "line-width": 3.5, "line-opacity": 0.85 },
  });
  map.addLayer({
    id: "aoi-line", type: "line", source: "aoi",
    paint: { "line-color": "#111827", "line-width": 1.75, "line-dasharray": [2, 2] },
  });
  map.on("moveend", updateAssessButton);
  updateAssessButton();
}

function setAoi(polys) {
  map.getSource("aoi").setData({
    type: "Feature",
    geometry: { type: "MultiPolygon", coordinates: polys },
  });
}

function clearAoi() {
  if (map?.getSource("aoi")) {
    map.getSource("aoi").setData({ type: "FeatureCollection", features: [] });
  }
}

async function initDuckDB() {
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" })
  );
  const worker = new Worker(workerUrl);
  db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
  await db.instantiate(bundle.mainModule, bundle.pkgWorker);
  URL.revokeObjectURL(workerUrl);
}

async function loadManifest() {
  const res = await fetch(new URL("manifest.json", DATA_BASE));
  if (!res.ok) throw new Error(`manifest fetch failed: ${res.status}`);
  manifest = await res.json();
  if (manifest.overture_release) el("src-overture").textContent = manifest.overture_release;
  buildPopulationUI();
}

function buildPopulationUI() {
  const sources = manifest.populations || [];
  if (sources.length) popSource = sources[0].name;
  const select = el("popsrc");
  select.innerHTML = "";
  for (const source of sources) {
    const option = document.createElement("option");
    option.value = source.name;
    option.textContent = source.label || source.name;
    select.appendChild(option);
  }
  el("popsrc-row").hidden = sources.length < 2;
  updateDataNote();
  const list = el("src-populations");
  list.innerHTML = "";
  for (const source of sources) {
    const item = document.createElement("li");
    const date = source.date ? ` (${source.date})` : "";
    const name = source.label || source.name;
    item.innerHTML = source.url
      ? `<a href="${source.url}" target="_blank" rel="noopener">${name}</a>${date}`
      : `${name}${date}`;
    list.appendChild(item);
  }
}

function polygonsOf(geojson) {
  const feats = geojson.type === "FeatureCollection" ? geojson.features
    : geojson.type === "Feature" ? [geojson]
      : [{ type: "Feature", geometry: geojson }];
  const polys = [];
  const otherTypes = new Set();
  for (const f of feats) {
    const g = f.geometry;
    if (!g) continue;
    if (g.type === "Polygon") polys.push(g.coordinates);
    else if (g.type === "MultiPolygon") polys.push(...g.coordinates);
    else otherTypes.add(g.type);
  }
  return { polys, otherTypes };
}

function coveredCells(polys, resolution) {
  const cells = new Set();
  for (const rings of polys) {
    for (const cell of h3.polygonToCells(rings, resolution, true)) cells.add(cell);
    if (cells.size === 0) {
      for (const [lng, lat] of rings[0]) cells.add(h3.latLngToCell(lat, lng, resolution));
    }
  }
  return cells;
}

async function queryTiles(parents) {
  const urls = [];
  for (const parent of parents) {
    const rel = manifest.tiles[parent];
    if (rel) urls.push(new URL(rel, DATA_BASE).href);
  }
  if (urls.length === 0) return [];

  const conn = await db.connect();
  const names = [];
  for (let i = 0; i < urls.length; i++) {
    const response = await fetch(urls[i]);
    if (!response.ok) throw new Error(`tile fetch failed (${response.status}): ${urls[i]}`);
    const name = `tile_${i}.parquet`;
    await db.registerFileBuffer(name, new Uint8Array(await response.arrayBuffer()));
    names.push(`'${name}'`);
  }
  const sources = popNames();
  const rows = [];
  const reader = await conn.send(
    `SELECT * FROM read_parquet([${names.join(",")}], union_by_name=true)`
  );
  for await (const batch of reader) {
    for (let i = 0; i < batch.numRows; i++) rows.push(tileRow(batch.get(i).toJSON(), sources));
  }
  await conn.close();
  for (let i = 0; i < names.length; i++) await db.dropFile(`tile_${i}.parquet`);
  return rows;
}

function tileRow(o, sources) {
  const row = {
    h3: o.h3,
    bld_count: Number(o.bld_count),
    bld_osm: Number(o.bld_osm),
    osm_pct: o.osm_pct == null ? null : Number(o.osm_pct),
    road_count: Number(o.road_count),
    road_osm: o.road_osm == null ? 0 : Number(o.road_osm),
    road_pct: o.road_pct == null ? null : Number(o.road_pct),
    road_len_m: Number(o.road_len_m),
  };
  for (const name of sources) {
    row[`population_${name}`] = o[`population_${name}`] == null ? 0 : Number(o[`population_${name}`]);
    row[`gap_score_${name}`] = o[`gap_score_${name}`] == null ? 0 : Number(o[`gap_score_${name}`]);
  }
  return row;
}

const PERCENT_METRICS = new Set(["osm_pct", "road_pct"]);
const HOT_METRICS = new Set(["gap_score"]);

async function ensureTsManifest() {
  if (tsManifest !== null) return tsManifest;
  const res = await fetch(new URL("worldpop_ts/manifest.json", DATA_BASE));
  tsManifest = res.ok ? await res.json() : false;
  return tsManifest;
}

async function fetchTs(parents) {
  tsData = new Map();
  if (!tsManifest || !tsManifest.years) return;
  const conn = await db.connect();
  const names = [];
  for (const parent of parents) {
    const rel = tsManifest.tiles[parent];
    if (!rel) continue;
    const response = await fetch(new URL(`worldpop_ts/${rel}`, DATA_BASE).href);
    if (!response.ok) continue;
    const name = `ts_${names.length}.parquet`;
    await db.registerFileBuffer(name, new Uint8Array(await response.arrayBuffer()));
    names.push(name);
  }
  if (names.length) {
    const years = tsManifest.years;
    const cols = years.map((y) => `pop_${y}`).join(", ");
    const files = names.map((n) => `'${n}'`).join(",");
    const reader = await conn.send(`SELECT h3, ${cols} FROM read_parquet([${files}], union_by_name=true)`);
    for await (const batch of reader) {
      for (let i = 0; i < batch.numRows; i++) {
        const o = batch.get(i).toJSON();
        tsData.set(o.h3, years.map((y) => (o[`pop_${y}`] == null ? 0 : Number(o[`pop_${y}`]))));
      }
    }
  }
  await conn.close();
  for (const name of names) await db.dropFile(name);
}

function yearActive() {
  return popSource === "worldpop" && tsManifest && tsData.size > 0 && selectedYear != null;
}

function popForYear(h3) {
  const arr = tsData.get(h3);
  const index = tsManifest ? tsManifest.years.indexOf(selectedYear) : -1;
  return arr && index >= 0 ? arr[index] : null;
}

function popValue(d) {
  if (yearActive()) {
    const value = popForYear(d.h3);
    if (value != null) return value;
  }
  return d[colFor("population")] ?? 0;
}

function metricValue(d) {
  const metric = el("metric").value;
  if (metric === "population" && yearActive()) {
    const value = popForYear(d.h3);
    if (value != null) return value;
  }
  return d[colFor(metric)];
}

function makeNormaliser(rows) {
  const metric = el("metric").value;
  if (PERCENT_METRICS.has(metric)) return (v) => (v == null ? null : v / 100);
  let max = 0;
  for (const d of rows) max = Math.max(max, metricValue(d) || 0);
  return (v) => (v == null || max === 0 ? null : v / max);
}

function render(rows) {
  if (!gridVisible) return overlay.setProps({ layers: [] });
  const norm = makeNormaliser(rows);
  const hot = HOT_METRICS.has(el("metric").value);
  const gapCol = colFor("gap_score");
  const dataSet = new Set(rows.map((d) => d.h3));
  const emptyCells = coverageCells.filter((h) => !dataSet.has(h));
  const coverageLayer = new H3HexagonLayer({
    id: "coverage",
    data: emptyCells,
    getHexagon: (d) => d,
    extruded: false,
    filled: true,
    stroked: true,
    getFillColor: [130, 130, 130, 18],
    getLineColor: [130, 130, 130, 55],
    lineWidthMinPixels: 0.5,
    pickable: false,
  });
  const layer = new H3HexagonLayer({
    id: "cells",
    data: rows,
    getHexagon: (d) => d.h3,
    extruded: false,
    filled: true,
    stroked: true,
    getLineColor: [255, 255, 255, 30],
    lineWidthMinPixels: 0.5,
    opacity: 0.72,
    pickable: true,
    getFillColor: (d) => {
      let t = norm(metricValue(d));
      if (t == null) return [80, 80, 80, 120];
      if (hot) t = 1 - t;
      return [...ramp(t), 200];
    },
    updateTriggers: { getFillColor: [el("metric").value, popSource, selectedYear, rows] },
  });
  overlay.setProps({
    layers: [coverageLayer, layer],
    getTooltip: ({ object }) =>
      object && {
        html: `<b>${object.h3}</b><br/>buildings: ${object.bld_count} (${object.osm_pct ?? "-"}% in OSM)<br/>roads: ${(object.road_len_m / 1000).toFixed(2)} km${ROADS_ENABLED ? ` (${object.road_pct ?? "-"}% in OSM)` : ""}<br/>population${yearActive() ? ` ${selectedYear}` : ""}: ${popValue(object).toLocaleString()} (gap ${(object[gapCol] ?? 0).toLocaleString()})`,
        style: {
          background: "#ffffff", color: "#1e293b", fontSize: "12px", padding: "6px",
          borderRadius: "4px", boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
        },
      },
  });
}

function showStats(rows) {
  const gapCol = colFor("gap_score");
  let bld = 0, bldOsm = 0, road = 0, roadOsm = 0, km = 0, pop = 0, gap = 0;
  for (const d of rows) {
    bld += d.bld_count;
    bldOsm += d.bld_osm;
    road += d.road_count;
    roadOsm += d.road_osm;
    km += d.road_len_m / 1000;
    pop += popValue(d);
    gap += d[gapCol] ?? 0;
  }
  el("s-cells").textContent = rows.length.toLocaleString();
  el("s-bld").textContent = bld.toLocaleString();
  el("s-pct").textContent = bld ? `${((bldOsm / bld) * 100).toFixed(1)}%` : "-";
  if (ROADS_ENABLED) {
    el("s-road-pct").textContent = road ? `${((roadOsm / road) * 100).toFixed(1)}%` : "-";
  }
  el("s-km").textContent = `${km.toFixed(1)} km`;
  el("s-pop").textContent = Math.round(pop).toLocaleString();
  el("s-gap").textContent = Math.round(gap).toLocaleString();
  el("stats").hidden = false;
}

function fitTo(rows) {
  if (rows.length === 0) return;
  const b = new maplibregl.LngLatBounds();
  for (const d of rows) for (const [lat, lng] of h3.cellToBoundary(d.h3)) b.extend([lng, lat]);
  map.fitBounds(b, { padding: 40, duration: 600 });
}

function fitToPolys(polys) {
  const b = new maplibregl.LngLatBounds();
  for (const rings of polys) for (const ring of rings) for (const [lng, lat] of ring) b.extend([lng, lat]);
  if (!b.isEmpty()) map.fitBounds(b, { padding: 40, duration: 600 });
}

async function process(geojson, fit = true, showAoi = true) {
  setBusy(true);
  try {
    status("Reading area...");
    const { polys, otherTypes } = polygonsOf(geojson);
    if (polys.length === 0) {
      const found = [...otherTypes].join(", ");
      return error(found
        ? `Only Polygon and MultiPolygon are supported, not ${found}.`
        : "No polygon found in that file.");
    }
    if (showAoi) setAoi(polys);

    const cells = coveredCells(polys, manifest.resolution);
    coverageCells = showAoi ? [...cells] : [];
    const parents = new Set([...cells].map((c) => h3.cellToParent(c, manifest.partition_resolution)));
    currentParents = parents;

    status(`Querying ${parents.size} tile(s)...`);
    const all = await queryTiles(parents);
    currentRows = all.filter((d) => cells.has(d.h3));
    await loadTimeSeries(parents);

    render(currentRows);
    if (currentRows.length === 0) {
      el("stats").hidden = true;
      el("download").disabled = true;
      updateYearUI();
      renderSparkline();
      if (fit) fitToPolys(polys);
      return status(`No mapped data in ${cells.size} covered cells. Coverage shown faint.`);
    }
    showStats(currentRows);
    updateYearUI();
    renderSparkline();
    if (fit) coverageCells.length ? fitToPolys(polys) : fitTo(currentRows);
    el("download").disabled = false;
    const empty = cells.size - currentRows.length;
    status(`${currentRows.length} cells with data${empty > 0 ? `, ${empty} empty` : ""}.`);
  } finally {
    setBusy(false);
  }
}

async function loadTimeSeries(parents) {
  if (popSource !== "worldpop") return;
  await ensureTsManifest();
  await fetchTs(parents);
}

function updateYearUI() {
  const years = tsManifest && tsManifest.years;
  const on = popSource === "worldpop" && years && years.length > 0;
  el("year-row").hidden = !on;
  if (!on) return;
  if (selectedYear == null || !years.includes(selectedYear)) {
    selectedYear = years.includes(DEFAULT_YEAR) ? DEFAULT_YEAR : years.at(-1);
  }
  const slider = el("year");
  slider.max = String(years.length - 1);
  slider.value = String(years.indexOf(selectedYear));
  el("year-val").textContent = yearLabel(selectedYear);
  el("year-min").textContent = years[0];
  el("year-max").textContent = years.at(-1);
}

function renderSparkline() {
  const box = el("spark");
  const caption = el("spark-delta");
  if (!yearActive() || currentRows.length === 0) {
    box.innerHTML = "";
    caption.textContent = "";
    return;
  }
  const years = tsManifest.years;
  const totals = years.map((y, i) => currentRows.reduce((sum, d) => sum + (tsData.get(d.h3)?.[i] ?? 0), 0));
  const width = 284, height = 44, pad = 5;
  const min = Math.min(...totals), max = Math.max(...totals);
  const xAt = (i) => pad + (width - 2 * pad) * (years.length < 2 ? 0.5 : i / (years.length - 1));
  const yAt = (v) => height - pad - (height - 2 * pad) * (max === min ? 0.5 : (v - min) / (max - min));
  const points = totals.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
  const si = years.indexOf(selectedYear);
  const cx = xAt(si).toFixed(1), cy = yAt(totals[si]).toFixed(1);
  box.innerHTML =
    `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" ` +
    `aria-label="Population ${years[0]} to ${years.at(-1)}">` +
    `<line x1="${cx}" y1="${pad}" x2="${cx}" y2="${height - pad}" stroke="var(--line)" stroke-width="1"/>` +
    `<polyline fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" ` +
    `stroke-linecap="round" points="${points}"/>` +
    `<circle cx="${cx}" cy="${cy}" r="3.5" fill="var(--accent)"/></svg>`;
  const base = totals[0] || 0, current = totals[si] || 0;
  const pct = base ? Math.round(((current - base) / base) * 100) : 0;
  caption.textContent =
    `${Math.round(current).toLocaleString()} in ${selectedYear} · ${pct >= 0 ? "+" : ""}${pct}% since ${years[0]}`;
}

function boundsAreaKm2(bounds) {
  const midLat = (((bounds.getNorth() + bounds.getSouth()) / 2) * Math.PI) / 180;
  const width = (bounds.getEast() - bounds.getWest()) * 111.32 * Math.cos(midLat);
  const height = (bounds.getNorth() - bounds.getSouth()) * 110.57;
  return Math.abs(width * height);
}

function assessView() {
  const b = map.getBounds();
  if (boundsAreaKm2(b) > 40000) return status("Zoom in to assess a smaller area.");
  const poly = {
    type: "Polygon",
    coordinates: [[
      [b.getWest(), b.getSouth()], [b.getEast(), b.getSouth()],
      [b.getEast(), b.getNorth()], [b.getWest(), b.getNorth()], [b.getWest(), b.getSouth()],
    ]],
  };
  return process(poly, false, true);
}

let searchHits = [];

async function searchSuggest(query) {
  const results = el("search-results");
  if (query.trim().length < 3) return (results.hidden = true);
  const url =
    "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&q=" + encodeURIComponent(query);
  const res = await fetch(url);
  if (!res.ok) return;
  searchHits = await res.json();
  results.innerHTML = "";
  for (const [i, hit] of searchHits.entries()) {
    const item = document.createElement("li");
    item.textContent = hit.display_name;
    item.dataset.index = i;
    results.appendChild(item);
  }
  results.hidden = searchHits.length === 0;
}

function selectPlace(hit) {
  if (!hit) return;
  el("search-results").hidden = true;
  el("search").value = hit.display_name.split(",")[0];
  const [south, north, west, east] = hit.boundingbox.map(Number);
  map.fitBounds([[west, south], [east, north]], { maxZoom: 13, duration: 800 });
  status(`Moved to ${hit.display_name.split(",").slice(0, 2).join(",")}.`);
}

function clearSearch() {
  el("search").value = "";
  el("search-results").hidden = true;
  el("search-clear").classList.remove("show");
  searchHits = [];
  currentRows = [];
  coverageCells = [];
  loadedGeojson = null;
  currentParents = new Set();
  tsData = new Map();
  updateLoadedButton();
  updateYearUI();
  renderSparkline();
  overlay.setProps({ layers: [] });
  clearAoi();
  el("stats").hidden = true;
  el("download").disabled = true;
  status("Ready.");
}

function downloadGeoJSON() {
  const years = tsManifest && tsData.size ? tsManifest.years : null;
  const features = currentRows.map((d) => {
    const properties = { ...d };
    const series = years && tsData.get(d.h3);
    if (series) years.forEach((year, i) => (properties[`pop_${year}`] = series[i]));
    return {
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [h3.cellToBoundary(d.h3, true)] },
      properties,
    };
  });
  const blob = new Blob([JSON.stringify({ type: "FeatureCollection", features })], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "osm-completeness.geojson";
  a.click();
  URL.revokeObjectURL(a.href);
}

function updateLoadedButton() {
  el("sample").textContent = loadedGeojson ? "Assess loaded area" : "Try a sample area";
}

function readFile(file) {
  el("drop-text").textContent = file.name;
  const reader = new FileReader();
  reader.onload = () => {
    loadedGeojson = JSON.parse(reader.result);
    updateLoadedButton();
    process(loadedGeojson);
  };
  reader.readAsText(file);
}

function wireUI() {
  const drop = el("drop");
  drop.addEventListener("click", () => el("file").click());
  el("file").addEventListener("change", (e) => e.target.files[0] && readFile(e.target.files[0]));
  ["dragover", "dragenter"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("hover"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("hover"); })
  );
  drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && readFile(e.dataTransfer.files[0]));

  const rerender = () => currentRows.length && (render(currentRows), updateLegend());
  el("metric").addEventListener("change", rerender);
  el("popsrc").addEventListener("change", async (e) => {
    popSource = e.target.value;
    updateDataNote();
    if (popSource === "worldpop" && currentParents.size) await loadTimeSeries(currentParents);
    updateYearUI();
    if (currentRows.length) (render(currentRows), showStats(currentRows));
    renderSparkline();
  });
  el("year").addEventListener("input", (e) => {
    selectedYear = tsManifest.years[Number(e.target.value)];
    el("year-val").textContent = yearLabel(selectedYear);
    if (currentRows.length) (render(currentRows), showStats(currentRows));
    renderSparkline();
  });
  el("download").addEventListener("click", downloadGeoJSON);
  el("sample").addEventListener("click", () => {
    if (!loadedGeojson) loadedGeojson = SAMPLE;
    updateLoadedButton();
    process(loadedGeojson);
  });
  el("assess").addEventListener("click", assessView);
  el("toggle-esri").addEventListener("change", (e) => toggleSatellite(e.target.checked));
  el("toggle-grid").addEventListener("change", (e) => toggleGrid(e.target.checked));

  const search = el("search");
  let searchTimer;
  search.addEventListener("input", () => {
    el("search-clear").classList.toggle("show", search.value.length > 0);
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => searchSuggest(search.value), 300);
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      selectPlace(searchHits[0]);
    }
  });
  el("search-results").addEventListener("click", (e) => {
    const item = e.target.closest("li");
    if (item) selectPlace(searchHits[Number(item.dataset.index)]);
  });
  el("search-clear").addEventListener("click", clearSearch);
  document.addEventListener("click", (e) => {
    if (!el("search-box").contains(e.target)) el("search-results").hidden = true;
  });

  const modal = el("info-modal");
  el("info-btn").addEventListener("click", () => modal.classList.add("open"));
  el("info-close").addEventListener("click", () => modal.classList.remove("open"));
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.classList.remove("open");
  });

  if (!ROADS_ENABLED) {
    el("metric").querySelector('option[value="road_pct"]')?.remove();
    el("stat-road").hidden = true;
    el("modal-road").hidden = true;
  }
}

const METRIC_LEGEND = {
  osm_pct: { label: "Building completeness (% in OSM)", min: "0%", max: "100%" },
  road_pct: { label: "Road completeness (% in OSM)", min: "0%", max: "100%" },
  gap_score: { label: "Mapping gap (people)", min: "high", max: "low" },
  population: { label: "Population", min: "fewer", max: "more" },
};

function updateLegend() {
  const legend = METRIC_LEGEND[el("metric").value];
  el("legend-label").textContent = legend.label;
  el("legend-min").textContent = legend.min;
  el("legend-max").textContent = legend.max;
}

const SAMPLE = {
  type: "Polygon",
  coordinates: [[
    [85.29, 27.67], [85.37, 27.67], [85.37, 27.74], [85.29, 27.74], [85.29, 27.67],
  ]],
};

async function boot() {
  const sharedView = location.hash.length > 1;
  wireUI();
  updateLegend();
  status("Loading engine...");
  await Promise.all([initDuckDB(), loadManifest(), initMap()]);
  status("Ready. Search a place, then assess the current view.");
  if (sharedView) assessView();
}

boot().catch((e) => error(`Error: ${e.message}`));
