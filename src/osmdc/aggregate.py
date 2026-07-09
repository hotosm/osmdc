"""Aggregate Overture buildings and roads into per-H3-cell completeness metrics."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb

from osmdc import config


@dataclass(frozen=True)
class BoundingBox:
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if self.west >= self.east or self.south >= self.north:
            raise ValueError(f"degenerate bbox: {self}")


def connect(
    threads: int | None = None,
    memory_limit: str | None = None,
    temp_dir: str | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the spatial, h3 and httpfs extensions loaded.

    threads, memory_limit and temp_dir cap resource use so a long run can share a
    machine with other workloads. memory_limit is a DuckDB size string, e.g. "16GB".
    """
    con = duckdb.connect()
    for extension in ("spatial", "httpfs"):
        con.execute(f"INSTALL {extension}; LOAD {extension};")
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    con.execute(f"SET s3_region='{config.S3_REGION}';")
    if threads is not None:
        con.execute(f"SET threads={threads};")
    if memory_limit is not None:
        con.execute(f"SET memory_limit='{memory_limit}';")
    if temp_dir is not None:
        con.execute(f"SET temp_directory='{temp_dir}';")
    return con


def _cell_expr(resolution: int) -> str:
    """H3 cell of a feature, taken from its bbox centre to avoid decoding geometry."""
    return (
        "h3_latlng_to_cell("
        "(bbox.ymin + bbox.ymax) / 2.0, (bbox.xmin + bbox.xmax) / 2.0, "
        f"{resolution})"
    )


def _bbox_filter(bbox: BoundingBox) -> str:
    return (
        f"bbox.xmin BETWEEN {bbox.west} AND {bbox.east} "
        f"AND bbox.ymin BETWEEN {bbox.south} AND {bbox.north}"
    )


def _feature_ctes(cell: str, where: str, buildings_path: str, segments_path: str) -> str:
    """CTEs of per-cell building and road counts, with OSM source share, for one area."""
    osm_present = f"len(list_filter(sources, s -> s.dataset = '{config.OSM_DATASET}')) > 0"
    return f"""
    WITH buildings AS (
        SELECT {cell} AS cell,
               count(*) AS bld_count,
               count(*) FILTER (WHERE {osm_present}) AS bld_osm
        FROM read_parquet('{buildings_path}', hive_partitioning=1)
        WHERE {where}
        GROUP BY 1
    ),
    roads AS (
        SELECT {cell} AS cell,
               count(*) AS road_count,
               count(*) FILTER (WHERE {osm_present}) AS road_osm,
               sum(CASE WHEN isfinite(ST_Length_Spheroid(geometry))
                        THEN ST_Length_Spheroid(geometry) ELSE 0 END) AS road_len_m
        FROM read_parquet('{segments_path}', hive_partitioning=1)
        WHERE {where} AND subtype = 'road'
        GROUP BY 1
    ),
    merged AS (
        SELECT coalesce(buildings.cell, roads.cell) AS cell,
               coalesce(bld_count, 0) AS bld_count,
               coalesce(bld_osm, 0) AS bld_osm,
               coalesce(road_count, 0) AS road_count,
               coalesce(road_osm, 0) AS road_osm,
               coalesce(road_len_m, 0.0) AS road_len_m
        FROM buildings FULL OUTER JOIN roads ON buildings.cell = roads.cell
    )"""


def _cells_query(
    bbox: BoundingBox,
    resolution: int,
    partition_resolution: int,
    buildings_path: str,
    segments_path: str,
) -> str:
    ctes = _feature_ctes(_cell_expr(resolution), _bbox_filter(bbox), buildings_path, segments_path)
    return f"""{ctes}
    SELECT h3_h3_to_string(cell) AS h3,
           h3_h3_to_string(h3_cell_to_parent(cell, {partition_resolution})) AS h3_parent,
           bld_count,
           bld_osm,
           round(bld_osm * 100.0 / nullif(bld_count, 0), 1) AS osm_pct,
           road_count,
           road_osm,
           round(road_osm * 100.0 / nullif(road_count, 0), 1) AS road_pct,
           round(road_len_m, 1) AS road_len_m
    FROM merged
    """


