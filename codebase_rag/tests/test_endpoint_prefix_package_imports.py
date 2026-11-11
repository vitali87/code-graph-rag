"""A mount in a package's `__init__.py` keeps its prefix (issue #3192).

`from . import blog` inside `flaskr/__init__.py` (the Flask tutorial's
`create_app` layout) resolved one level too high: the module of an
`__init__.py` IS the package, so a leading dot names the package itself,
not its parent. The blueprint never resolved, its `url_prefix` was dropped,
and two blueprints' `/` merged into one `GET /`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

PROJECT = "flaskpkg"
_BLUEPRINT = """\
from flask import Blueprint

bp = Blueprint("{name}", __name__)


@bp.route("/")
def index():
    return "{name}"
"""
_ROUTER = """\
from fastapi import APIRouter

router = APIRouter()


@router.get("/items")
def items():
    return []
"""
_FLASK_INIT = """\
from flask import Flask


def create_app():
    app = Flask(__name__)
    from . import blog
    app.register_blueprint(blog.bp, url_prefix="/blog")
    from . import api
    app.register_blueprint(api.bp, url_prefix="/api")
    return app
"""
_FASTAPI_INIT = """\
from fastapi import FastAPI

from .shop import router

app = FastAPI()
app.include_router(router, prefix="/shop")
"""
_FACTORY = """\
from flask import Flask

from . import blog


def create_app():
    app = Flask(__name__)
    app.register_blueprint(blog.bp, url_prefix="/blog")
    return app
"""
_ABSOLUTE_INIT = """\
from flask import Flask

from flaskr import blog


def create_app():
    app = Flask(__name__)
    app.register_blueprint(blog.bp, url_prefix="/blog")
    return app
"""


def _endpoints(root: Path, files: dict[str, str]) -> dict[str, set[str]]:
    # Endpoint identity -> the handlers that expose it.
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    mock = MagicMock()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()
    out: dict[str, set[str]] = {}
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) == cs.RelationshipType.EXPOSES.value:
            identity = str(c.args[2][2]).rsplit("::", 1)[-1]
            out.setdefault(identity, set()).add(
                str(c.args[0][2]).removeprefix(f"{PROJECT}.")
            )
    return out


def _blueprints() -> dict[str, str]:
    return {
        f"flaskr/{name}.py": _BLUEPRINT.format(name=name) for name in ("blog", "api")
    }


def test_flask_blueprints_mounted_from_a_package_init_keep_their_prefix(
    tmp_path: Path,
) -> None:
    files = {**_blueprints(), "flaskr/__init__.py": _FLASK_INIT}
    assert _endpoints(tmp_path / PROJECT, files) == {
        "GET /blog/": {"flaskr.blog.index"},
        "GET /api/": {"flaskr.api.index"},
    }


def test_a_fastapi_router_included_from_a_package_init_keeps_its_prefix(
    tmp_path: Path,
) -> None:
    files = {"svc/shop.py": _ROUTER, "svc/__init__.py": _FASTAPI_INIT}
    assert _endpoints(tmp_path / PROJECT, files) == {
        "GET /shop/items": {"svc.shop.items"}
    }


@pytest.mark.parametrize(
    "mounting",
    [
        {"flaskr/__init__.py": "", "flaskr/factory.py": _FACTORY},
        {"flaskr/__init__.py": _ABSOLUTE_INIT},
    ],
    ids=["relative-import-in-a-plain-module", "absolute-import-in-init"],
)
def test_the_layouts_that_already_worked_still_do(
    tmp_path: Path, mounting: dict[str, str]
) -> None:
    # Negatives: a `from . import` in a plain module names its own package
    # (one level up), and an absolute import resolves by suffix.
    files = {"flaskr/blog.py": _BLUEPRINT.format(name="blog"), **mounting}
    assert _endpoints(tmp_path / PROJECT, files) == {
        "GET /blog/": {"flaskr.blog.index"}
    }


def test_a_prefix_edited_in_the_package_init_re_mounts_incrementally(
    tmp_path: Path,
) -> None:
    # The scoped re-ingest of `__init__.py` alone must find the blueprint
    # module through the same package-relative import, or the unchanged
    # handler re-emits without any prefix.
    from evals.cgr_graph import _StatefulIngestor

    root = tmp_path / PROJECT
    files = {**_blueprints(), "flaskr/__init__.py": _FLASK_INIT}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        # The routes alone: the emulator does not answer endpoint linking.
        capture=resolve_capture([cs.RelationshipType.EXPOSES.value]),
    )
    updater.run(force=True)
    (root / "flaskr/__init__.py").write_text(
        _FLASK_INIT.replace('"/blog"', '"/journal"'), encoding="utf-8"
    )
    updater.reingest(["flaskr/__init__.py"], deleted=[])

    # The emulator does not model the delete of a handler's previous EXPOSES
    # (Memgraph runs it), so the old `/blog/` edge can linger here; what this
    # pins is that the unchanged handler re-mounts under the edited prefix.
    exposed = {
        (str(edge[4]).rsplit("::", 1)[-1], str(edge[1]).removeprefix(f"{PROJECT}."))
        for edge in store.edges
        if edge[2] == cs.RelationshipType.EXPOSES.value
    }
    assert ("GET /journal/", "flaskr.blog.index") in exposed, exposed
    assert ("GET /", "flaskr.blog.index") not in exposed, exposed
