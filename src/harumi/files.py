"""Upload local files to a project's S3-backed workspace via harumi-api.

Mirrors PUT /projects/{project_id}/files (multipart: file, mime_type, path)
— see harumi-api/src/api/projects/router.py:create_project_file and
harumi-api/src/api/projects/services/files.py:ProjectFileService.upload.
Files are later downloaded into the sandbox at execution time by
harumi-api's worker (src/workers/tasks/notebook_execution.py).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from harumi.models import ProjectFile

if TYPE_CHECKING:
    from harumi.client import ApiClient

# Directories/files that should never be pushed to the sandbox workspace.
_IGNORED_NAMES = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
    "node_modules",
}


def _iter_upload_files(local_path: Path) -> Iterable[Path]:
    if local_path.is_file():
        yield local_path
        return

    for path in sorted(local_path.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_NAMES for part in path.relative_to(local_path).parts):
            continue
        yield path


def upload_path(api: "ApiClient", project_id: str, local_path: Path) -> list[ProjectFile]:
    """Upload a single file or an entire directory tree to a project.

    Relative subdirectories are preserved via the `path` form field so a
    whole local project mirrors into `{project_id}/<relative dir>/<file>`.
    """
    local_path = Path(local_path)
    if not local_path.exists():
        raise FileNotFoundError(f"No such file or directory: {local_path}")

    uploaded: list[ProjectFile] = []
    is_dir = local_path.is_dir()

    for file_path in _iter_upload_files(local_path):
        rel_dir = ""
        if is_dir:
            rel_dir = str(file_path.relative_to(local_path).parent)
            if rel_dir == ".":
                rel_dir = ""

        uploaded.append(_upload_one(api, project_id, file_path, rel_dir))

    return uploaded


def _upload_one(api: "ApiClient", project_id: str, file_path: Path, rel_dir: str) -> ProjectFile:
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    content = file_path.read_bytes()

    data: dict[str, str] = {"mime_type": mime_type}
    if rel_dir:
        data["path"] = rel_dir

    response = api.request(
        "PUT",
        f"/projects/{project_id}/files",
        data=data,
        files={"file": (file_path.name, content, mime_type)},
        timeout=120.0,
    )
    return ProjectFile.model_validate(response.json())