def aggregate_tile(
    con: duckdb.DuckDBPyConnection,
    bbox: BoundingBox,
    out_dir: Path,
    resolution: int = config.H3_RESOLUTION,
    partition_resolution: int = config.PARTITION_RESOLUTION,
    buildings_path: str = config.BUILDINGS_PATH,
    segments_path: str = config.SEGMENTS_PATH,
    overture_release: str = config.OVERTURE_RELEASE,
) -> int:
    """Write per-cell metrics for one bbox as parquet sharded by coarse H3 parent.

    Returns the number of cells written.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    query = _cells_query(bbox, resolution, partition_resolution, buildings_path, segments_path)
    con.execute(
        f"COPY ({query}) TO '{out_dir}' "
        "(FORMAT PARQUET, PARTITION_BY (h3_parent), COMPRESSION zstd)"
    )
    count_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{out_dir}/**/*.parquet')"
    ).fetchone()
    assert count_row is not None
    write_manifest(out_dir, resolution, partition_resolution, overture_release)
    return count_row[0]


def write_manifest(
    out_dir: Path,
    resolution: int,
    partition_resolution: int,
    overture_release: str = config.OVERTURE_RELEASE,
    populations: list[dict[str, str]] | None = None,
) -> None:
    """Write a manifest mapping each coarse H3 parent to its tile path for the browser."""
    tiles = {
        path.parent.name.removeprefix("h3_parent="): str(path.relative_to(out_dir))
        for path in sorted(out_dir.glob("h3_parent=*/*.parquet"))
    }
    manifest = {
        "resolution": resolution,
        "partition_resolution": partition_resolution,
        "overture_release": overture_release,
        "populations": populations or [],
        "tiles": tiles,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def iter_chunks(
    lon_min: float,
    lon_max: float,
    lat_min: float,
    lat_max: float,
    lon_step: float,
    lat_step: float,
) -> list[BoundingBox]:
    """Tile a lon/lat range into chunk boxes covered without overlap."""
    chunks = []
    lat = lat_min
    while lat < lat_max:
        top = min(lat + lat_step, lat_max)
        lon = lon_min
        while lon < lon_max:
            right = min(lon + lon_step, lon_max)
            chunks.append(BoundingBox(lon, lat, right, top))
            lon += lon_step
        lat += lat_step
    return chunks


def _chunk_filter(bbox: BoundingBox) -> str:
    """Half-open box on each feature's bbox corner: every feature lands in one chunk."""
    return (
        f"bbox.xmin >= {bbox.west} AND bbox.xmin < {bbox.east} "
        f"AND bbox.ymin >= {bbox.south} AND bbox.ymin < {bbox.north}"
    )


def _raw_chunk_query(
    bbox: BoundingBox,
    resolution: int,
    partition_resolution: int,
    buildings_path: str,
    segments_path: str,
) -> str:
    """Per-cell partial sums for one chunk. Completeness is derived later, after merge."""
    ctes = _feature_ctes(_cell_expr(resolution), _chunk_filter(bbox), buildings_path, segments_path)
    return f"""{ctes}
    SELECT h3_h3_to_string(cell) AS h3,
           h3_h3_to_string(h3_cell_to_parent(cell, {partition_resolution})) AS h3_parent,
           bld_count, bld_osm, road_count, road_osm, road_len_m
    FROM merged
    """


def merge_chunks(
    con: duckdb.DuckDBPyConnection,
    chunk_dir: Path,
    out_dir: Path,
    resolution: int,
    partition_resolution: int,
    population_sources: dict[str, str] | None = None,
    overture_release: str = config.OVERTURE_RELEASE,
) -> int:
    """Sum partial cells across all chunks into browser tiles, joining each population.

    Every named population source contributes population_<name> and gap_score_<name>
    columns. The cell set is the union of chunk cells and every source's cells, so
    people living where no building is mapped still appear. gap_score weights a
    source's population by how incomplete the buildings are.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    population_sources = population_sources or {}
    chunk_columns = {
        row[0]
        for row in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{chunk_dir}/chunk_*.parquet')"
        ).fetchall()
    }
    # Treat road_osm as zero when the chunks do not carry that column.
    road_osm_agg = "sum(road_osm)" if "road_osm" in chunk_columns else "0"
    completeness = "coalesce(c.bld_osm * 1.0 / nullif(c.bld_count, 0), 0)"

    pop_ctes, key_unions, pop_selects, pop_joins = [], ["SELECT h3 FROM chunk_cells"], [], []
    for name, path in population_sources.items():
        alias = f"pop_{name}"
        pop_ctes.append(f"{alias} AS (SELECT h3, population FROM read_parquet('{path}'))")
        key_unions.append(f"SELECT h3 FROM {alias}")
        pop_selects.append(f"coalesce({alias}.population, 0) AS population_{name}")
        pop_selects.append(
            f"round(coalesce({alias}.population, 0) * (1 - {completeness})) AS gap_score_{name}"
        )
        pop_joins.append(f"LEFT JOIN {alias} ON k.h3 = {alias}.h3")

    ctes = [
        f"""chunk_cells AS (
            SELECT h3,
                   any_value(h3_parent) AS h3_parent,
                   sum(bld_count) AS bld_count,
                   sum(bld_osm) AS bld_osm,
                   sum(road_count) AS road_count,
                   {road_osm_agg} AS road_osm,
                   sum(CASE WHEN isfinite(road_len_m) THEN road_len_m ELSE 0 END) AS road_len_m
            FROM read_parquet('{chunk_dir}/chunk_*.parquet')
            GROUP BY h3
        )""",
        *pop_ctes,
        f"keys AS ({' UNION '.join(key_unions)})",
    ]
    select_cols = [
        "k.h3 AS h3",
        "coalesce(c.h3_parent, "
        f"h3_h3_to_string(h3_cell_to_parent(h3_string_to_h3(k.h3), {partition_resolution}))"
        ") AS h3_parent",
        "coalesce(c.bld_count, 0) AS bld_count",
        "coalesce(c.bld_osm, 0) AS bld_osm",
        "round(coalesce(c.bld_osm, 0) * 100.0 / nullif(coalesce(c.bld_count, 0), 0), 1) AS osm_pct",
        "coalesce(c.road_count, 0) AS road_count",
        "coalesce(c.road_osm, 0) AS road_osm",
        "round(coalesce(c.road_osm, 0) * 100.0 "
        "/ nullif(coalesce(c.road_count, 0), 0), 1) AS road_pct",
        "round(coalesce(c.road_len_m, 0), 1) AS road_len_m",
        *pop_selects,
    ]
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE merged_cells AS
        WITH {", ".join(ctes)}
        SELECT {", ".join(select_cols)}
        FROM keys k
        LEFT JOIN chunk_cells c ON k.h3 = c.h3
        {" ".join(pop_joins)}
    """)
    con.execute(
        f"COPY merged_cells TO '{out_dir}' "
        "(FORMAT PARQUET, PARTITION_BY (h3_parent), COMPRESSION zstd)"
    )
    _compact_partitions(con, out_dir)
    populations = [
        {"name": name, **config.POPULATION_SOURCES.get(name, {"label": name})}
        for name in population_sources
    ]
    write_manifest(out_dir, resolution, partition_resolution, overture_release, populations)
    count_row = con.execute("SELECT count(*) FROM merged_cells").fetchone()
    assert count_row is not None
    return count_row[0]


