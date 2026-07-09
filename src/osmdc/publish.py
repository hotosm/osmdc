"""Publish the merged H3 completeness tiles to a Hugging Face dataset repository."""

from __future__ import annotations

from pathlib import Path

from huggingface_hub import HfApi


def publish_tiles(
    tiles_dir: Path,
    repo_id: str,
    token: str | None = None,
    path_in_repo: str | None = None,
) -> str:
    """Upload a tile directory (parquet shards + manifest) to a HF dataset repo.

    path_in_repo places the tiles in a subfolder of the same repo (e.g. worldpop_ts).
    token defaults to the cached login or HF_TOKEN environment variable.
    Returns the dataset URL.
    """
    if not (tiles_dir / "manifest.json").exists():
        raise FileNotFoundError(f"no manifest.json in {tiles_dir}; run the aggregation first")
    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
    api.upload_folder(
        folder_path=str(tiles_dir),
        path_in_repo=path_in_repo,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"Update {path_in_repo or 'completeness'} tiles",
    )
    return f"https://huggingface.co/datasets/{repo_id}"
