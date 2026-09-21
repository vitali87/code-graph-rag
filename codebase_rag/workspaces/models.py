from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class WorkspaceRepo(BaseModel):
    path: str
    project_name: str
    # Older workspace files have no provenance field. Keep their historical
    # named behavior while new entries record the user's actual choice.
    project_named: bool = True

    def repo_path(self) -> Path:
        return Path(self.path).expanduser().resolve()


class WorkspaceConfig(BaseModel):
    name: str
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
