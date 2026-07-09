import json

import numpy as np
import rasterio
from rasterio.transform import from_origin

from osmdc.aggregate import connect
from osmdc.worldpop import worldpop_timeseries_tiles, worldpop_to_parquet


def _write_raster(path, value):
    """One-pixel WGS84 raster centred at 85.305, 27.715 with the given population."""
    transform = from_origin(85.30, 27.72, 0.01, 0.01)
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
