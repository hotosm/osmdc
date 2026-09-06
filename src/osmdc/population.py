"""Aggregate the Kontur Population GeoPackage, already an H3 resolution 8 grid, into parquet.

Download and decompress it from config.KONTUR_POPULATION_URL first.
"""

from pathlib import Path

import duckdb


def kontur_to_parquet(
    con: duckdb.DuckDBPyConnection, gpkg_path: str, out_path: Path
) -> tuple[int, float]:
    """Write an (h3, population) parquet from the Kontur GeoPackage. Returns (cells, total)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
            SELECT h3, round(sum(population)) AS population
            FROM ST_Read('{gpkg_path}')
            GROUP BY h3
        ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION zstd)
    """)
    summary = con.execute(
        f"SELECT count(*), coalesce(sum(population), 0) FROM read_parquet('{out_path}')"
    ).fetchone()
    assert summary is not None
    return summary[0], summary[1]
