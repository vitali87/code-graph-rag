"""Which module an import names, where several files could answer to it.

An importer writes a module the way its language spells it; the graph may
hold that module under another qualified name. A container module lives at
its index file (`pkg/__init__.py`, `shared/index.js`, `utils/mod.rs`), a
path resolved from the wrong root still names one real module by its
suffix, and a stem shared across language families (`util.py` beside
`util.js`) carries each file's extension, so each importer is pointed at the
file of its own family. These are the lookups the import pass verifies an
internal target with and re-points an import map by; none of them writes."""

from collections.abc import Mapping
from pathlib import Path

from .. import constants as cs
from ..language_spec import (
    LANGUAGE_SPECS,
    get_language_for_extension,
    language_family,
    own_extension_module_qn,
)
from ..types_defs import LanguageFamily, StemSiblingModules
from ..utils.path_utils import base_module_qn, declaration_extension, module_extension


def stem_sibling_modules(
    module_paths: Mapping[str, Path | str], repo_path: Path, project_name: str
) -> StemSiblingModules:
    """Each family's module on a stem whose files carry their extension.

    A module qn that is its path-derived name plus its own extension was
    suffixed by the disambiguator, and importers of its language still
    write the bare name. Where one family has several such files (`util.js`
    and `util.ts` beside `util.py`), the one the in-family rule would have
    named bare is taken: an implementation over a declaration, then the
    first in walk order.
    """
    found: list[tuple[tuple[bool, str], str, LanguageFamily, str]] = []
    for qn, raw_path in module_paths.items():
        if not raw_path:
            continue
        path = Path(raw_path)
        language = get_language_for_extension(path.suffix)
        # Most modules carry no extension segment; skip them before paying
        # for the path arithmetic below.
        if language is None or not qn.endswith(module_extension(path.name)):
            continue
        try:
            rel = path.relative_to(repo_path)
        except ValueError:
            # A module whose file is not under the repository root has no
            # repo-relative name the disambiguator could have suffixed, so
            # it is nobody's stem sibling.
            continue
        base = base_module_qn(rel, project_name)
        if qn == own_extension_module_qn(base, path.name):
            rank = (declaration_extension(path.name) is not None, rel.as_posix())
            found.append((rank, base, language_family(language), qn))
    siblings: StemSiblingModules = {}
    for _rank, base, family, qn in sorted(found, key=lambda item: item[0]):
        siblings.setdefault(base, {}).setdefault(family, qn)
    return siblings


def own_language_target(
    name: str,
    family: LanguageFamily,
    module_paths: Mapping[str, Path | str],
    siblings: StemSiblingModules,
) -> str:
    """`name` re-rooted on its stem's module of the importer's family.

    The longest prefix that names a module decides: one the importer can
    reach keeps the name as written, and a stem whose module is missing or
    in another family is swapped for the family's own sibling. A name
    under a real submodule (`proj.util.sub.f`) therefore stays put.
    """
    parts = name.split(cs.SEPARATOR_DOT)
    for end in range(len(parts), 1, -1):
        prefix = cs.SEPARATOR_DOT.join(parts[:end])
        holder = module_paths.get(prefix)
        if holder is not None and _reachable_module(holder, family):
            return name
        own = siblings.get(prefix, {}).get(family)
        if own is not None:
            return f"{own}{name[len(prefix) :]}"
        if holder is not None:
            return name
    return name


def _reachable_module(path: Path | str, family: LanguageFamily) -> bool:
    # A module with no file (an inline Rust `mod`, a rehydrated qn) or of
    # an unknown language is given the benefit of the doubt.
    if not path:
        return True
    language = get_language_for_extension(Path(path).suffix)
    return language is None or language in family


def module_alias_map(known_module_qns: set[str]) -> dict[str, str]:
    """Each container module's written qn, to the index-file module the
    graph holds it under."""
    # A module reached through its container's name: pkg/__init__.py,
    # shared/index.js, utils/mod.rs. Importers write the container qn; the real
    # Module node lives at the index-file leaf.
    aliases: dict[str, str] = {}
    for qn in known_module_qns:
        base, _, leaf = qn.rpartition(cs.SEPARATOR_DOT)
        if base and leaf in cs.MODULE_INDEX_FILE_STEMS:
            aliases[base] = qn
    return aliases


def verify_internal_import_target(
    module_path: str,
    known_module_paths: dict[str, str],
    module_aliases: dict[str, str],
    language: cs.SupportedLanguage,
    project_name: str,
    siblings: StemSiblingModules | None = None,
) -> str | None:
    """The real module an internal import names, or None where none or
    several answer."""
    if siblings:
        module_path = own_language_target(
            module_path, language_family(language), known_module_paths, siblings
        )
    if module_path in known_module_paths:
        return module_path
    if alias := module_aliases.get(module_path):
        return alias
    # A path resolved from the wrong root (`use crate::utils` written outside
    # src/) still names a unique real module; a whole-segment suffix match
    # recovers it. Ambiguity means no edge, not a guess.
    prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    if not module_path.startswith(prefix):
        return None
    tail = module_path[len(prefix) :]
    if not tail:
        return None
    suffix = f"{cs.SEPARATOR_DOT}{tail}"
    matches = {qn for qn in known_module_paths if qn.endswith(suffix)}
    matches.update(
        real for base, real in module_aliases.items() if base.endswith(suffix)
    )
    if len(matches) > 1:
        # A polyglot repo can hold same-named modules in several
        # languages (thrift: src/protocol/__init__.py vs
        # src/ext/protocol.h); the importing language can only target
        # its own modules, so tie-break by file extension. Inline
        # modules (no file) stay eligible.
        matches = {
            qn
            for qn in matches
            if _path_matches_language(known_module_paths.get(qn, ""), language)
        }
    if len(matches) == 1:
        return matches.pop()
    return None


def _path_matches_language(path: str, language: cs.SupportedLanguage) -> bool:
    if not path:
        return True
    spec = LANGUAGE_SPECS.get(language)
    if spec is None:
        return True
    return path.endswith(tuple(spec.file_extensions))
