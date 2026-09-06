"""Bin WorldPop Global 2 country rasters into per-H3-cell population parquet.

Each populated pixel is binned by centre coordinate, so a cell on a border sums
across the countries that cover it.
"""

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from math import ceil
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import rasterio

from osmdc import config
from osmdc.aggregate import compact_partitions

# Rows per pivot pass: at this size the planet pivots in 8GB of memory, 0.6GB of spill.
PIVOT_BATCH_ROWS = 25_000_000


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
    """Write an (h3, population) parquet for one year. Returns (cells, total_population)."""
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


def worldpop_year_parquet(
    con: duckdb.DuckDBPyConnection,
    tif_dir: str,
    out_path: Path,
    year: int,
    resolution: int = config.H3_RESOLUTION,
) -> int:
    """Bin one year's rasters into a (cell, year, pop) parquet. Returns the cell count."""
    tifs = _year_total_tifs(Path(tif_dir), year)
    if not tifs:
        raise FileNotFoundError(f"no {year} total-population rasters found under {tif_dir}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute("CREATE OR REPLACE TEMP TABLE wp_year (cell UBIGINT, pop DOUBLE)")
    for tif in tifs:
        _bin_raster(con, tif, resolution, "wp_year")
    con.execute(f"""
        COPY (SELECT cell, {year} AS year, round(sum(pop)) AS pop
              FROM wp_year GROUP BY cell HAVING sum(pop) > 0)
        TO '{out_path}' (FORMAT PARQUET, COMPRESSION zstd)
    """)
    count_row = con.execute(f"SELECT count(*) FROM read_parquet('{out_path}')").fetchone()
    assert count_row is not None
    return count_row[0]


def pivot_years(
    con: duckdb.DuckDBPyConnection,
    scratch_dir: Path,
    out_dir: Path,
    years: list[int],
    partition_resolution: int = config.PARTITION_RESOLUTION,
) -> int:
    """Pivot per-year cell parquet into wide (h3, pop_<year>...) tiles. Returns the cell count.

    A year missing here would publish a column of nulls, so every one must be binned
    already. Parents are batched because one pass holds every cell it pivots in memory.
    """
    sources = {year: scratch_dir / f"ts_{year}.parquet" for year in years}
    missing = [year for year, path in sources.items() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"no binned cells for {missing} in {scratch_dir}")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    year_cols = ", ".join(f"round(sum(pop) FILTER (WHERE year = {y})) AS pop_{y}" for y in years)
    paths = ", ".join(f"'{path}'" for path in sources.values())
    row_count = con.execute(f"SELECT count(*) FROM read_parquet([{paths}])").fetchone()
    assert row_count is not None
    batches = max(1, ceil(row_count[0] / PIVOT_BATCH_ROWS))
    parent = f"h3_cell_to_parent(cell, {partition_resolution})"
    for batch in range(batches):
        # Hashed because the unused digits of a coarse H3 index are constant.
        where = "" if batches == 1 else f"WHERE hash({parent}) % {batches} = {batch}"
        con.execute(f"""
            COPY (
                SELECT h3_h3_to_string(cell) AS h3,
                       h3_h3_to_string({parent}) AS h3_parent,
                       {year_cols}
                FROM read_parquet([{paths}])
                {where}
                GROUP BY cell
            ) TO '{out_dir}' (FORMAT PARQUET, PARTITION_BY (h3_parent), COMPRESSION zstd, APPEND)
        """)
    compact_partitions(con, out_dir)
    count_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{out_dir}/**/*.parquet')"
    ).fetchone()
    assert count_row is not None
    return count_row[0]


def worldpop_timeseries_tiles(
    con: duckdb.DuckDBPyConnection,
    tif_dir: str,
    out_dir: Path,
    scratch_dir: Path,
    years: list[int],
    resolution: int = config.H3_RESOLUTION,
    partition_resolution: int = config.PARTITION_RESOLUTION,
) -> tuple[list[int], int]:
    """Bin each year found under tif_dir on its own, then pivot. Returns (years, cells)."""
    scratch_dir.mkdir(parents=True, exist_ok=True)
    present = [year for year in years if _year_total_tifs(Path(tif_dir), year)]
    if not present:
        raise FileNotFoundError(f"no total-population rasters for {years} under {tif_dir}")
    for year in present:
        worldpop_year_parquet(con, tif_dir, scratch_dir / f"ts_{year}.parquet", year, resolution)
    cells = pivot_years(con, scratch_dir, out_dir, present, partition_resolution)
    write_ts_manifest(out_dir, present, cells)
    return present, cells


def write_ts_manifest(
    out_dir: Path, years: list[int], cells: int, release: str = config.WORLDPOP_RELEASE
) -> None:
    """Write the time-series manifest the browser reads: provenance, years and tile paths."""
    tiles = {
        path.parent.name.removeprefix("h3_parent="): str(path.relative_to(out_dir))
        for path in sorted(out_dir.glob("h3_parent=*/*.parquet"))
    }
    manifest = {
        "source": "worldpop",
        "release": release,
        "years": years,
        "cells": cells,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tiles": tiles,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