def _compact_partitions(con: duckdb.DuckDBPyConnection, out_dir: Path) -> None:
    """Collapse each partition's parallel-write shards into one tile file per parent."""
    for partition in sorted(out_dir.glob("h3_parent=*")):
        shards = list(partition.glob("*.parquet"))
        if len(shards) <= 1:
            continue
        compacted = out_dir / f"{partition.name}.compact.parquet"
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{partition}/*.parquet')) "
            f"TO '{compacted}' (FORMAT PARQUET, COMPRESSION zstd)"
        )
        for shard in shards:
            shard.unlink()
        compacted.rename(partition / "data_0.parquet")


def run_global(
    con: duckdb.DuckDBPyConnection,
    out_dir: Path,
    chunk_dir: Path,
    lon_min: float = -180.0,
    lon_max: float = 180.0,
    lat_min: float = -60.0,
    lat_max: float = 84.0,
    lon_step: float = 360.0,
    lat_step: float = 12.0,
    resolution: int = config.H3_RESOLUTION,
    partition_resolution: int = config.PARTITION_RESOLUTION,
    buildings_path: str = config.BUILDINGS_PATH,
    segments_path: str = config.SEGMENTS_PATH,
    population_sources: dict[str, str] | None = None,
    overture_release: str = config.OVERTURE_RELEASE,
) -> int:
    """Aggregate a lon/lat range chunk by chunk, then merge into browser tiles.

    Each chunk is written atomically; a present chunk file means done, so a rerun
    resumes where it stopped. Network errors on a chunk are logged and retried on
    the next run rather than aborting the whole job.
    """
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks = iter_chunks(lon_min, lon_max, lat_min, lat_max, lon_step, lat_step)
    for index, bbox in enumerate(chunks):
        target = chunk_dir / f"chunk_{index:04d}.parquet"
        label = f"[{index + 1}/{len(chunks)}] {bbox}"
        if target.exists():
            print(f"{label} skip (done)", flush=True)
            continue
        query = _raw_chunk_query(
            bbox, resolution, partition_resolution, buildings_path, segments_path
        )
        temp = target.with_suffix(".parquet.tmp")
        try:
            con.execute(f"COPY ({query}) TO '{temp}' (FORMAT PARQUET, COMPRESSION zstd)")
        except duckdb.IOException as error:
            print(f"{label} network error, will retry on resume: {error}", flush=True)
            continue
        os.replace(temp, target)
        cells = con.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()
        assert cells is not None
        print(f"{label} -> {cells[0]} cells", flush=True)
    total = merge_chunks(
        con,
        chunk_dir,
        out_dir,
        resolution,
        partition_resolution,
        population_sources,
        overture_release,
    )
    print(f"merged {total} cells to {out_dir}", flush=True)
    return total
