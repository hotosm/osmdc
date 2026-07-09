from pathlib import Path

import duckdb
import pytest

from osmdc.aggregate import BoundingBox, aggregate_tile, connect, iter_chunks, merge_chunks


def _write_fixture(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> tuple[str, str]:
    """Synthetic Overture-shaped buildings and segments: 3 buildings (2 OSM), 1 road."""
    buildings = tmp_path / "buildings.parquet"
    segments = tmp_path / "segments.parquet"
    con.execute(f"""
        COPY (SELECT * FROM (VALUES
            (struct_pack(xmin := 85.3100::DOUBLE, xmax := 85.3101::DOUBLE,
                         ymin := 27.7100::DOUBLE, ymax := 27.7101::DOUBLE),
             [struct_pack(dataset := 'OpenStreetMap')]),
            (struct_pack(xmin := 85.3102::DOUBLE, xmax := 85.3103::DOUBLE,
                         ymin := 27.7102::DOUBLE, ymax := 27.7103::DOUBLE),
             [struct_pack(dataset := 'OpenStreetMap')]),
            (struct_pack(xmin := 85.3104::DOUBLE, xmax := 85.3105::DOUBLE,
                         ymin := 27.7104::DOUBLE, ymax := 27.7105::DOUBLE),
             [struct_pack(dataset := 'Microsoft')])
        ) AS t(bbox, sources)) TO '{buildings}' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (SELECT
            ST_GeomFromText('LINESTRING(85.3100 27.7100, 85.3105 27.7100)') AS geometry,
            'road' AS subtype,
            [struct_pack(dataset := 'OpenStreetMap')] AS sources,
            struct_pack(xmin := 85.3100::DOUBLE, xmax := 85.3105::DOUBLE,
                        ymin := 27.7100::DOUBLE, ymax := 27.7101::DOUBLE) AS bbox
        ) TO '{segments}' (FORMAT PARQUET)
    """)
    return str(buildings), str(segments)


def test_bbox_rejects_degenerate():
    with pytest.raises(ValueError):
        BoundingBox(85.4, 27.7, 85.3, 27.8)
    with pytest.raises(ValueError):
        BoundingBox(85.3, 27.8, 85.4, 27.7)


def test_aggregate_tile_counts_osm_share(tmp_path):
    con = connect()
    buildings_path, segments_path = _write_fixture(con, tmp_path)
    out_dir = tmp_path / "tiles"

    cells = aggregate_tile(
        con,
        BoundingBox(85.30, 27.70, 85.32, 27.72),
        out_dir,
        buildings_path=buildings_path,
        segments_path=segments_path,
    )

    assert cells >= 1
    assert list(out_dir.glob("h3_parent=*/*.parquet"))

    totals = con.execute(f"""
        SELECT sum(bld_count), sum(bld_osm), sum(road_count), sum(road_osm), sum(road_len_m)
        FROM read_parquet('{out_dir}/**/*.parquet')
    """).fetchone()
    assert totals is not None
    assert totals[0] == 3
    assert totals[1] == 2
    assert totals[2] == 1
    assert totals[3] == 1  # the one road carries an OpenStreetMap source
    assert totals[4] > 0


def test_iter_chunks_covers_range_without_overlap():
    chunks = iter_chunks(-180, 180, -60, 84, 360.0, 12.0)
    assert len(chunks) == 12  # full-width latitude bands
    assert chunks[0] == BoundingBox(-180, -60, 180, -48)
    assert chunks[-1].north == 84


def test_merge_sums_border_cells(tmp_path):
    """A cell split across two chunks must be summed, not duplicated."""
    con = connect()
    chunk_dir = tmp_path / "chunks"
    chunk_dir.mkdir()
    for name, bld, osm in [("chunk_0000", 3, 2), ("chunk_0001", 5, 1)]:
        con.execute(f"""
            COPY (SELECT '8abc' AS h3, '82' AS h3_parent,
                         {bld}::BIGINT AS bld_count, {osm}::BIGINT AS bld_osm,
                         {bld}::BIGINT AS road_count, {osm}::BIGINT AS road_osm,
                         0.0 AS road_len_m)
            TO '{chunk_dir / name}.parquet' (FORMAT PARQUET)
        """)
    out_dir = tmp_path / "out"
    cells = merge_chunks(con, chunk_dir, out_dir, 8, 2)

    assert cells == 1  # same h3 from both chunks collapses to one row
    row = con.execute(
        "SELECT bld_count, bld_osm, osm_pct, road_count, road_osm, road_pct "
        f"FROM read_parquet('{out_dir}/**/*.parquet')"
    ).fetchone()
    assert row is not None
    assert row[0] == 8
    assert row[1] == 3
    assert row[2] == 37.5
    assert (row[3], row[4], row[5]) == (8, 3, 37.5)


def test_merge_joins_population_and_gap(tmp_path):
    """Population joins onto cells, and gap weights people by building incompleteness."""
    import h3

    con = connect()
    chunk_dir = tmp_path / "chunks"
    chunk_dir.mkdir()
    cell_a = h3.latlng_to_cell(27.70, 85.30, 8)
    cell_b = h3.latlng_to_cell(52.50, 13.40, 8)
    parent_a = h3.cell_to_parent(cell_a, 2)

    con.execute(f"""
        COPY (SELECT '{cell_a}' AS h3, '{parent_a}' AS h3_parent,
                     10::BIGINT AS bld_count, 5::BIGINT AS bld_osm,
                     0::BIGINT AS road_count, 0::BIGINT AS road_osm, 0.0 AS road_len_m)
        TO '{chunk_dir / "chunk_0000"}.parquet' (FORMAT PARQUET)
    """)
    pop = tmp_path / "pop.parquet"
    con.execute(f"""
        COPY (SELECT * FROM (VALUES ('{cell_a}', 1000.0), ('{cell_b}', 500.0)) AS t(h3, population))
        TO '{pop}' (FORMAT PARQUET)
    """)

    cells = merge_chunks(con, chunk_dir, tmp_path / "out", 8, 2, {"kontur": str(pop)})
    assert cells == 2  # cell_a (buildings + people) and cell_b (people only)

    rows = {
        r[0]: r
        for r in con.execute(
            f"SELECT h3, bld_count, population_kontur, gap_score_kontur "
            f"FROM read_parquet('{tmp_path / 'out'}/**/*.parquet')"
        ).fetchall()
    }
    assert rows[cell_a][1:] == (10, 1000, 500)  # 50% complete x 1000 people -> 500 gap
    assert rows[cell_b][1:] == (0, 500, 500)  # no buildings -> full 500 gap
