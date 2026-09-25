"""mkdocs hook: add the generated Credits page to every docs build (#2175).

The page is built from the notices generator's component list at build time,
so it is never committed and cannot fall behind the dependencies.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

CREDITS_SRC = "credits.md"
_GENERATOR = Path(__file__).with_name("generate_credits_page.py")


def _build_page() -> str:
    spec = importlib.util.spec_from_file_location("generate_credits_page", _GENERATOR)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("generate_credits_page", module)
    spec.loader.exec_module(module)
    return module.build_page()


def on_files(files: Any, config: Any) -> Any:
    from mkdocs.structure.files import File

    files.append(File.generated(config, CREDITS_SRC, content=_build_page()))
    return files
