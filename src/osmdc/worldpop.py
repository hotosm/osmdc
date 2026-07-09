"""Aggregate WorldPop Global 2 total-population rasters into a per-H3-cell parquet.

WorldPop ships one total-population GeoTIFF per country and year, named
``{iso}_pop_{year}_CN_{res}_R2025A_UA_v1.tif`` (constrained, UN-adjusted, WGS84;
number of people per pixel). Each populated pixel is binned to its H3 cell by centre
coordinate and summed; cells straddling country borders sum across the source files.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import rasterio

from osmdc import config


def _year_total_tifs(tif_dir: Path, year: int) -> list[Path]:
    """Total-population rasters for one year, one per country ({iso}_pop_{year}_...)."""
    year_token = str(year)
    return sorted(
        tif
        for tif in tif_dir.rglob("*.tif")
        for parts in [tif.stem.split("_")]
        if len(parts) > 2 and parts[1] == "pop" and parts[2] == year_token
    )


def _bin_raster(con: duckdb.DuckDBPyConnection, tif: Path, resolution: int) -> None:
    """Bin one raster's populated pixels into the wp_partials table by H3 cell."""
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
            con.execute(f"""
                INSERT INTO wp_partials
                SELECT h3_latlng_to_cell(lat, lon, {resolution}) AS cell, sum(pop)
                FROM wp_window GROUP BY 1
            """)
            con.unregister("wp_window")


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
