import os
import re
import subprocess
from pathlib import Path

from loguru import logger
from packaging.specifiers import InvalidSpecifier, Specifier
from packaging.version import Version

from . import constants as vc
from . import logs as ls

_REQUIREMENT_HEAD = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$"
)


class WorkspaceError(RuntimeError):
    pass


def work_dir() -> Path:
    root = Path(os.environ.get(vc.WORK_DIR_ENV, vc.DEFAULT_WORK_DIR))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _env() -> dict[str, str]:
    # A VIRTUAL_ENV inherited from the parent would make `uv sync` in a
    # worktree target the parent's venv instead of the worktree's own.
    return {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def run(
    cmd: list[str], cwd: Path, timeout: float = vc.SYNC_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=_env(),
        capture_output=True,
        text=True,
        encoding=vc.ENCODING,
        timeout=timeout,
        check=False,
    )


def _must(cmd: list[str], cwd: Path, timeout: float = vc.SYNC_TIMEOUT_S) -> str:
    done = run(cmd, cwd, timeout)
    if done.returncode != 0:
        raise WorkspaceError(tail(done.stderr or done.stdout))
    return done.stdout


def tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-").lower()


def corpus_checkout(corpus: vc.Corpus) -> Path:
    """A clean checkout of `corpus` at its pinned commit.

    A dirty or moved checkout is reset rather than trusted: a stray file
    would be indexed into both sides of every comparison and still change
    the digest a golden was recorded against.
    """
    path = work_dir() / "corpora" / corpus.name
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _must(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                "--branch",
                corpus.ref,
                corpus.url,
                str(path),
            ],
            path.parent,
        )
    head = _must(["git", "rev-parse", "HEAD"], path).strip()
    if head != corpus.commit:
        raise WorkspaceError(
            ls.CORPUS_MOVED.format(name=corpus.name, head=head, commit=corpus.commit)
        )
    if _must(["git", "status", "--porcelain", "--ignored"], path).strip():
        _must(["git", "checkout", "-q", "--force", corpus.commit], path)
        _must(["git", "clean", "-qdffx"], path)
    return path


def add_worktree(repo: Path, name: str, base_ref: str) -> tuple[Path, str]:
    """A fresh worktree on branch vigil/<name>, cut from `base_ref`."""
    branch = f"{vc.BRANCH_PREFIX}{name}"
    path = work_dir() / "wt" / name
    if path.exists():
        remove_worktree(repo, path, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    _must(
        ["git", "worktree", "add", "-q", "--force", "-B", branch, str(path), base_ref],
        repo,
    )
    return path, branch


def remove_worktree(repo: Path, path: Path, branch: str | None = None) -> None:
    run(["git", "worktree", "remove", "--force", str(path)], repo)
    if branch is not None:
        run(["git", "branch", "-D", branch], repo)


def sync(worktree: Path, extras: tuple[str, ...], groups: tuple[str, ...] = ()) -> None:
    cmd = ["uv", "sync", "--frozen"]
    for extra in extras:
        cmd += ["--extra", extra]
    for group in groups:
        cmd += ["--group", group]
    logger.info(ls.SYNCING.format(path=worktree.name, extras=", ".join(extras) or "-"))
    _must(cmd, worktree)


class LockConflict(WorkspaceError):
    pass


def lock_upgrade(worktree: Path, package: str, version: str) -> None:
    done = run(["uv", "lock", "--upgrade-package", f"{package}=={version}"], worktree)
    if done.returncode != 0:
        raise LockConflict(conflict_summary(done.stderr or done.stdout))


def conflict_summary(text: str, limit: int = 320) -> str:
    """uv's derivation, from its first "Because", on one line.

    The first clause names the packages that pin the conflict, which is the
    part worth reading in a report; the tail of uv's message is generic
    advice about requires-python.
    """
    flat = " ".join(text.replace("│", " ").replace("╰─▶", " ").split())
    start = flat.find("Because")
    summary = flat[start:] if start >= 0 else flat
    return summary if len(summary) <= limit else summary[: limit - 1] + "…"


def install_direct(worktree: Path, package: str, version: str) -> None:
    """Put `version` in the worktree's venv over its synced lock, the way a
    fresh `pip install code-graph-rag` resolves it when the lock cannot."""
    python = worktree / ".venv" / "bin" / "python"
    _must(
        ["uv", "pip", "install", "--python", str(python), f"{package}=={version}"],
        worktree,
    )


def widen(written: str, latest: str) -> str:
    """Rewrite a requirement so it admits `latest`, changing nothing else.

    Each clause that already admits `latest` is kept as written. An upper
    bound that excludes it moves to the next major, keeping the cap a
    deliberate `<2` was there to keep; a pin moves to `latest`; an
    exclusion of exactly `latest` is dropped. The clause order, extras and
    marker stay as they were, so the diff is one clause.
    """
    head, sep, marker = written.partition(";")
    match = _REQUIREMENT_HEAD.match(head)
    if match is None:
        raise WorkspaceError(ls.BAD_REQUIREMENT.format(req=written))
    name, extras, spec_text = match.group(1), match.group(2) or "", match.group(3)
    target = Version(latest)
    clauses: list[str] = []
    for raw in (c.strip() for c in spec_text.split(",")):
        if not raw:
            continue
        try:
            spec = Specifier(raw)
        except InvalidSpecifier as exc:
            raise WorkspaceError(ls.BAD_REQUIREMENT.format(req=written)) from exc
        if spec.contains(target, prereleases=True):
            clauses.append(raw)
        elif spec.operator in ("<", "<="):
            clauses.append(f"<{target.major + 1}")
        elif spec.operator in ("==", "==="):
            clauses.append(f"=={latest}")
        elif spec.operator == "~=":
            clauses.append(f">={latest}")
        elif spec.operator == "!=":
            continue
        else:
            raise WorkspaceError(ls.CANNOT_WIDEN.format(req=written, latest=latest))
    widened = f"{name}{extras}{','.join(clauses)}"
    return f"{widened};{marker}" if sep else widened


def rewrite_requirement(worktree: Path, written: str, latest: str) -> str:
    pyproject = worktree / vc.PYPROJECT
    text = pyproject.read_text(encoding=vc.ENCODING)
    quoted = f'"{written}"'
    if text.count(quoted) != 1:
        raise WorkspaceError(ls.REQUIREMENT_NOT_FOUND.format(req=written))
    widened = widen(written, latest)
    pyproject.write_text(text.replace(quoted, f'"{widened}"'), encoding=vc.ENCODING)
    return widened


def changed_paths(worktree: Path, base_ref: str) -> list[str]:
    tracked = _must(["git", "diff", "--name-only", base_ref], worktree).splitlines()
    untracked = _must(
        ["git", "ls-files", "--others", "--exclude-standard"], worktree
    ).splitlines()
    return sorted({p for p in [*tracked, *untracked] if p})


def commit_all(worktree: Path, message: str) -> str:
    _must(["git", "add", "-A"], worktree)
    _must(["git", "commit", "-q", "-m", message], worktree)
    return _must(["git", "rev-parse", "--short", "HEAD"], worktree).strip()
