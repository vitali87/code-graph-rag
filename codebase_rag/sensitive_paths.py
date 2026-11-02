# One rule for "might this file hold a credential", shared by every read the
# agent can make without an operator: the read_file tool, confined shell reads
# and MCP read_file. Repository text can ask the agent for a secret file as
# easily as for source, and whatever such a read returns goes to the model
# provider (issue #3457).

import os
import subprocess
from pathlib import Path

from . import constants as cs


def is_sensitive_path(path: Path) -> bool:
    """Whether `path`, or the file a symlink there points at, looks like a
    credential store (see the SENSITIVE_* constants)."""
    if _is_sensitive_name(path):
        return True
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved != path and _is_sensitive_name(resolved)


def _is_sensitive_name(path: Path) -> bool:
    name = path.name.lower()
    parents = [part.lower() for part in path.parts[:-1]]
    # The directory itself too: `rg -e . .ssh` searches everything in it.
    if name in cs.SENSITIVE_DIR_NAMES or any(
        part in cs.SENSITIVE_DIR_NAMES for part in parents
    ):
        return True
    if parents and (parents[-1], name) in cs.SENSITIVE_PARENT_FILES:
        return True
    if name in cs.ENV_TEMPLATE_NAMES:
        return False
    return (
        name in cs.SENSITIVE_FILE_NAMES
        or name.startswith(cs.SENSITIVE_ENV_PREFIX)
        or cs.SENSITIVE_NAME_INFIX in name
        or any(name.endswith(suffix) for suffix in cs.SENSITIVE_FILE_SUFFIXES)
    )


def is_git_internal(path: Path, root: Path) -> bool:
    """Whether `path`, resolved and inside `root`, is a `.git` entry, lies
    inside one, or lies inside a git directory git uses for it.

    Git reads its config and hooks from there and runs the commands they name
    (`core.fsmonitor`, a hook), and cgr runs git after every write to refresh
    the graph, so one write there ran code on the user's machine. No tool has
    a reason to write there. The name match ignores case and the name endings
    Windows drops, which macOS and Windows filesystems resolve to `.git`.
    """
    if any(
        part.lower().rstrip(cs.WINDOWS_IGNORED_NAME_TAIL) == cs.GIT_DIR_NAME
        for part in path.relative_to(root).parts
    ):
        return True
    git_dirs = [*_linked_git_dirs(root), *_git_reported_dirs(path, root)]
    return any(_within(path, git_dir) for git_dir in git_dirs)


def _git_reported_dirs(path: Path, root: Path) -> list[Path]:
    """The git directory, and the shared one, that git itself uses for the
    folder `path` is in.

    Git knows every layout it supports, a nested repository whose git
    directory was moved and named anything included, where the `.git`-file
    rules see only the root's. The search stops at `root`, so a repository
    above the project is not asked about. Git gets none of this process's
    `GIT_*` variables and reads no system or user config, since the
    project's `.env` can set either: a `GIT_DIR` there pointed git elsewhere,
    and a git that fails answers nothing (review of PR 3569). Empty when git
    is missing, too old, or finds no repository: the other rules still hold.
    """
    folder = next(p for p in (path, *path.parents) if p.is_dir())
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(cs.GIT_ENV_PREFIX)
    }
    environment |= {
        cs.GIT_CEILING_DIRECTORIES_ENV: str(root.parent),
        cs.GIT_CONFIG_NOSYSTEM_ENV: cs.GIT_ENV_ON,
        cs.GIT_CONFIG_GLOBAL_ENV: os.devnull,
    }
    try:
        answer = subprocess.run(
            [cs.SHELL_CMD_GIT, *cs.GIT_LOCATE_DIR_ARGS],
            cwd=folder,
            env=environment,
            capture_output=True,
            encoding=cs.ENCODING_UTF8,
            errors="replace",
            check=False,
            timeout=cs.GIT_LOCATE_DIR_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if answer.returncode != 0:
        return []
    return [Path(line) for line in answer.stdout.splitlines() if line]


def _linked_git_dirs(root: Path) -> list[Path]:
    """The git directories a `.git` file at `root` points git to, read
    without git, so they hold where git cannot be asked.

    `git init --separate-git-dir=meta` and `git worktree add` leave a file
    there, `gitdir: <path>`, and git keeps the config and hooks in that
    directory, or in the one its `commondir` names, under any name.
    """
    try:
        text = (root / cs.GIT_DIR_NAME).read_text(encoding=cs.ENCODING_UTF8)
    except (OSError, UnicodeDecodeError):
        return []
    if not text.startswith(cs.LANG_GITFILE_PREFIX):
        return []
    git_dir = root / text.removeprefix(cs.LANG_GITFILE_PREFIX).strip()
    found = [git_dir.resolve()]
    try:
        common = (git_dir / cs.GIT_COMMONDIR_FILENAME).read_text(
            encoding=cs.ENCODING_UTF8
        )
    except (OSError, UnicodeDecodeError):
        return found
    found.append((git_dir / common.strip()).resolve())
    return found


def _within(path: Path, directory: Path) -> bool:
    # By file identity, not spelling: on a case-insensitive filesystem
    # `META/config` opens `meta/config`, and a path that does not exist yet
    # is judged by the nearest directory above it that does.
    for candidate in (path, *path.parents):
        try:
            if candidate.samefile(directory):
                return True
        except OSError:
            continue
    return False
