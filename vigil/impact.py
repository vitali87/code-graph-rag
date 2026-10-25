import importlib.metadata as md
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

from packaging.utils import canonicalize_name

from codebase_rag import cypher_queries as cq
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import PropertyValue

from . import constants as vc
from .types_defs import Cone

# Importers of a package root or of any module under it: `import
# google.protobuf.message` reaches protobuf, `google.genai` does not.
CYPHER_IMPORTERS_UNDER = """MATCH (m:Module)-[r:IMPORTS]->(target)
WHERE (target.qualified_name = $root OR target.qualified_name STARTS WITH $root_dot)
  AND m.qualified_name STARTS WITH $project_prefix
RETURN DISTINCT m.qualified_name AS qualified_name, m.path AS path"""

_MODULE_SUFFIXES = (".py", ".pyi", ".so", ".pyd")
_NOT_IMPORTABLE = (".dist-info", ".data", ".egg-info")
_NOT_IMPORTABLE_ROOTS = frozenset({"__pycache__", "..", "bin", "share", "include"})


def _top_parts(path: str) -> tuple[str, ...] | None:
    parts = PurePosixPath(path).parts
    if not parts or parts[0] in _NOT_IMPORTABLE_ROOTS:
        return None
    if parts[0].endswith(_NOT_IMPORTABLE) or not path.endswith(_MODULE_SUFFIXES):
        return None
    # `six.py` and `_cffi_backend.cpython-312-x86_64-linux-gnu.so` are
    # modules named by the text before the first dot.
    return tuple(p.split(".", 1)[0] for p in parts)


def import_roots(files: Mapping[str, Iterable[str]], package: str) -> list[str]:
    """The names `package` is imported by, from each distribution's files.

    A root several distributions install into is a namespace package
    (protobuf, google-auth and google-genai all ship `google/`), and
    importers of the bare namespace would charge every one of them for the
    others' imports, so a shared root is narrowed to the next level:
    protobuf is `google.protobuf`.
    """
    tops: dict[str, set[tuple[str, ...]]] = {}
    for dist, paths in files.items():
        tops[dist] = {p for path in paths if (p := _top_parts(path)) is not None}
    owners = Counter(root for parts in tops.values() for root in {p[0] for p in parts})
    roots: set[str] = set()
    for parts in tops.get(canonicalize_name(package), set()):
        if owners[parts[0]] > 1 and len(parts) > 2:
            roots.add(f"{parts[0]}.{parts[1]}")
        elif owners[parts[0]] == 1:
            roots.add(parts[0])
    return sorted(roots)


def installed_files() -> dict[str, list[str]]:
    return {
        canonicalize_name(d.metadata["Name"]): [str(f) for f in (d.files or [])]
        for d in md.distributions()
    }


def _is_test(path: str) -> bool:
    return path.startswith(vc.TESTS_PREFIX)


def _graph(repo: Path) -> tuple[str, MemgraphIngestor]:
    from codebase_rag.cli_runtime import connect_memgraph
    from codebase_rag.config import settings
    from codebase_rag.utils.path_utils import derive_project_name

    project = derive_project_name(repo.resolve())
    return f"{project}.", connect_memgraph(batch_size=settings.resolve_batch_size(None))


def _importers(
    ingestor: MemgraphIngestor, query: str, params: dict[str, PropertyValue]
) -> list[tuple[str, str]]:
    """(module qualified name, file path) per row; a row without a path is a
    module the graph knows by name only and cannot select a file to run."""
    out: list[tuple[str, str]] = []
    for row in ingestor.fetch_all(query, params):
        qn, path = row.get("qualified_name"), row.get("path")
        if isinstance(qn, str) and isinstance(path, str) and path:
            out.append((qn, path))
    return out


def _tests_importing(
    ingestor: MemgraphIngestor, prefix: str, module_qns: Iterable[str]
) -> set[str]:
    qns = sorted(set(module_qns))
    if not qns:
        return set()
    rows = _importers(
        ingestor, cq.CYPHER_GRAPH_IMPORTERS_OF, {"qns": qns, "project_prefix": prefix}
    )
    return {path for _qn, path in rows if _is_test(path)}


def cone(repo: Path, package: str, imports: list[str]) -> Cone:
    """What `package` reaches in cgr, asked of cgr's own graph of itself.

    Source files import the package directly. Test files are the ones that
    import it, plus the ones importing any of those source modules: one hop,
    enough to cover a module's own tests without dragging in the whole suite
    through `graph_updater`, which nearly every test reaches transitively.
    """
    prefix, ingestor = _graph(repo)
    with ingestor:
        rows = [
            row
            for root in imports
            for row in _importers(
                ingestor,
                CYPHER_IMPORTERS_UNDER,
                {"root": root, "root_dot": f"{root}.", "project_prefix": prefix},
            )
        ]
        source = {qn: path for qn, path in rows if not _is_test(path)}
        tests = _tests_importing(ingestor, prefix, source)
    tests |= {path for _qn, path in rows if _is_test(path)}
    return Cone(
        package=package,
        imports=imports,
        source_files=sorted(set(source.values())),
        test_files=sorted(t for t in tests if t.endswith(vc.PY_SUFFIX)),
    )


def module_qn(prefix: str, path: str) -> str:
    dotted = path.removesuffix(vc.PY_SUFFIX).replace("/", ".")
    return prefix + dotted.removesuffix(".__init__")


def tests_for_paths(repo: Path, paths: list[str]) -> list[str]:
    """Test files that import any of the changed modules, by the graph."""
    prefix, ingestor = _graph(repo)
    with ingestor:
        tests = _tests_importing(
            ingestor, prefix, (module_qn(prefix, p) for p in paths)
        )
    return sorted(tests)
