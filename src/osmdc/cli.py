"""Command line entrypoint for the aggregation pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path

from osmdc import config
from osmdc.aggregate import BoundingBox, aggregate_tile, connect, merge_chunks, run_global
from osmdc.population import kontur_to_parquet
from osmdc.publish import publish_tiles
from osmdc.worldpop import worldpop_timeseries_tiles, worldpop_to_parquet


def _add_throttle_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--threads", type=int, default=None, help="cap DuckDB worker threads")
    parser.add_argument("--memory-limit", default=None, help="DuckDB memory cap, e.g. 16GB")
    parser.add_argument("--temp-dir", default=None, help="directory for DuckDB spill files")


def _parse_population_sources(pairs: list[str] | None) -> dict[str, str]:
    """Turn repeated NAME=PATH arguments into an ordered {name: path} mapping."""
    sources: dict[str, str] = {}
    for pair in pairs or []:
        name, _, path = pair.partition("=")
        if not name or not path:
            raise ValueError(f"expected NAME=PATH, got {pair!r}")
        sources[name] = path
    return sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osmdc", description="Overture to H3 completeness tiles")
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        required=True,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="bounding box in lon/lat degrees",
    )
    parser.add_argument("--out", type=Path, required=True, help="output tile directory")
    parser.add_argument("--resolution", type=int, default=config.H3_RESOLUTION)
    parser.add_argument("--partition-resolution", type=int, default=config.PARTITION_RESOLUTION)
    parser.add_argument("--release", default=config.OVERTURE_RELEASE, help="Overture release id")
    _add_throttle_args(parser)
    return parser


def build_planet_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osmdc-planet", description="Chunked, resumable global Overture to H3 aggregation"
    )
    parser.add_argument("--out", type=Path, required=True, help="merged browser tile directory")
    parser.add_argument("--chunk-dir", type=Path, required=True, help="per-chunk scratch directory")
    parser.add_argument("--lon-min", type=float, default=-180.0)
    parser.add_argument("--lon-max", type=float, default=180.0)
    parser.add_argument("--lat-min", type=float, default=-60.0)
    parser.add_argument("--lat-max", type=float, default=84.0)
    parser.add_argument("--lon-step", type=float, default=360.0)
    parser.add_argument("--lat-step", type=float, default=12.0)
    parser.add_argument("--resolution", type=int, default=config.H3_RESOLUTION)
    parser.add_argument("--partition-resolution", type=int, default=config.PARTITION_RESOLUTION)
    parser.add_argument("--release", default=config.OVERTURE_RELEASE, help="Overture release id")
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="skip the chunk scan and only merge existing chunks into tiles",
    )
    parser.add_argument(
        "--population",
        action="append",
        metavar="NAME=PATH",
        help="population parquet to join, e.g. kontur=kontur.parquet (repeatable)",
    )
    _add_throttle_args(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    bbox = BoundingBox(*args.bbox)
    buildings_path, segments_path = config.overture_paths(args.release)
    con = connect(args.threads, args.memory_limit, args.temp_dir)
    cells = aggregate_tile(
        con,
        bbox,
        args.out,
        args.resolution,
        args.partition_resolution,
        buildings_path,
        segments_path,
        args.release,
    )
    print(f"wrote {cells} cells to {args.out}")


def publish_main() -> None:
    parser = argparse.ArgumentParser(
        prog="osmdc-publish", description="Upload tiles to a Hugging Face dataset"
    )
    parser.add_argument("--tiles", type=Path, required=True, help="merged tile directory")
    parser.add_argument("--repo", required=True, help="HF dataset id, e.g. user/name")
    parser.add_argument("--token", default=None, help="HF token (defaults to cached login)")
    parser.add_argument(
        "--path-in-repo", default=None, help="subfolder in the repo, e.g. worldpop_ts"
    )
    args = parser.parse_args()
    url = publish_tiles(args.tiles, args.repo, args.token, args.path_in_repo)
    print(f"published to {url}")


def population_main() -> None:
    parser = argparse.ArgumentParser(
        prog="osmdc-population",
        description="Aggregate Kontur population to (h3, population) parquet",
    )
    parser.add_argument("--gpkg", required=True, help="path to the decompressed Kontur .gpkg")
    parser.add_argument("--out", type=Path, required=True, help="output parquet path")
    _add_throttle_args(parser)
    args = parser.parse_args()
    con = connect(args.threads, args.memory_limit, args.temp_dir)
    cells, total = kontur_to_parquet(con, args.gpkg, args.out)
    print(f"wrote {cells} cells, total population {total:,.0f} to {args.out}")


def worldpop_main() -> None:
    parser = argparse.ArgumentParser(
        prog="osmdc-worldpop",
        description="Aggregate WorldPop Global 2 rasters to (h3, population) parquet",
    )
    parser.add_argument("--tif-dir", required=True, help="directory of unzipped WorldPop GeoTIFFs")
    parser.add_argument("--out", type=Path, required=True, help="output parquet path")
    parser.add_argument("--year", type=int, default=config.WORLDPOP_YEAR)
    _add_throttle_args(parser)
    args = parser.parse_args()
    con = connect(args.threads, args.memory_limit, args.temp_dir)
    cells, total = worldpop_to_parquet(con, args.tif_dir, args.out, args.year)
    print(f"wrote {cells} cells, total population {total:,.0f} to {args.out}")


def worldpop_ts_main() -> None:
    parser = argparse.ArgumentParser(
        prog="osmdc-worldpop-ts",
        description="Build wide (h3, pop_<year>...) WorldPop time-series tiles",
    )
    parser.add_argument("--tif-dir", required=True, help="directory of WorldPop GeoTIFFs")
    parser.add_argument("--out", type=Path, required=True, help="output tile directory")
    parser.add_argument("--scratch-dir", type=Path, required=True, help="per-year scratch parquet")
    parser.add_argument("--year-min", type=int, default=2015)
    parser.add_argument("--year-max", type=int, default=2030)
    _add_throttle_args(parser)
    args = parser.parse_args()
    con = connect(args.threads, args.memory_limit, args.temp_dir)
    years = list(range(args.year_min, args.year_max + 1))
    present, cells = worldpop_timeseries_tiles(con, args.tif_dir, args.out, args.scratch_dir, years)
    print(f"wrote {cells} cells for years {present[0]}-{present[-1]} to {args.out}")


def planet_main() -> None:
    args = build_planet_parser().parse_args()
    population_sources = _parse_population_sources(args.population)
    buildings_path, segments_path = config.overture_paths(args.release)
    con = connect(args.threads, args.memory_limit, args.temp_dir)
    if args.merge_only:
        cells = merge_chunks(
            con,
            args.chunk_dir,
            args.out,
            args.resolution,
            args.partition_resolution,
            population_sources,
            args.release,
        )
        print(f"merged {cells} cells to {args.out}")
        return
    run_global(
        con,
        args.out,
        args.chunk_dir,
        lon_min=args.lon_min,
        lon_max=args.lon_max,
        lat_min=args.lat_min,
        lat_max=args.lat_max,
        lon_step=args.lon_step,
        lat_step=args.lat_step,
        resolution=args.resolution,
        partition_resolution=args.partition_resolution,
        buildings_path=buildings_path,
        segments_path=segments_path,
        population_sources=population_sources,
        overture_release=args.release,
    )


if __name__ == "__main__":
    main()
