from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from .constants import WORKSPACE_NAME_PATTERN


class WorkspaceRepo(BaseModel):
    path: str
    project_name: str
    # Older workspace files have no provenance field. Keep their historical
    # named behavior while new entries record the user's actual choice.
    project_named: bool = True

    def repo_path(self) -> Path:
        return Path(self.path).expanduser().resolve()


@dataclass(frozen=True, slots=True)
class WorkspaceContents:
    """What a new workspace starts with: its description and member repos."""

    description: str = ""
    repos: Sequence[WorkspaceRepo] = ()


class WorkspaceConfig(BaseModel):
    # save_workspace writes to the path this name selects, so a hand-edited
    # file must not smuggle a traversing name back in on the next save.
    name: str = Field(pattern=WORKSPACE_NAME_PATTERN)
    description: str = ""
    repos: list[WorkspaceRepo] = Field(default_factory=list)

    def project_names(self) -> list[str]:
        return [r.project_name for r in self.repos]

    def find_repo(self, path: str) -> WorkspaceRepo | None:
        target = Path(path).expanduser().resolve()
        for repo in self.repos:
            if repo.repo_path() == target:
                return repo
        return None
