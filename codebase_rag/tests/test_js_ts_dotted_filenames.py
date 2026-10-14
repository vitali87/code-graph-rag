"""TS/JS files whose name carries a dot (issue #2565).

`url.test.ts`, `users.service.ts` and `vite.config.ts` keep the dot in their
module qn (`proj.src.url.test`), the same way a dotted C# stem
(`Foo.TResult.cs`) does. The qn is not the problem; reading a DIRECTORY or a
module/export boundary back out of it by splitting at dots is. An importer's
directory has to come from its file path, and a dotted target has to be found
in the registry of real modules, or a co-located test and every NestJS/Angular
file lose their IMPORTS edges and their calls fall back to the name-only
heuristic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
    get_relationships,
)

URL_TS = "export function getPath(u: string): string { return u }\n"
URL_CONSUMER = (
    "import { getPath } from './url'\n"
    "export function t1(): string { return getPath('a') }\n"
)

USERS_SERVICE = (
    "export class UsersService { findAll(): string[] { return [] } }\n"
    "export function helper(): number { return 1 }\n"
)
USERS_CONTROLLER = (
    "import { UsersService, helper } from './users.service'\n"
    "export class UsersController {\n"
    "  constructor(private readonly users: UsersService) {}\n"
    "  list(): string[] { helper(); return this.users.findAll() }\n"
    "}\n"
)
USERS_MODULE = (
    "import { UsersController } from './users.controller'\n"
    "import { UsersService } from './users.service'\n"
    "export const providers = [UsersController, UsersService]\n"
)
NEST_MAIN = (
    "import { providers } from './users/users.module'\n"
    "import { UsersService } from './users/users.service'\n"
    "export function boot(): number {\n"
    "  new UsersService().findAll()\n"
    "  return providers.length\n"
    "}\n"
)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _index(root: Path, files: dict[str, str]) -> tuple[MagicMock, MagicMock]:
    _write(root, files)
    ingestor = MagicMock()
    updater = create_and_run_updater(root, ingestor, skip_if_missing="typescript")
    return ingestor, updater


def _tail(qn: str) -> str:
    # The project segment carries a path digest; tests compare what follows.
    return qn.split(cs.SEPARATOR_DOT, 1)[1]


def _imports(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (_tail(c.args[0][2]), _tail(c.args[2][2]))
        for c in get_relationships(ingestor, cs.RelationshipType.IMPORTS)
        if c.args[2][0] == cs.NodeLabel.MODULE
    }


def _external_imports(ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (_tail(c.args[0][2]), c.args[2][2])
        for c in get_relationships(ingestor, cs.RelationshipType.IMPORTS)
        if c.args[2][0] == cs.NodeLabel.EXTERNAL_MODULE
    }


def _calls(ingestor: MagicMock) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for c in get_relationships(ingestor, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or {}
        out[(_tail(c.args[0][2]), _tail(c.args[2][2]))] = props.get(
            cs.KEY_RESOLUTION, ""
        )
    return out


def _modules(ingestor: MagicMock) -> set[str]:
    return {_tail(qn) for qn in get_node_names(ingestor, cs.NodeLabel.MODULE)}


class TestDottedImporter:
    """A dotted file importing a plain sibling (co-located tests)."""

    def test_colocated_tests_import_their_subject(self, temp_repo: Path) -> None:
        ingestor, _ = _index(
            temp_repo / "tsdot",
            {
                "src/url.ts": URL_TS,
                "src/url.test.ts": URL_CONSUMER,
                "src/url.spec.ts": URL_CONSUMER,
                "src/other.test.ts": URL_CONSUMER,
                "src/consumer.ts": URL_CONSUMER,
            },
        )

        imports = _imports(ingestor)
        calls = _calls(ingestor)
        for importer in ("url.test", "url.spec", "other.test", "consumer"):
            assert (f"src.{importer}", "src.url") in imports, imports
            assert calls.get((f"src.{importer}.t1", "src.url.getPath")) == (
                cs.EdgeResolution.EXACT
            ), calls

    def test_dotted_importer_resolves_parent_and_index(self, temp_repo: Path) -> None:
        # `'.'` and `'..'` name a directory's index; the directory is the
        # file's, not `src.utils.url` (the qn minus one dotted segment). A
        # plain importer beside it is the control: the dotted one must end
        # up with the same edges.
        body = (
            "import { fromIndex } from '.'\n"
            "import { fromLib } from '../lib'\n"
            "export function t1(): number { return fromIndex() + fromLib() }\n"
        )
        ingestor, _ = _index(
            temp_repo / "tsidx",
            {
                "src/utils/index.ts": (
                    "export function fromIndex(): number { return 1 }\n"
                ),
                "src/utils/url.ts": URL_TS,
                "src/lib/index.ts": "export function fromLib(): number { return 2 }\n",
                "src/utils/url.test.ts": body,
                "src/utils/plain.ts": body,
            },
        )

        imports = _imports(ingestor)
        calls = _calls(ingestor)
        for importer in ("src.utils.url.test", "src.utils.plain"):
            assert (importer, "src.utils.index") in imports, imports
            assert (importer, "src.lib.index") in imports, imports
            assert (importer, "src.utils.url") not in imports, imports
        for callee in ("src.utils.index.fromIndex", "src.lib.index.fromLib"):
            dotted = calls.get(("src.utils.url.test.t1", callee))
            assert dotted is not None, calls
            assert dotted == calls.get(("src.utils.plain.t1", callee)), calls

    def test_dotted_importer_leaves_no_unresolved_specifier(
        self, temp_repo: Path
    ) -> None:
        # `./url` from `url.test.ts` names a real file; recording it as
        # unresolved would re-parse the test every time any file is created.
        _, updater = _index(
            temp_repo / "tsunres",
            {"src/url.ts": URL_TS, "src/url.test.ts": URL_CONSUMER},
        )

        assert not updater.factory.import_processor.unresolved_specifiers


class TestDottedTarget:
    """A plain or dotted file importing a dotted file (NestJS/Angular)."""

    def test_nest_layout_imports_and_calls(self, temp_repo: Path) -> None:
        ingestor, _ = _index(
            temp_repo / "tsnest",
            {
                "src/users/users.service.ts": USERS_SERVICE,
                "src/users/users.controller.ts": USERS_CONTROLLER,
                "src/users/users.module.ts": USERS_MODULE,
                "src/main.ts": NEST_MAIN,
            },
        )

        assert {
            ("src.users.users.controller", "src.users.users.service"),
            ("src.users.users.module", "src.users.users.controller"),
            ("src.users.users.module", "src.users.users.service"),
            ("src.main", "src.users.users.module"),
            ("src.main", "src.users.users.service"),
        } <= _imports(ingestor)

        calls = _calls(ingestor)
        service = "src.users.users.service"
        list_qn = "src.users.users.controller.UsersController.list"
        assert calls.get((list_qn, f"{service}.helper")) == cs.EdgeResolution.EXACT, (
            calls
        )
        assert (list_qn, f"{service}.UsersService.findAll") in calls, calls

    def test_plain_importer_leaves_no_unresolved_specifier(
        self, temp_repo: Path
    ) -> None:
        _, updater = _index(
            temp_repo / "tsnestunres",
            {
                "src/users/users.service.ts": USERS_SERVICE,
                "src/main.ts": NEST_MAIN,
                "src/users/users.module.ts": "export const providers = []\n",
            },
        )

        assert not updater.factory.import_processor.unresolved_specifiers

    def test_esm_specifier_with_js_extension(self, temp_repo: Path) -> None:
        # nest writes `./hello/hello.module.js` in its ESM sources.
        ingestor, _ = _index(
            temp_repo / "tsesm",
            {
                "src/hello/hello.module.ts": (
                    "export function hello(): number { return 1 }\n"
                ),
                "src/app.module.ts": (
                    "import { hello } from './hello/hello.module.js'\n"
                    "export function boot(): number { return hello() }\n"
                ),
            },
        )

        assert ("src.app.module", "src.hello.hello.module") in _imports(ingestor)
        assert (
            _calls(ingestor).get(
                ("src.app.module.boot", "src.hello.hello.module.hello")
            )
            == cs.EdgeResolution.EXACT
        )

    def test_reexports_of_a_dotted_file(self, temp_repo: Path) -> None:
        ingestor, _ = _index(
            temp_repo / "tsbarrel",
            {
                "src/users/users.service.ts": USERS_SERVICE,
                "src/users/users.dto.ts": "export class UserDto { id = 1 }\n",
                "src/users/index.ts": (
                    "export { UsersService } from './users.service'\n"
                    "export { UserDto as Dto } from './users.dto'\n"
                ),
                "src/main.ts": (
                    "import { UsersService } from './users'\n"
                    "export const service = UsersService\n"
                ),
            },
        )

        imports = _imports(ingestor)
        assert ("src.users.index", "src.users.users.service") in imports, imports
        assert ("src.users.index", "src.users.users.dto") in imports, imports
        assert ("src.main", "src.users.index") in imports, imports

    def test_tsconfig_alias_landing_on_a_dotted_file(self, temp_repo: Path) -> None:
        root = temp_repo / "tsalias"
        _write(
            root,
            {
                "tsconfig.json": json.dumps(
                    {"compilerOptions": {"paths": {"@app/*": ["src/*"]}}}
                ),
            },
        )
        ingestor, _ = _index(
            root,
            {
                "src/utils/cli-colors.util.ts": (
                    "export function yellow(s: string): string { return s }\n"
                ),
                "src/main.ts": (
                    "import { yellow } from '@app/utils/cli-colors.util.js'\n"
                    "export function boot(): string { return yellow('x') }\n"
                ),
            },
        )

        assert ("src.main", "src.utils.cli-colors.util") in _imports(ingestor)

    def test_dotted_directory(self, temp_repo: Path) -> None:
        # A dot in a DIRECTORY name collides with the separator the same way.
        ingestor, _ = _index(
            temp_repo / "tsdotdir",
            {
                "src/v1.2/b.ts": "export function helper(): number { return 1 }\n",
                "src/v1.2/a.ts": (
                    "import { helper } from './b'\n"
                    "export function run(): number { return helper() }\n"
                ),
                "src/main.ts": (
                    "import { helper } from './v1.2/b'\n"
                    "export function boot(): number { return helper() }\n"
                ),
            },
        )

        imports = _imports(ingestor)
        assert ("src.v1.2.a", "src.v1.2.b") in imports, imports
        assert ("src.main", "src.v1.2.b") in imports, imports

    def test_whole_module_import_of_a_dotted_file_beside_its_plain_sibling(
        self, temp_repo: Path
    ) -> None:
        # A namespace import or a bare require binds the WHOLE dotted module,
        # so `app.config` is the target even though `app` is a real module
        # too (the named-import converse is in TestUnchangedNeighbours).
        ingestor, _ = _index(
            temp_repo / "tswhole",
            {
                "src/app.ts": "export const name = 'app'\n",
                "src/app.config.ts": "export const port = 1\n",
                "src/main.ts": (
                    "import * as config from './app.config'\n"
                    "export function boot(): number { return config.port }\n"
                ),
                "src/legacy.js": (
                    "const config = require('./app.config')\n"
                    "module.exports = { port: config.port }\n"
                ),
            },
        )

        imports = _imports(ingestor)
        assert ("src.main", "src.app.config") in imports, imports
        assert ("src.legacy", "src.app.config") in imports, imports
        assert ("src.main", "src.app") not in imports, imports
        assert ("src.legacy", "src.app") not in imports, imports

    def test_namespace_import_beside_a_directory_index(self, temp_repo: Path) -> None:
        # The same whole-module rule, with no dot involved: `src/index.ts`
        # made `* as utils from './utils'` land on the directory's index.
        ingestor, _ = _index(
            temp_repo / "tsnsidx",
            {
                "src/index.ts": "export const x = 1\n",
                "src/utils.ts": "export function u(): number { return 1 }\n",
                "src/app.ts": (
                    "import * as utils from './utils'\n"
                    "export function run(): number { return utils.u() }\n"
                ),
            },
        )

        assert _imports(ingestor) == {("src.app", "src.utils")}

    def test_dotted_directory_index_is_imported(self, temp_repo: Path) -> None:
        # `./v1.2` names the directory `v1.2/`, whose entry point is its
        # `index` file: the registry spells it `src.v1.2.index`, which the
        # dot-split prefix `src.v1.2` must reach the way `./v1` reaches
        # `src.v1.index`.
        for ext in (".ts", ".js"):
            ingestor, _ = _index(
                temp_repo / f"dotteddir{ext[1:]}",
                {
                    f"src/v1/index{ext}": "export function foo() { return 1 }\n",
                    f"src/v1.2/index{ext}": "export function bar() { return 2 }\n",
                    f"src/main{ext}": (
                        "import { foo } from './v1'\n"
                        "import { bar } from './v1.2'\n"
                        "export function run() { return foo() + bar() }\n"
                    ),
                },
            )
            imports = _imports(ingestor)
            assert ("src.main", "src.v1.2.index") in imports, (ext, imports)
            assert ("src.main", "src.v1.index") in imports, (ext, imports)

    def test_dotted_directory_without_an_index_imports_nothing(
        self, temp_repo: Path
    ) -> None:
        ingestor, _ = _index(
            temp_repo / "dotteddirnoindex",
            {
                "src/v1.2/util.ts": "export function bar() { return 2 }\n",
                "src/main.ts": (
                    "import { bar } from './v1.2'\n"
                    "export function run() { return bar() }\n"
                ),
            },
        )
        assert not {i for i in _imports(ingestor) if i[0] == "src.main"}


class TestOtherModuleFlavours:
    def test_commonjs_require_of_dotted_files(self, temp_repo: Path) -> None:
        ingestor, _ = _index(
            temp_repo / "jsreq",
            {
                "src/users.service.js": (
                    "function helper() { return 1 }\n"
                    "function other() { return 2 }\n"
                    "module.exports = { helper, other }\n"
                ),
                "src/users.controller.js": (
                    "const { helper } = require('./users.service')\n"
                    "function list() { return helper() }\n"
                    "module.exports = { list }\n"
                ),
                "src/users.test.js": (
                    "const svc = require('./users.service')\n"
                    "function t() { return svc.other() }\n"
                    "module.exports = { t }\n"
                ),
            },
        )

        imports = _imports(ingestor)
        assert ("src.users.controller", "src.users.service") in imports, imports
        assert ("src.users.test", "src.users.service") in imports, imports
        calls = _calls(ingestor)
        assert ("src.users.controller.list", "src.users.service.helper") in calls
        assert ("src.users.test.t", "src.users.service.other") in calls, calls

    def test_tsx_jsx_mjs_cjs(self, temp_repo: Path) -> None:
        ingestor, _ = _index(
            temp_repo / "jsflavours",
            {
                "src/Button.tsx": ("export function Button(): string { return 'b' }\n"),
                "src/Button.stories.tsx": (
                    "import { Button } from './Button'\n"
                    "export function Primary(): string { return Button() }\n"
                ),
                "src/Card.jsx": "export function Card() { return 'c' }\n",
                "src/Card.test.jsx": (
                    "import { Card } from './Card'\n"
                    "export function t() { return Card() }\n"
                ),
                "tools/vite.plugins.mjs": "export function plugins() { return [] }\n",
                "tools/vite.config.mjs": (
                    "import { plugins } from './vite.plugins.mjs'\n"
                    "export function config() { return plugins() }\n"
                ),
                "tools/jest.setup.cjs": "function setup() { return 1 }\n"
                "module.exports = { setup }\n",
                "tools/jest.config.cjs": (
                    "const { setup } = require('./jest.setup.cjs')\n"
                    "function config() { return setup() }\n"
                    "module.exports = { config }\n"
                ),
            },
        )

        imports = _imports(ingestor)
        assert {
            ("src.Button.stories", "src.Button"),
            ("src.Card.test", "src.Card"),
            ("tools.vite.config", "tools.vite.plugins"),
            ("tools.jest.config", "tools.jest.setup"),
        } <= imports, imports
        calls = _calls(ingestor)
        assert calls.get(("src.Button.stories.Primary", "src.Button.Button")) == (
            cs.EdgeResolution.EXACT
        ), calls
        assert calls.get(("src.Card.test.t", "src.Card.Card")) == (
            cs.EdgeResolution.EXACT
        ), calls
        assert ("tools.vite.config.config", "tools.vite.plugins.plugins") in calls
        assert ("tools.jest.config.config", "tools.jest.setup.setup") in calls


class TestUnchangedNeighbours:
    """What the fix must not move."""

    def test_plain_file_keeps_its_name_and_edges(self, temp_repo: Path) -> None:
        ingestor, _ = _index(
            temp_repo / "tsplain",
            {
                "src/users.ts": "export function findUser(): number { return 1 }\n",
                "src/main.ts": (
                    "import { findUser } from './users'\n"
                    "export function boot(): number { return findUser() }\n"
                ),
            },
        )

        assert {"src.users", "src.main"} <= _modules(ingestor)
        assert _imports(ingestor) == {("src.main", "src.users")}
        assert _calls(ingestor) == {
            ("src.main.boot", "src.users.findUser"): cs.EdgeResolution.EXACT
        }

    def test_dotted_module_qns_are_unchanged(self, temp_repo: Path) -> None:
        # The dot stays in the qn (as for a dotted C# stem), so an existing
        # graph keeps its names and only gains the missing edges.
        ingestor, _ = _index(
            temp_repo / "tsqn",
            {
                "src/users/users.service.ts": USERS_SERVICE,
                "src/url.test.ts": "export const x = 1\n",
            },
        )

        assert {"src.users.users.service", "src.url.test"} <= _modules(ingestor)

    def test_named_import_beside_a_dotted_sibling_targets_the_plain_file(
        self, temp_repo: Path
    ) -> None:
        # `{ test } from './url'` spells the same dotted string as the
        # module `url.test`; the binding names an export, so the edge goes to
        # `url`, never to the test file.
        ingestor, _ = _index(
            temp_repo / "tsambig",
            {
                "src/url.ts": "export function test(): number { return 1 }\n",
                "src/url.test.ts": "export const x = 1\n",
                "src/main.ts": (
                    "import { test } from './url'\n"
                    "export function boot(): number { return test() }\n"
                ),
            },
        )

        imports = _imports(ingestor)
        assert ("src.main", "src.url") in imports, imports
        assert ("src.main", "src.url.test") not in imports, imports

    def test_missing_dotted_target_stays_unresolved(self, temp_repo: Path) -> None:
        ingestor, updater = _index(
            temp_repo / "tsmissing",
            {
                "src/users.ts": "export const x = 1\n",
                "src/main.ts": (
                    "import { UsersService } from './users.service'\n"
                    "export function boot(): number { return 1 }\n"
                ),
            },
        )

        assert _imports(ingestor) == set()
        assert _external_imports(ingestor) == set()
        specifiers = updater.factory.import_processor.unresolved_specifiers
        assert {_tail(k): v for k, v in specifiers.items()} == {
            "src.main": {"./users.service"}
        }

    def test_dotted_specifier_spelling_another_languages_module(
        self, temp_repo: Path
    ) -> None:
        # `./helpers.v1` and the Python module `helpers/v1.py` share the qn
        # `proj.helpers.v1`; only a JS/TS module may answer a JS/TS import.
        ingestor, _ = _index(
            temp_repo / "tspoly",
            {
                "helpers/__init__.py": "",
                "helpers/v1.py": "def go():\n    return 1\n",
                "main.ts": (
                    "import { go } from './helpers.v1'\n"
                    "export function boot(): number { return go() }\n"
                ),
            },
        )

        assert "helpers.v1" in _modules(ingestor)
        assert _imports(ingestor) == set()

    def test_dotted_package_import_is_not_a_local_file(self, temp_repo: Path) -> None:
        # `lodash.debounce` is an npm package; a same-named local file must
        # not capture it.
        ingestor, _ = _index(
            temp_repo / "tspkg",
            {
                "src/lodash.debounce.ts": (
                    "export default function debounce(): number { return 1 }\n"
                ),
                "src/main.ts": (
                    "import debounce from 'lodash.debounce'\n"
                    "export function boot(): number { return debounce() }\n"
                ),
            },
        )

        assert _imports(ingestor) == set()
        assert ("src.main", "lodash.debounce") in _external_imports(ingestor)
        assert ("src.main.boot", "src.lodash.debounce.debounce") not in _calls(ingestor)
