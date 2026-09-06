import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from osmdc import worldpop
from osmdc.aggregate import connect
from osmdc.worldpop import (
    pivot_years,
    worldpop_timeseries_tiles,
    worldpop_to_parquet,
    worldpop_year_parquet,
)


def _write_raster(path, value, lng=85.30, lat=27.72):
    """One-pixel WGS84 raster just south-east of the given corner, with that population."""
    transform = from_origin(lng, lat, 0.01, 0.01)
    profile = {
        "driver": "GTiff",
        "height": 1,
        "width": 1,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -99999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.array([[value]], dtype="float32"), 1)


def test_worldpop_sums_country_totals_for_year(tmp_path):
    """Total-population rasters for the year sum by cell across countries; other years ignored."""
    tif_dir = tmp_path / "wp"
    tif_dir.mkdir()
    _write_raster(tif_dir / "aaa_pop_2025_CN_1km_R2025A_UA_v1.tif", 100.0)
    _write_raster(tif_dir / "bbb_pop_2025_CN_1km_R2025A_UA_v1.tif", 100.0)
    _write_raster(tif_dir / "ccc_pop_2020_CN_1km_R2025A_UA_v1.tif", 50.0)

    con = connect()
    cells, total = worldpop_to_parquet(con, str(tif_dir), tmp_path / "wp.parquet", year=2025)

    assert cells == 1
    assert total == 200


def test_worldpop_timeseries_builds_wide_tiles(tmp_path):
    """Each year becomes a pop_<year> column; years without rasters are skipped."""
    tif_dir = tmp_path / "wp"
    tif_dir.mkdir()
    _write_raster(tif_dir / "aaa_pop_2015_CN_1km_R2025A_UA_v1.tif", 100.0)
    _write_raster(tif_dir / "aaa_pop_2016_CN_1km_R2025A_UA_v1.tif", 150.0)

    con = connect()
    out, scratch = tmp_path / "ts", tmp_path / "scratch"
    present, cells = worldpop_timeseries_tiles(con, str(tif_dir), out, scratch, [2015, 2016, 2017])

    assert present == [2015, 2016]
    assert cells == 1
    cols = [
        c[0]
        for c in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{out}/**/*.parquet')"
        ).fetchall()
    ]
    assert "pop_2015" in cols and "pop_2016" in cols and "pop_2017" not in cols
    row = con.execute(
        f"SELECT pop_2015, pop_2016 FROM read_parquet('{out}/**/*.parquet')"
    ).fetchone()
    assert row == (100, 150)
    assert json.loads((out / "manifest.json").read_text())["years"] == [2015, 2016]


def test_worldpop_year_parquet_sums_one_year_only(tmp_path):
    """A year job bins its own year and ignores rasters belonging to another."""
    tif_dir = tmp_path / "wp"
    tif_dir.mkdir()
    _write_raster(tif_dir / "aaa_pop_2025_CN_1km_R2025A_UA_v1.tif", 100.0)
    _write_raster(tif_dir / "bbb_pop_2025_CN_1km_R2025A_UA_v1.tif", 40.0)
    _write_raster(tif_dir / "aaa_pop_2024_CN_1km_R2025A_UA_v1.tif", 999.0)

    con = connect()
    out = tmp_path / "ts_2025.parquet"
    cells = worldpop_year_parquet(con, str(tif_dir), out, 2025)

    assert cells == 1
    row = con.execute(f"SELECT year, pop FROM read_parquet('{out}')").fetchone()
    assert row == (2025, 140)


def test_pivot_years_refuses_a_missing_year(tmp_path):
    """Publishing a year that no job produced would ship a column of nulls."""
    tif_dir, scratch = tmp_path / "wp", tmp_path / "scratch"
    tif_dir.mkdir()
    _write_raster(tif_dir / "aaa_pop_2025_CN_1km_R2025A_UA_v1.tif", 100.0)
    con = connect()
    worldpop_year_parquet(con, str(tif_dir), scratch / "ts_2025.parquet", 2025)

    with pytest.raises(FileNotFoundError, match="2026"):
        pivot_years(con, scratch, tmp_path / "tiles", [2025, 2026])


def test_pivot_batches_cover_every_parent_exactly_once(tmp_path, monkeypatch):
    """Splitting the pivot into passes must not drop, duplicate or reshuffle a cell."""
    tif_dir, scratch = tmp_path / "wp", tmp_path / "scratch"
    tif_dir.mkdir()
    places = [(85.30, 27.72), (13.40, 52.50), (151.20, -33.90), (-58.40, -34.60)]
    for index, (lng, lat) in enumerate(places):
        for year, base in ((2025, 100.0), (2026, 200.0)):
            name = f"c{index}_pop_{year}_CN_1km_R2025A_UA_v1.tif"
            _write_raster(tif_dir / name, base + index, lng, lat)
    con = connect()
    for year in (2025, 2026):
        worldpop_year_parquet(con, str(tif_dir), scratch / f"ts_{year}.parquet", year)

    single = pivot_years(con, scratch, tmp_path / "single", [2025, 2026])
    monkeypatch.setattr(worldpop, "PIVOT_BATCH_ROWS", 1)
    batched = pivot_years(con, scratch, tmp_path / "batched", [2025, 2026])

    assert single == batched == len(places)
    read = "SELECT h3, pop_2025, pop_2026 FROM read_parquet('{0}/**/*.parquet') ORDER BY h3"
    assert (
        con.execute(read.format(tmp_path / "single")).fetchall()
        == con.execute(read.format(tmp_path / "batched")).fetchall()
    )
    # A pass that skipped its filter would append every cell again.
    duplicates = con.execute(
        f"SELECT count(*) FROM (SELECT h3 FROM read_parquet('{tmp_path / 'batched'}/**/*.parquet')"
        " GROUP BY h3 HAVING count(*) > 1)"
    ).fetchone()
    assert duplicates == (0,)
