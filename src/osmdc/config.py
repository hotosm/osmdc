OVERTURE_RELEASE = "2026-06-17.0"


def overture_paths(release: str) -> tuple[str, str]:
    """Buildings and transportation-segment parquet globs for one Overture release."""
    base = f"s3://overturemaps-us-west-2/release/{release}"
    return (
        f"{base}/theme=buildings/type=building/*.parquet",
        f"{base}/theme=transportation/type=segment/*.parquet",
    )


BUILDINGS_PATH, SEGMENTS_PATH = overture_paths(OVERTURE_RELEASE)

H3_RESOLUTION = 8
# Coarse parent used to shard output into browser-fetchable tiles.
PARTITION_RESOLUTION = 2

OSM_DATASET = "OpenStreetMap"
S3_REGION = "us-west-2"

# Kontur Population (H3 resolution 8, CC BY). Download and gunzip before aggregating.
KONTUR_POPULATION_URL = (
    "https://geodata-eu-central-1-kontur-public.s3.eu-central-1.amazonaws.com"
    "/kontur_datasets/kontur_population_20231101.gpkg.gz"
)
KONTUR_POPULATION_DATE = "2023-11-01"

# WorldPop Global 2: one 1km constrained, UN-adjusted raster per country and year.
WORLDPOP_RELEASE = "R2025A"
WORLDPOP_STAC = "https://api.stac.worldpop.org"
WORLDPOP_YEAR = 2025
WORLDPOP_TS_PATH = "worldpop_ts"

# Published in the manifest so the browser can label each source.
POPULATION_SOURCES = {
    "kontur": {
        "label": "Kontur Population",
        "date": KONTUR_POPULATION_DATE,
        "url": "https://data.humdata.org/dataset/kontur-population-dataset",
    },
    "worldpop": {
        "label": "WorldPop Global 2",
        "date": f"{WORLDPOP_YEAR}, {WORLDPOP_RELEASE}",
        "url": "https://www.worldpop.org/",
    },
}
