from __future__ import annotations

import re
import tomllib
from pathlib import Path

import tomli_w
from pydantic import ValidationError

from ..config import settings
from ..utils.path_utils import derive_project_name, project_name_error
from . import constants as cs
from .models import WorkspaceConfig, WorkspaceRepo


class WorkspaceError(RuntimeError):
    pass


def workspaces_dir(home: Path | None = None) -> Path:
    base = (home or settings.CGR_HOME).expanduser()
    return base / cs.WORKSPACES_SUBDIR


def validate_workspace_name(name: str) -> str:
    if re.fullmatch(cs.WORKSPACE_NAME_PATTERN, name) is None:
        raise WorkspaceError(cs.ERR_WORKSPACE_INVALID_NAME.format(name=name))
    return name


def workspace_path(name: str, home: Path | None = None) -> Path:
    # Every command reaches the file through here, so validating the name once
    # keeps delete/create --force/load from touching a file outside the
    # workspaces directory.
    validate_workspace_name(name)
    return workspaces_dir(home) / f"{name}{cs.WORKSPACE_EXTENSION}"


def list_workspaces(home: Path | None = None) -> list[str]:
    root = workspaces_dir(home)
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob(f"*{cs.WORKSPACE_EXTENSION}"))


def load_workspace(name: str, home: Path | None = None) -> WorkspaceConfig:
    path = workspace_path(name, home)
    if not path.exists():
        raise WorkspaceError(cs.ERR_WORKSPACE_NOT_FOUND.format(name=name, path=path))
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise WorkspaceError(
            cs.ERR_WORKSPACE_INVALID_TOML.format(name=name, error=e)
        ) from e
    body = data.get("workspace", data)
    try:
        return WorkspaceConfig.model_validate(body)
    except ValidationError as e:
        raise WorkspaceError(
            cs.ERR_WORKSPACE_INVALID_SCHEMA.format(name=name, error=e)
        ) from e


def save_workspace(config: WorkspaceConfig, home: Path | None = None) -> Path:
    path = workspace_path(config.name, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"workspace": config.model_dump()}
    with path.open("wb") as f:
        tomli_w.dump(payload, f)
    return path


def create_workspace(
    name: str,
    description: str = "",
    repos: list[WorkspaceRepo] | None = None,
    home: Path | None = None,
    overwrite: bool = False,
) -> tuple[WorkspaceConfig, Path]:
    path = workspace_path(name, home)
    if path.exists() and not overwrite:
        raise WorkspaceError(
            cs.ERR_WORKSPACE_ALREADY_EXISTS.format(name=name, path=path)
        )
    config = WorkspaceConfig(name=name, description=description, repos=repos or [])
    saved = save_workspace(config, home=home)
    return config, saved


def delete_workspace(name: str, home: Path | None = None) -> Path:
    path = workspace_path(name, home)
    if not path.exists():
        raise WorkspaceError(cs.ERR_WORKSPACE_NOT_FOUND.format(name=name, path=path))
    path.unlink()
    return path


def add_repo(
    name: str,
    repo_path: str,
    project_name: str | None = None,
    home: Path | None = None,
) -> tuple[WorkspaceConfig, WorkspaceRepo]:
    resolved = Path(repo_path).expanduser().resolve()
    if not resolved.exists():
        raise WorkspaceError(cs.ERR_WORKSPACE_REPO_PATH_MISSING.format(path=resolved))
    if not resolved.is_dir():
        raise WorkspaceError(
            cs.ERR_WORKSPACE_REPO_NOT_A_DIRECTORY.format(path=resolved)
        )
    if project_name and (error := project_name_error(project_name)) is not None:
        raise WorkspaceError(error)
    config = load_workspace(name, home=home)
    if config.find_repo(str(resolved)) is not None:
        raise WorkspaceError(
            cs.ERR_WORKSPACE_REPO_DUPLICATE.format(path=resolved, name=name)
        )
    for member in config.repos:
        member_path = member.repo_path()
        if resolved.is_relative_to(member_path) or member_path.is_relative_to(resolved):
            raise WorkspaceError(
                cs.ERR_WORKSPACE_REPO_OVERLAPS.format(
                    path=resolved, member=member_path, name=name
                )
            )
    repo = WorkspaceRepo(
        path=str(resolved),
        project_name=(project_name or derive_project_name(resolved)),
        project_named=bool(project_name and project_name.strip()),
    )
    config.repos.append(repo)
    save_workspace(config, home=home)
    return config, repo


def remove_repo(
    name: str, repo_path: str, home: Path | None = None
) -> tuple[WorkspaceConfig, WorkspaceRepo]:
    config = load_workspace(name, home=home)
    found = config.find_repo(repo_path)
    if found is None:
        raise WorkspaceError(
            cs.ERR_WORKSPACE_REPO_NOT_IN_WORKSPACE.format(
                path=Path(repo_path).expanduser().resolve(), name=name
            )
        )
    config.repos = [r for r in config.repos if r is not found]
    save_workspace(config, home=home)
    return config, found
