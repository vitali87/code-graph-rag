from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class WorkspaceRepo(BaseModel):
    path: str
    project_name: str

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
