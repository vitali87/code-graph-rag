import tomllib
from pathlib import Path

from . import constants as cs


def source_checkout_root(package_dir: Path) -> Path | None:
    """The code-graph-rag checkout `package_dir` sits in, or None if installed.

    Found from the running package, never from the cwd: an installed cgr run
    inside someone's project must not take that project for the checkout
    (issue #2422), nor load grammars a repo it indexes has planted. The
    project name rules out a repo that merely vendors the package.
    """
    root = package_dir.parent
    if not (root / cs.LANG_CONFIG_FILE).is_file():
        return None
    if not (root / cs.LANG_GIT_DIR).exists():
        return None
    try:
        with open(root / cs.PYPROJECT_PATH, "rb") as f:
            project = tomllib.load(f).get(cs.TOML_KEY_PROJECT)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(project, dict):
        return None
    if project.get(cs.LANG_TOML_KEY_NAME) != cs.PACKAGE_NAME:
        return None
    return root
