"""Aggregate WorldPop Global 2 total-population rasters into per-H3-cell parquet.

WorldPop ships one total-population GeoTIFF per country and year, named
``{iso}_pop_{year}_CN_{res}_R2025A_UA_v1.tif`` (constrained, UN-adjusted, WGS84;
number of people per pixel). Each populated pixel is binned to its H3 cell by centre
coordinate and summed; cells straddling country borders sum across the source files.

worldpop_to_parquet writes one year as (h3, population). worldpop_timeseries_tiles
writes a wide (h3, pop_<year>...) tile set so the browser can scrub years in memory.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import rasterio

from osmdc import config
from osmdc.aggregate import _compact_partitions


def _year_total_tifs(tif_dir: Path, year: int) -> list[Path]:
    """Total-population rasters for one year, one per country ({iso}_pop_{year}_...)."""
    year_token = str(year)
    return sorted(
        tif
        for tif in tif_dir.rglob("*.tif")
        for parts in [tif.stem.split("_")]
        if len(parts) > 2 and parts[1] == "pop" and parts[2] == year_token
    )


def _each_window(con: duckdb.DuckDBPyConnection, tif: Path) -> Iterator[None]:
    """Register each block's populated pixels as the wp_window view (lon, lat, pop)."""
    with rasterio.open(tif) as src:
        transform = src.transform
        if transform.b or transform.d:
            raise ValueError(f"{tif} is not a north-up raster")
        nodata = src.nodata
        for _, window in src.block_windows(1):
            band = src.read(1, window=window)
            valid = np.isfinite(band) & (band > 0)
            if nodata is not None:
                valid &= band != nodata
            rows, cols = np.nonzero(valid)
            if rows.size == 0:
                continue
            lon = transform.c + (cols + window.col_off + 0.5) * transform.a
            lat = transform.f + (rows + window.row_off + 0.5) * transform.e
            pixels = pa.table({"lon": lon, "lat": lat, "pop": band[rows, cols].astype("float64")})
            con.register("wp_window", pixels)
            yield
            con.unregister("wp_window")


def _bin_raster(
    con: duckdb.DuckDBPyConnection, tif: Path, resolution: int, table: str = "wp_partials"
) -> None:
    """Bin one raster's populated pixels into the given (cell, pop) table by H3 cell."""
    for _ in _each_window(con, tif):
        con.execute(f"""
            INSERT INTO {table}
            SELECT h3_latlng_to_cell(lat, lon, {resolution}) AS cell, sum(pop)
            FROM wp_window GROUP BY 1
        """)


def worldpop_to_parquet(
    con: duckdb.DuckDBPyConnection,
    tif_dir: str,
    out_path: Path,
    year: int = config.WORLDPOP_YEAR,
    resolution: int = config.H3_RESOLUTION,
) -> tuple[int, float]:
    """Read WorldPop rasters under tif_dir and write an (h3, population) parquet.

    Returns (cell_count, total_population).
    """
    tifs = _year_total_tifs(Path(tif_dir), year)
    if not tifs:
        raise FileNotFoundError(f"no {year} total-population rasters found under {tif_dir}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute("CREATE OR REPLACE TEMP TABLE wp_partials (cell UBIGINT, pop DOUBLE)")
    for tif in tifs:
        _bin_raster(con, tif, resolution)
    con.execute(f"""
        COPY (
            SELECT h3_h3_to_string(cell) AS h3, round(sum(pop)) AS population
            FROM wp_partials
            GROUP BY cell
            HAVING sum(pop) > 0
        ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION zstd)
    """)
    summary = con.execute(
        f"SELECT count(*), coalesce(sum(population), 0) FROM read_parquet('{out_path}')"
    ).fetchone()
    assert summary is not None
    return summary[0], summary[1]


def worldpop_timeseries_tiles(
    con: duckdb.DuckDBPyConnection,
    tif_dir: str,
    out_dir: Path,
    scratch_dir: Path,
    years: list[int],
    resolution: int = config.H3_RESOLUTION,
    partition_resolution: int = config.PARTITION_RESOLUTION,
) -> tuple[list[int], int]:
    """Write wide (h3, pop_<year>...) tiles sharded by H3 parent, one column per year.

    Each year is binned on its own so peak memory is one year of cells; the years are
    then pivoted to wide from the per-year scratch parquet. Returns (years, cell_count).
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    present = []
    for year in years:
        tifs = _year_total_tifs(Path(tif_dir), year)
        if not tifs:
            continue
        present.append(year)
        con.execute("CREATE OR REPLACE TEMP TABLE wp_year (cell UBIGINT, pop DOUBLE)")
        for tif in tifs:
            _bin_raster(con, tif, resolution, "wp_year")
        con.execute(f"""
            COPY (SELECT cell, {year} AS year, round(sum(pop)) AS pop
                  FROM wp_year GROUP BY cell HAVING sum(pop) > 0)
            TO '{scratch_dir}/ts_{year}.parquet' (FORMAT PARQUET, COMPRESSION zstd)
        """)
    if not present:
        raise FileNotFoundError(f"no total-population rasters for {years} under {tif_dir}")
    year_cols = ", ".join(f"round(sum(pop) FILTER (WHERE year = {y})) AS pop_{y}" for y in present)
    con.execute(f"""
        COPY (
            SELECT h3_h3_to_string(cell) AS h3,
                   h3_h3_to_string(h3_cell_to_parent(cell, {partition_resolution})) AS h3_parent,
                   {year_cols}
            FROM read_parquet('{scratch_dir}/ts_*.parquet')
            GROUP BY cell
        ) TO '{out_dir}' (FORMAT PARQUET, PARTITION_BY (h3_parent), COMPRESSION zstd)
    """)
    _compact_partitions(con, out_dir)
    write_ts_manifest(out_dir, present)
    count_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{out_dir}/**/*.parquet')"
    ).fetchone()
    assert count_row is not None
    return present, count_row[0]


def write_ts_manifest(out_dir: Path, years: list[int]) -> None:
    """Write the time-series manifest: available years and each parent's tile path."""
    tiles = {
        path.parent.name.removeprefix("h3_parent="): str(path.relative_to(out_dir))
        for path in sorted(out_dir.glob("h3_parent=*/*.parquet"))
    }
    manifest = {
        "source": "worldpop",
        "release": config.WORLDPOP_RELEASE,
        "years": years,
        "tiles": tiles,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
