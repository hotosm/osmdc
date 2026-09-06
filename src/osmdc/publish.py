"""Publish the merged H3 completeness tiles to a Hugging Face dataset repository."""

from pathlib import Path

from huggingface_hub import HfApi


def publish_tiles(
    tiles_dir: Path,
    repo_id: str,
    token: str | None = None,
    path_in_repo: str | None = None,
    delete_patterns: list[str] | None = None,
) -> str:
    """Upload a tile directory to a HF dataset repo and return the dataset URL.

    delete_patterns drops remote files under path_in_repo that this upload does not
    replace, so a rebuild leaves no tile from the previous release behind.
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
        delete_patterns=delete_patterns,
        commit_message=f"Update {path_in_repo or 'completeness'} tiles",
    )
    return f"https://huggingface.co/datasets/{repo_id}"
