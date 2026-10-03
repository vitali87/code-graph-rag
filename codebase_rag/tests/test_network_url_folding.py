"""Client URLs built from constants and concatenation link (issue #2521).

`requests.get(f"{BASE}/users/{uid}")` over a module-level `BASE` recorded
`{BASE}/users/{uid}`, and `requests.post(BASE + "/users")` or
`fetch("/orders/" + id)` recorded `<dynamic>`: both are statically
resolvable, yet neither could link to the endpoint it calls. A literal
prefix plus an expression now renders like the template-literal form does
(`/orders/{id}`), and a module-level string constant folds into the URL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.endpoints import url_matches_template
from codebase_rag.parsers.io_access.constants import DYNAMIC_TARGET

_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
_ACCESS_RELS = {
    cs.RelationshipType.READS_FROM.value,
    cs.RelationshipType.WRITES_TO.value,
}


def _accesses(
    tmp_path: Path, files: dict[str, str], language: str, kind: str = "NETWORK"
) -> dict[str, set[tuple[str, str]]]:
    # caller leaf name -> {(relationship, resource identity)} for one kind
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    marker = f"resource::{kind}::"
    out: dict[str, set[tuple[str, str]]] = {}
    for call in mock.ensure_relationship_batch.call_args_list:
        rel = str(call.args[1])
        target_qn = call.args[2][2]
        if rel not in _ACCESS_RELS or not target_qn.startswith(marker):
            continue
        caller = call.args[0][2].rsplit(".", 1)[-1]
        out.setdefault(caller, set()).add((rel, target_qn.removeprefix(marker)))
    return out


def _urls(accesses: dict[str, set[tuple[str, str]]], caller: str) -> set[str]:
    return {url for _rel, url in accesses.get(caller, set())}


_ISSUE_CLIENT = (
    "import requests\n\n"
    'BASE = "http://localhost:5000"\n\n\n'
    "def fetch_user(uid):\n"
    '    return requests.get(f"{BASE}/users/{uid}").json()\n\n\n'
    "def make_user():\n"
    '    return requests.post(BASE + "/users", json={})\n'
)

_ISSUE_WEB = (
    "export async function loadOrder(id) {\n"
    "  const r = await fetch(`/orders/${id}`);\n"
    "  return r.json();\n"
    "}\n"
    "export function removeOrder(id) {\n"
    '  return fetch("/orders/" + id, { method: "DELETE" });\n'
    "}\n"
)


class TestPythonUrls:
    def test_fstring_over_a_module_constant_folds(self, tmp_path: Path) -> None:
        accesses = _accesses(tmp_path, {"client/client.py": _ISSUE_CLIENT}, "python")
        assert _urls(accesses, "fetch_user") == {"http://localhost:5000/users/{uid}"}

    def test_constant_plus_literal_folds(self, tmp_path: Path) -> None:
        accesses = _accesses(tmp_path, {"client/client.py": _ISSUE_CLIENT}, "python")
        assert accesses["make_user"] == {
            (cs.RelationshipType.WRITES_TO.value, "http://localhost:5000/users")
        }

    def test_literal_plus_expression_renders_a_placeholder(
        self, tmp_path: Path
    ) -> None:
        source = (
            "import requests\n\n\n"
            "def get_order(order_id):\n"
            '    return requests.get("/orders/" + str(order_id) + "/items")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "get_order") == {"/orders/{str(order_id)}/items"}

    def test_keyword_url_folds(self, tmp_path: Path) -> None:
        source = (
            "import requests\n\n"
            'BASE = "http://orders:8000"\n\n\n'
            "def ping():\n"
            '    return requests.get(url=BASE + "/health")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "ping") == {"http://orders:8000/health"}

    def test_a_constant_built_from_a_constant_folds(self, tmp_path: Path) -> None:
        source = (
            "import requests\n\n"
            'HOST = "http://localhost:5000"\n'
            'API = HOST + "/api/v1"\n\n\n'
            "def list_users():\n"
            '    return requests.get(f"{API}/users")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "list_users") == {"http://localhost:5000/api/v1/users"}


class TestJavaScriptUrls:
    def test_literal_plus_expression_renders_like_the_template(
        self, tmp_path: Path
    ) -> None:
        accesses = _accesses(tmp_path, {"node/web.js": _ISSUE_WEB}, "javascript")
        assert _urls(accesses, "removeOrder") == {"/orders/{id}"}
        assert _urls(accesses, "loadOrder") == {"/orders/{id}"}

    def test_template_over_a_module_constant_folds(self, tmp_path: Path) -> None:
        source = (
            'const API = "http://orders:3000";\n'
            "export function loadOrder(id) {\n"
            "  return fetch(`${API}/orders/${id}`);\n"
            "}\n"
            "export function cancel(id) {\n"
            '  return fetch(API + "/orders/" + id + "/cancel", { method: "POST" });\n'
            "}\n"
        )
        accesses = _accesses(tmp_path, {"web.js": source}, "javascript")
        assert _urls(accesses, "loadOrder") == {"http://orders:3000/orders/{id}"}
        assert _urls(accesses, "cancel") == {"http://orders:3000/orders/{id}/cancel"}

    def test_exported_typescript_constant_folds(self, tmp_path: Path) -> None:
        source = (
            'export const API: string = "http://orders:3000";\n'
            "export async function list(): Promise<Response> {\n"
            '  return fetch(API + "/orders");\n'
            "}\n"
        )
        accesses = _accesses(tmp_path, {"web.ts": source}, "typescript")
        assert _urls(accesses, "list") == {"http://orders:3000/orders"}


class TestWhatIsNotGuessed:
    def test_a_parameter_prefix_stays_unresolved(self, tmp_path: Path) -> None:
        source = (
            "import requests\n\n\n"
            "def get_users(base):\n"
            '    return requests.get(base + "/users")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        urls = _urls(accesses, "get_users")
        assert urls == {"{base}/users"}
        # Not rooted and not absolute: no endpoint template can claim it.
        assert not any(url_matches_template(url, "/users") for url in urls)

    def test_a_shadowed_constant_does_not_fold(self, tmp_path: Path) -> None:
        source = (
            "import requests\n\n"
            'BASE = "http://localhost:5000"\n\n\n'
            "def get_users(BASE):\n"
            '    return requests.get(BASE + "/users")\n\n\n'
            "def get_orders():\n"
            '    BASE = "http://orders:9000"\n'
            '    return requests.get(f"{BASE}/orders")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "get_users") == {"{BASE}/users"}
        assert _urls(accesses, "get_orders") == {"{BASE}/orders"}

    def test_a_rebound_module_name_does_not_fold(self, tmp_path: Path) -> None:
        source = (
            "import requests\n\n"
            'BASE = "http://localhost:5000"\n'
            'BASE = "http://localhost:6000"\n\n\n'
            "def get_users():\n"
            '    return requests.get(BASE + "/users")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "get_users") == {"{BASE}/users"}

    @pytest.mark.parametrize(
        "client",
        [
            pytest.param(
                "import requests\n"
                "from settings import *\n\n\n"
                "def get_users():\n"
                '    return requests.get(BASE + "/users")\n\n\n'
                "get_users()\n"
                'BASE = "/other"\n',
                id="assigned-after-the-star-import",
            ),
            pytest.param(
                "import requests\n\n"
                'BASE = "/other"\n'
                "try:\n"
                "    from settings import *\n"
                "except ImportError:\n"
                "    pass\n\n\n"
                "def get_users():\n"
                '    return requests.get(BASE + "/users")\n',
                id="star-import-inside-a-try",
            ),
        ],
    )
    def test_a_name_a_star_import_may_bind_does_not_fold(
        self, tmp_path: Path, client: str
    ) -> None:
        # Bot review on PR #2596: `settings` supplies BASE, and `get_users()`
        # runs before the module reassigns it, so Python requests
        # /imported/users. Which binding a call sees is not knowable here,
        # so the URL stays unresolved rather than folding `/other`.
        files = {"settings.py": 'BASE = "/imported"\n', "client.py": client}
        accesses = _accesses(tmp_path, files, "python")
        assert _urls(accesses, "get_users") == {"{BASE}/users"}

    def test_a_named_import_beside_the_constant_still_folds(
        self, tmp_path: Path
    ) -> None:
        source = (
            "import requests\n"
            "from settings import TIMEOUT\n\n"
            'BASE = "http://localhost:5000"\n\n\n'
            "def get_users():\n"
            '    return requests.get(BASE + "/users", timeout=TIMEOUT)\n'
        )
        files = {"settings.py": "TIMEOUT = 5\n", "client.py": source}
        accesses = _accesses(tmp_path, files, "python")
        assert _urls(accesses, "get_users") == {"http://localhost:5000/users"}

    def test_a_converted_substitution_does_not_fold(self, tmp_path: Path) -> None:
        # `!r` and `=` change the rendered text, so the value is not the URL.
        source = (
            "import requests\n\n"
            'BASE = "http://localhost:5000"\n\n\n'
            "def debug():\n"
            '    return requests.get(f"{BASE!r}/users")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "debug") == {"{BASE!r}/users"}

    def test_a_let_binding_does_not_fold(self, tmp_path: Path) -> None:
        source = (
            'let API = "http://orders:3000";\n'
            "export function list() {\n"
            '  return fetch(API + "/orders");\n'
            "}\n"
        )
        accesses = _accesses(tmp_path, {"web.js": source}, "javascript")
        assert _urls(accesses, "list") == {"{API}/orders"}

    def test_a_js_parameter_shadowing_the_constant_does_not_fold(
        self, tmp_path: Path
    ) -> None:
        source = (
            'const API = "http://orders:3000";\n'
            "export function list(API) {\n"
            "  return fetch(`${API}/orders`);\n"
            "}\n"
        )
        accesses = _accesses(tmp_path, {"web.js": source}, "javascript")
        assert _urls(accesses, "list") == {"{API}/orders"}

    def test_a_concatenation_without_literal_text_stays_dynamic(
        self, tmp_path: Path
    ) -> None:
        source = (
            "import requests\n\n\n"
            "def get(base, path):\n"
            "    return requests.get(base + path)\n"
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "get") == {DYNAMIC_TARGET}

    def test_file_paths_are_not_folded(self, tmp_path: Path) -> None:
        # Only request URLs are folded: they are what endpoint linking reads.
        source = (
            'DATA = "/var/data"\n\n\n'
            "def load():\n"
            '    with open(DATA + "/seed.json") as fh:\n'
            "        return fh.read()\n"
        )
        accesses = _accesses(tmp_path, {"loader.py": source}, "python", kind="FILE")
        assert _urls(accesses, "load") == {DYNAMIC_TARGET}

    def test_a_plain_literal_is_unchanged(self, tmp_path: Path) -> None:
        source = (
            "import requests\n\n\n"
            "def health():\n"
            '    return requests.get("http://svc:8000/health")\n'
        )
        accesses = _accesses(tmp_path, {"client.py": source}, "python")
        assert _urls(accesses, "health") == {"http://svc:8000/health"}
