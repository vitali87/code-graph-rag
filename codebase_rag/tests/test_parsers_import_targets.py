"""Which module an internal import names: a container written for its index
file, a path resolved from the wrong root recovered by a unique suffix, a
polyglot tie broken by the importer's extension, and a stem shared across
language families re-pointed at the importer's own family's file."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.language_spec import language_family
from codebase_rag.parsers.import_targets import (
    module_alias_map,
    own_language_target,
    stem_sibling_modules,
    verify_internal_import_target,
)

_REPO = Path("/repo")
_PY = language_family(cs.SupportedLanguage.PYTHON)
_JS_TS = language_family(cs.SupportedLanguage.TS)

# `util.py` beside `util.js`, `util.ts` and `util.d.ts`: the disambiguator
# suffixed each with its extension, so no module is named `p.util` itself.
_SUFFIXED: dict[str, Path | str] = {
    "p.util.py": _REPO / "util.py",
    "p.util.d.ts": _REPO / "util.d.ts",
    "p.util.ts": _REPO / "util.ts",
    "p.util.js": _REPO / "util.js",
    "p.lib.helpers": _REPO / "lib/helpers.py",
    # Not under the repository: no repo-relative name could be suffixed.
    "p.vendor.py": Path("/elsewhere/vendor.py"),
    # An inline module with no file.
    "p.inline": "",
}


def test_each_family_gets_one_sibling_preferring_an_implementation() -> None:
    assert stem_sibling_modules(_SUFFIXED, _REPO, "p") == {
        # `.d.ts` sorts first by path but is only a declaration, and of the
        # implementations the first in walk order is taken.
        "p.util": {_PY: "p.util.py", _JS_TS: "p.util.js"},
    }


def test_a_stem_with_no_suffixed_module_has_no_siblings() -> None:
    plain: dict[str, Path | str] = {
        "p.util": _REPO / "util.py",
        "p.app": _REPO / "app.ts",
    }
    assert stem_sibling_modules(plain, _REPO, "p") == {}


def test_a_name_on_a_shared_stem_is_re_rooted_on_the_importers_family() -> None:
    siblings = stem_sibling_modules(_SUFFIXED, _REPO, "p")
    assert (
        own_language_target("p.util.helper", _JS_TS, _SUFFIXED, siblings)
        == "p.util.js.helper"
    )
    assert (
        own_language_target("p.util.helper", _PY, _SUFFIXED, siblings)
        == "p.util.py.helper"
    )
    assert own_language_target("p.util", _JS_TS, _SUFFIXED, siblings) == "p.util.js"


def test_a_name_under_a_module_the_importer_reaches_stays_as_written() -> None:
    paths: dict[str, Path | str] = {
        "p.util": _REPO / "util.py",
        "p.util.js": _REPO / "util.js",
    }
    siblings = stem_sibling_modules(paths, _REPO, "p")
    assert own_language_target("p.util.helper", _PY, paths, siblings) == (
        "p.util.helper"
    )
    # Python's `p.util` is no module a JS importer reaches.
    assert own_language_target("p.util.helper", _JS_TS, paths, siblings) == (
        "p.util.js.helper"
    )


def test_a_module_of_another_family_without_a_sibling_is_left_alone() -> None:
    siblings = stem_sibling_modules(_SUFFIXED, _REPO, "p")
    assert (
        own_language_target("p.lib.helpers.f", _JS_TS, _SUFFIXED, siblings)
        == "p.lib.helpers.f"
    )


def test_a_container_module_is_aliased_to_its_index_file() -> None:
    aliases = module_alias_map(
        {
            "p.pkg.__init__",
            "p.shared.index",
            "p.utils.mod",
            "p.scripts.init",
            "p.a.b",
            # An index file at no container names nothing else.
            "index",
        }
    )
    assert aliases == {
        "p.pkg": "p.pkg.__init__",
        "p.shared": "p.shared.index",
        "p.utils": "p.utils.mod",
        "p.scripts": "p.scripts.init",
    }


def _verify(
    module_path: str,
    known: dict[str, str],
    language: cs.SupportedLanguage = cs.SupportedLanguage.PYTHON,
    aliases: dict[str, str] | None = None,
) -> str | None:
    return verify_internal_import_target(
        module_path, known, aliases or {}, language, "p"
    )


def test_a_known_module_or_its_container_alias_is_the_target() -> None:
    known = {"p.app": "app.py", "p.shared.index": "shared/index.js"}
    aliases = {"p.shared": "p.shared.index"}
    assert _verify("p.app", known, aliases=aliases) == "p.app"
    assert _verify("p.shared", known, aliases=aliases) == "p.shared.index"


def test_a_wrong_root_is_recovered_by_a_unique_whole_segment_suffix() -> None:
    known = {"p.src.utils": "src/utils.rs", "p.src.myutils": "src/myutils.rs"}
    assert _verify("p.utils", known, cs.SupportedLanguage.RUST) == "p.src.utils"


def test_an_ambiguous_suffix_or_a_foreign_name_links_nothing() -> None:
    known = {"p.a.utils": "a/utils.py", "p.b.utils": "b/utils.py"}
    assert _verify("p.utils", known) is None
    assert _verify("other.utils", known) is None
    assert _verify("p.", known) is None


def test_a_polyglot_tie_is_broken_by_the_importers_extension() -> None:
    known = {"p.src.protocol": "src/protocol.py", "p.src.ext.protocol": "src/ext.h"}
    assert _verify("p.protocol", known) == "p.src.protocol"
    assert _verify("p.protocol", known, cs.SupportedLanguage.CPP) == (
        "p.src.ext.protocol"
    )


def test_an_inline_module_stays_eligible_in_a_tie() -> None:
    known = {"p.a.utils": "", "p.b.utils": "b/utils.py"}
    assert _verify("p.utils", known, cs.SupportedLanguage.RUST) == "p.a.utils"


def test_siblings_point_the_import_at_the_importers_own_file() -> None:
    known = {qn: str(path) for qn, path in _SUFFIXED.items()}
    siblings = stem_sibling_modules(_SUFFIXED, _REPO, "p")
    assert (
        verify_internal_import_target(
            "p.util", known, {}, cs.SupportedLanguage.TS, "p", siblings
        )
        == "p.util.js"
    )
    # Without them the bare stem names no module.
    assert (
        verify_internal_import_target("p.util", known, {}, cs.SupportedLanguage.TS, "p")
        is None
    )
