"""Track the WorldPop STAC catalogue and rebuild the published tiles when it moves."""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import httpx
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import EntryNotFoundError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from osmdc import config

# 1km constrained, UN-adjusted total population: the raster form worldpop.py bins.
ASSET_ID = re.compile(r"^([a-z]{3})_pop_(\d{4})_CN_1km_(R\d{4}[A-Z])_UA_v1$")
TS_MANIFEST = "manifest.json"
WORKERS = 8

# Thousands of requests against one public host, so a dropped connection is retried.
on_transport_error = retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(max=10),
    reraise=True,
)


@dataclass(frozen=True)
class Asset:
    iso: str
    year: int
    release: str
    href: str


@dataclass(frozen=True)
class Catalog:
    """One WorldPop release: the raster href for each country, grouped by year."""

    release: str
    assets: dict[int, dict[str, str]]

    @property
    def years(self) -> list[int]:
        return sorted(self.assets)


@dataclass(frozen=True)
class Plan:
    catalog: Catalog
    published_release: str | None
    published_years: list[int]

    @property
    def needs_update(self) -> bool:
        published = (self.published_release, self.published_years)
        return published != (self.catalog.release, self.catalog.years)


def client() -> httpx.Client:
    """HTTP client shared across a catalogue sweep or a year of downloads."""
    return httpx.Client(timeout=120.0, follow_redirects=True)


def collection_ids(http: httpx.Client) -> list[str]:
    """Every ISO3 country collection the STAC API offers, in one page or not at all."""
    response = http.get(f"{config.WORLDPOP_STAC}/collections")
    response.raise_for_status()
    payload = response.json()
    if payload["numberReturned"] != payload["numberMatched"]:
        raise RuntimeError(
            f"{config.WORLDPOP_STAC}/collections returned {payload['numberReturned']} of "
            f"{payload['numberMatched']} collections; the sweep would cover part of the world"
        )
    return [collection["id"] for collection in payload["collections"]]


@on_transport_error
def country_assets(http: httpx.Client, iso: str) -> list[Asset]:
    """The 1km population rasters listed for one country, across every release and year.

    The query and fields extensions trim the response from 1.4MB to 21KB per country.
    """
    body: dict | None = {
        "collections": [iso],
        "limit": 200,
        "query": {"project": {"eq": "Population"}},
        "fields": {"include": ["id", "assets.data.href", "properties.Release"]},
    }
    assets = []
    while body is not None:
        response = http.post(f"{config.WORLDPOP_STAC}/search", json=body)
        response.raise_for_status()
        page = response.json()
        for feature in page["features"]:
            match = ASSET_ID.match(feature["id"])
            if match is None:
                continue
            country, year, release = match.groups()
            assets.append(Asset(country, int(year), release, feature["assets"]["data"]["href"]))
        body = next(
            (link["body"] for link in page.get("links", []) if link.get("rel") == "next"), None
        )
    return assets


def worldpop_catalog(http: httpx.Client) -> Catalog:
    """Sweep the STAC API and keep the highest release it offers, by year and country.

    A release only part of the world carries is refused: publishing it would replace a
    complete planet with a partial one.
    """
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        found = [
            asset
            for country in pool.map(partial(country_assets, http), collection_ids(http))
            for asset in country
        ]
    if not found:
        raise RuntimeError(f"no population rasters listed by {config.WORLDPOP_STAC}")
    release = max(asset.release for asset in found)
    current = {asset.iso for asset in found if asset.release == release}
    lagging = {asset.iso for asset in found} - current
    if lagging:
        raise RuntimeError(
            f"{len(lagging)} of {len(current) + len(lagging)} countries are behind {release}; "
            "rerun once the rollover covers every country"
        )
    by_year: dict[int, dict[str, str]] = {}
    for asset in found:
        if asset.release == release:
            by_year.setdefault(asset.year, {})[asset.iso] = asset.href
    if len({frozenset(countries) for countries in by_year.values()}) != 1:
        raise RuntimeError(f"{release} does not list the same countries in every year")
    return Catalog(release, by_year)


def published_manifest(repo_id: str, token: str | None = None) -> dict:
    """The worldpop_ts manifest on the Hub, empty when the dataset carries no tiles yet.

    A dataset that cannot be read at all raises, so a bad token never reads as an empty
    dataset and triggers a rebuild that then overwrites it.
    """
    try:
        path = hf_hub_download(repo_id, TS_MANIFEST, repo_type="dataset", token=token)
    except EntryNotFoundError:
        return {}
    return json.loads(Path(path).read_text())


def build_plan(http: httpx.Client, repo_id: str, token: str | None = None) -> Plan:
    """Compare the live catalogue with what the Hub serves today."""
    manifest = published_manifest(repo_id, token)
    return Plan(worldpop_catalog(http), manifest.get("release"), manifest.get("years", []))


def plan_json(plan: Plan) -> dict:
    """Serialise a plan, including every asset href the year jobs will download."""
    return {
        "release": plan.catalog.release,
        "years": plan.catalog.years,
        "needs_update": plan.needs_update,
        "published_release": plan.published_release,
        "published_years": plan.published_years,
        "assets": {str(year): plan.catalog.assets[year] for year in plan.catalog.years},
    }


def read_plan(path: Path) -> Plan:
    data = json.loads(path.read_text())
    catalog = Catalog(data["release"], {int(y): hrefs for y, hrefs in data["assets"].items()})
    return Plan(catalog, data["published_release"], data["published_years"])


def download_year(http: httpx.Client, catalog: Catalog, year: int, dest_dir: Path) -> int:
    """Fetch every country raster for one year into dest_dir. Returns the file count."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    hrefs = catalog.assets[year]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(partial(_download, http, dest_dir), hrefs.values()))
    return len(hrefs)


@on_transport_error
def _download(http: httpx.Client, dest_dir: Path, href: str) -> None:
    """Stream one raster to its STAC file name, replaced atomically once complete."""
    name = href.rsplit("/", 1)[-1]
    if ASSET_ID.match(name.removesuffix(".tif")) is None:
        raise ValueError(f"unexpected raster name {name!r} in {href}")
    target = dest_dir / name
    temp = target.with_suffix(".tif.part")
    with http.stream("GET", href) as response:
        response.raise_for_status()
        with temp.open("wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                handle.write(chunk)
    temp.replace(target)


def verify_published(tiles_dir: Path, repo_id: str, token: str | None = None) -> None:
    """Fail when the Hub is missing a file the upload should have placed under the tiles.

    An upload that stops partway can leave the manifest published without every tile,
    which the next plan would read as nothing to do.
    """
    local = {str(path.relative_to(tiles_dir)) for path in tiles_dir.rglob("*") if path.is_file()}
    published = set(HfApi(token=token).list_repo_files(repo_id, repo_type="dataset"))
    missing = sorted(local - published)
    if missing:
        raise RuntimeError(
            f"{len(missing)} of {len(local)} files did not reach {repo_id}, "
            f"starting with {missing[:3]}"
        )
