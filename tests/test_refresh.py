import json

import httpx
import pytest
from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

from osmdc import config, refresh


def _item(item_id: str, release: str) -> dict:
    year = item_id.split("_")[2]
    iso = item_id.split("_")[0]
    href = f"https://data.worldpop.org/GIS/Population/{year}/{iso.upper()}/{item_id}.tif"
    return {"id": item_id, "properties": {"Release": release}, "assets": {"data": {"href": href}}}


def _stac_client(pages: dict[str, dict], returned: int | None = None) -> httpx.Client:
    """A client serving canned STAC responses, keyed by collection id for searches."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/collections":
            collections = [{"id": iso} for iso in pages]
            body = {
                "collections": collections,
                "numberReturned": returned if returned is not None else len(collections),
                "numberMatched": len(collections),
            }
            return httpx.Response(200, json=body)
        iso = json.loads(request.content)["collections"][0]
        return httpx.Response(200, json=pages[iso])

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=config.WORLDPOP_STAC)


def test_catalog_keeps_only_1km_constrained_rasters():
    """The 100m and older-release items must not reach the binning stage."""
    pages = {
        "NPL": {
            "features": [
                _item("npl_pop_2025_CN_1km_R2025A_UA_v1", "R2025A"),
                _item("npl_pop_2026_CN_1km_R2025A_UA_v1", "R2025A"),
                _item("npl_pop_2025_CN_100m_R2025A_v1", "R2025A"),
                _item("npl_pop_2025_CN_1km_R2024A_UA_v1", "R2024A"),
            ]
        },
        "BTN": {
            "features": [
                _item("btn_pop_2025_CN_1km_R2025A_UA_v1", "R2025A"),
                _item("btn_pop_2026_CN_1km_R2025A_UA_v1", "R2025A"),
            ]
        },
    }
    with _stac_client(pages) as http:
        catalog = refresh.worldpop_catalog(http)

    assert catalog.release == "R2025A"
    assert catalog.years == [2025, 2026]
    assert sorted(catalog.assets[2025]) == ["btn", "npl"]
    assert catalog.assets[2025]["npl"].endswith("npl_pop_2025_CN_1km_R2025A_UA_v1.tif")


def test_catalog_refuses_a_partial_release_rollover():
    """A release only some countries carry would shrink the published planet."""
    pages = {
        "NPL": {"features": [_item("npl_pop_2025_CN_1km_R2026A_UA_v1", "R2026A")]},
        "BTN": {"features": [_item("btn_pop_2025_CN_1km_R2025A_UA_v1", "R2025A")]},
    }
    with _stac_client(pages) as http, pytest.raises(RuntimeError, match="R2026A"):
        refresh.worldpop_catalog(http)


def test_catalog_refuses_a_year_missing_a_country():
    """Every year must list the same countries, or one year publishes a smaller world."""
    pages = {
        "NPL": {
            "features": [
                _item("npl_pop_2025_CN_1km_R2025A_UA_v1", "R2025A"),
                _item("npl_pop_2026_CN_1km_R2025A_UA_v1", "R2025A"),
            ]
        },
        "BTN": {"features": [_item("btn_pop_2025_CN_1km_R2025A_UA_v1", "R2025A")]},
    }
    with _stac_client(pages) as http, pytest.raises(RuntimeError, match="every year"):
        refresh.worldpop_catalog(http)


def test_collection_sweep_refuses_a_truncated_listing():
    """A paginated collection listing would sweep part of the world without saying so."""
    pages = {"NPL": {"features": []}, "BTN": {"features": []}}
    with _stac_client(pages, returned=1) as http, pytest.raises(RuntimeError, match="part of the"):
        refresh.collection_ids(http)


def test_catalog_follows_pagination():
    """A second page of items must be read, not silently dropped.

    The live API repeats the whole request body in the next link and adds a token, so
    the body is replaced rather than merged.
    """
    first = {
        "features": [_item("npl_pop_2025_CN_1km_R2025A_UA_v1", "R2025A")],
        "links": [{"rel": "next", "body": {"collections": ["NPL"], "token": "page2"}}],
    }
    second = {"features": [_item("npl_pop_2026_CN_1km_R2025A_UA_v1", "R2025A")]}
    responses = iter([first, second])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assets = refresh.country_assets(http, "NPL")

    assert [asset.year for asset in assets] == [2025, 2026]


def test_catalog_rejects_an_empty_sweep():
    with _stac_client({"NPL": {"features": []}}) as http, pytest.raises(RuntimeError):
        refresh.worldpop_catalog(http)


def _plan(release: str, years: list[int], published_release: str | None, published: list[int]):
    assets = {year: {"npl": f"https://example.invalid/{year}.tif"} for year in years}
    return refresh.Plan(refresh.Catalog(release, assets), published_release, published)


def test_needs_update_tracks_release_and_years():
    assert not _plan("R2025A", [2025, 2026], "R2025A", [2025, 2026]).needs_update
    assert _plan("R2026A", [2025, 2026], "R2025A", [2025, 2026]).needs_update
    assert _plan("R2025A", [2025, 2026, 2027], "R2025A", [2025, 2026]).needs_update
    assert _plan("R2025A", [2025], None, []).needs_update


def test_plan_round_trips_through_json(tmp_path):
    """The year jobs read the plan back from disk, so the asset hrefs must survive it."""
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(refresh.plan_json(_plan("R2025A", [2025], "R2024A", [2024]))))

    restored = refresh.read_plan(path)

    assert restored.catalog.release == "R2025A"
    assert restored.catalog.years == [2025]
    assert restored.catalog.assets[2025]["npl"] == "https://example.invalid/2025.tif"
    assert restored.needs_update


def test_download_year_writes_every_raster(tmp_path):
    """Each country's raster lands under the STAC file name worldpop.py looks for."""
    catalog = refresh.Catalog(
        "R2025A",
        {
            2025: {
                "npl": "https://example.invalid/npl_pop_2025_CN_1km_R2025A_UA_v1.tif",
                "btn": "https://example.invalid/btn_pop_2025_CN_1km_R2025A_UA_v1.tif",
            }
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"raster-bytes")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        count = refresh.download_year(http, catalog, 2025, tmp_path / "rasters")

    written = sorted(path.name for path in (tmp_path / "rasters").glob("*"))
    assert count == 2
    assert written == [
        "btn_pop_2025_CN_1km_R2025A_UA_v1.tif",
        "npl_pop_2025_CN_1km_R2025A_UA_v1.tif",
    ]


def test_download_year_fails_loudly_on_a_missing_raster(tmp_path):
    href = "https://example.invalid/npl_pop_2025_CN_1km_R2025A_UA_v1.tif"
    catalog = refresh.Catalog("R2025A", {2025: {"npl": href}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with client as http, pytest.raises(httpx.HTTPStatusError):
        refresh.download_year(http, catalog, 2025, tmp_path / "rasters")


def test_published_manifest_is_empty_when_the_dataset_has_no_tiles(monkeypatch):
    """A dataset without the manifest is a first run, not a failure."""

    def missing(*args, **kwargs):
        raise EntryNotFoundError("no such file")

    monkeypatch.setattr(refresh, "hf_hub_download", missing)
    assert refresh.published_manifest("user/dataset") == {}


def test_published_manifest_raises_when_the_dataset_cannot_be_read(monkeypatch):
    """A gated or misnamed dataset must not read as empty and trigger an overwrite."""

    def unreadable(*args, **kwargs):
        raise RepositoryNotFoundError(
            "401 Client Error",
            response=httpx.Response(404, request=httpx.Request("GET", "https://huggingface.co/x")),
        )

    monkeypatch.setattr(refresh, "hf_hub_download", unreadable)
    with pytest.raises(RepositoryNotFoundError):
        refresh.published_manifest("user/dataset")


def test_download_rejects_a_raster_name_it_cannot_bin(tmp_path):
    """worldpop.py finds rasters by file name, so an unexpected name must not be written."""
    catalog = refresh.Catalog("R2025A", {2025: {"npl": "https://example.invalid/surprise.tif"}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"raster-bytes")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with client as http, pytest.raises(ValueError, match="surprise.tif"):
        refresh.download_year(http, catalog, 2025, tmp_path / "rasters")
