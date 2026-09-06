# OSM Completeness Helper

Drop a GeoJSON polygon and see how complete OpenStreetMap is inside it. The area is
split into H3 hexagons, each coloured by how much of what Overture Maps knows about,
buildings and roads, already carries an OpenStreetMap source. Low values are mapping
gaps.

Each hexagon also carries population, from either Kontur Population or WorldPop
Global 2 (selectable in the panel), and a mapping gap score that weights people by how
incomplete the buildings are, so the places where mapping helps most stand out.

It is a static browser app. Hexagon tiles are pre-built for the whole planet and read
straight from a Hugging Face dataset with DuckDB-WASM, so there is no backend.

## Develop

```
just setup
just serve
just test
```

`just serve` runs the app at http://localhost:8000.

## Updating the data

WorldPop Global 2 publishes an annual release covering the years 2015 to 2030. The
`Refresh WorldPop tiles` workflow runs on the first of each month and compares the
release and years offered by the [WorldPop STAC API](https://api.stac.worldpop.org) with
the manifest already published in the
[worldpop-h3](https://huggingface.co/datasets/kshitijrajsharma/worldpop-h3) dataset,
which holds the population tiles on their own. When they match it
stops there, which takes about fifteen seconds. When they differ, it works through the
years one at a time, downloading each year's 1km constrained, UN-adjusted country
rasters, binning them into H3 cells and deleting the rasters, then pivots the years into
tiles and uploads them. A full rebuild takes about ninety minutes and peaks around 8 GB
of memory. The published manifest records the release, the years, the cell count and the
build time; the app reads the release and the years to label what it is showing.

The workflow needs an `HF_TOKEN` repository secret with write access to the dataset.

It tracks one version of each release. If WorldPop revises a release in place, the sweep
finds no rasters it recognises and the run stops instead of publishing part of the world.
Picking up a revision is then a deliberate change to the pattern in `refresh.ASSET_ID`.

The same three steps run by hand:

```
uv run osmdc-refresh plan --repo user/dataset --out plan.json
uv run osmdc-refresh worldpop-year --plan plan.json --year 2025 --out-dir cells
uv run osmdc-refresh worldpop-publish --plan plan.json --scratch-dir cells --tiles tiles --repo user/dataset
```

Omit `--repo` from the last command to build the tiles without uploading them.

Overture is not on a schedule. A planet rebuild scans about 350 GB and takes several
hours, so it is run with `osmdc-planet` on a machine of your choosing. The Overture
bucket keeps only the two most recent releases, so a pinned release id stops resolving
after two months.

Built with ❤️ by [kshitij](https://github.com/sponsors/kshitijrajsharma).
