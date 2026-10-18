# Do two graph backends answer cgr's read queries the same way? (issue #386)
#
# Runs every read query the package ships against one graph held by two
# engines and compares the rows. The embedded SQLite graph was checked this
# way against Memgraph before it became the default: on a 38k-node graph all
# 154 read queries returned the same rows.
#
#   1. dump  -- copy a graph out of a backend that holds it, with query
#               parameters picked from that graph (a real project, its most
#               called functions, ...)
#   2. run   -- load the dump into a fresh embedded graph through cgr's own
#               ingestor (or, for the backend the dump came from, use it as
#               it is) and record every read query's rows and time
#   3. compare -- the two recordings, query by query
#
#   uv run python -m evals.graph_backend_parity dump --engine memgraph \
#       --project my-repo graph.json
#   uv run python -m evals.graph_backend_parity run graph.json memgraph.json \
#       --engine memgraph
#   uv run python -m evals.graph_backend_parity run graph.json sqlite.json
#   uv run python -m evals.graph_backend_parity compare memgraph.json sqlite.json
from __future__ import annotations

import importlib
import json
import pkgutil
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Annotated

import typer
from loguru import logger

import codebase_rag
from codebase_rag import constants as cs
from codebase_rag.graph_dialects import get_dialect
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import JsonValue, PropertyValue, ResultRow, ResultValue

from . import constants as ec
from . import logs as ls
from .types_defs import ParityDump, ParityQueryRecording, ParityRecording

app = typer.Typer(no_args_is_help=True)

Row = list[JsonValue]


def _modules() -> list[ModuleType]:
    """Every module of the package, where the query constants live."""
    modules: list[ModuleType] = []
    for info in pkgutil.walk_packages(codebase_rag.__path__, ec.PARITY_PACKAGE_PREFIX):
        if ec.PARITY_SKIP_MODULE in info.name or info.name.endswith(ec.PARITY_MAIN):
            continue
        try:
            modules.append(importlib.import_module(info.name))
        except ImportError:  # an optional extra this environment lacks
            continue
    return modules


def read_queries() -> dict[str, str]:
    """Every read query the package defines, by qualified constant name."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    for module in _modules():
        for name, text in vars(module).items():
            if (
                name.startswith(ec.PARITY_QUERY_PREFIX)
                and name not in ec.PARITY_FRAGMENTS
                and isinstance(text, str)
                and text not in seen
                and ec.PARITY_READ_PATTERN.search(text)
                and not ec.PARITY_WRITE_PATTERN.search(text)
                and not ec.PARITY_FORMAT_FIELD_PATTERN.search(text)
            ):
                seen.add(text)
                out[f"{module.__name__}.{name}"] = text
    return dict(sorted(out.items()))


def _ingestor(engine: ec.ParityEngine, sqlite_path: str | None) -> MemgraphIngestor:
    from codebase_rag.config import settings

    return MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        username=settings.MEMGRAPH_USERNAME,
        password=settings.MEMGRAPH_PASSWORD,
        dialect=get_dialect(engine),
        sqlite_path=sqlite_path,
    )


def _text(row: ResultRow, column: str) -> str | None:
    value = row.get(column)
    return value if isinstance(value, str) else None


def _params(ingestor: MemgraphIngestor, project: str) -> dict[str, PropertyValue]:
    """Parameters every query can use, picked from one project of the graph."""
    prefix = project + cs.SEPARATOR_DOT
    roots = ingestor.fetch_all(ec.PARITY_PROJECT_ROOT, {"name": project})
    root = _text(roots[0], "root") if roots else None
    top = ingestor.fetch_all(ec.PARITY_TOP_CALLEES, {"prefix": prefix})
    modules = ingestor.fetch_all(ec.PARITY_MODULES, {"prefix": prefix})
    files = ingestor.fetch_all(ec.PARITY_FILES, {"root": root})
    qns = [qn for t in top if (qn := _text(t, "qn"))]
    name = _text(top[0], "name") if top else None
    line = top[0].get("line") if top else None
    paths = [p for f in files if (p := _text(f, "path"))]
    absolute = [p for f in files if (p := _text(f, "absolute_path"))]
    return {
        "project_name": project,
        "project": project,
        "project_names": [project],
        "project_prefix": prefix,
        "prefix": prefix,
        "repo_prefix": prefix,
        "variant_prefix": prefix,
        "prefixes": [prefix],
        "longer_project_prefixes": [],
        "qn": qns[0] if qns else None,
        "qns": qns,
        "callers": qns,
        "to_qns": qns,
        "from_qns": qns,
        "module_qns": [qn for m in modules if (qn := _text(m, "qn"))],
        "module_names": [n for m in modules if (n := _text(m, "name"))],
        "name": name,
        "names": [n for t in top if (n := _text(t, "name"))],
        "suffix": cs.SEPARATOR_DOT + name if name else None,
        "label": cs.NodeLabel.FUNCTION.value,
        "line": line if isinstance(line, int) else None,
        "path": paths[0] if paths else None,
        "paths": paths,
        "absolute_path": absolute[0] if absolute else None,
        "absolute_paths": absolute,
        "repo_root": root,
        "extensions": [cs.EXT_PY],
        "hashes": [],
        "node_id": 0,
        "run_id": ec.PARITY_PARAM_FALLBACK,
    }


@app.command()
def dump(
    out: Annotated[Path, typer.Argument(help="Where to write the graph JSON")],
    project: Annotated[str, typer.Option(help="Project whose names parametrize")],
    engine: Annotated[ec.ParityEngine, typer.Option()] = ec.ParityEngine.MEMGRAPH,
    sqlite_path: Annotated[str | None, typer.Option()] = None,
) -> None:
    with _ingestor(engine, sqlite_path) as ingestor:
        nodes = ingestor.fetch_all(ec.PARITY_DUMP_NODES)
        rels = ingestor.fetch_all(ec.PARITY_DUMP_RELS)
        params = _params(ingestor, project)
    out.write_text(
        json.dumps({"nodes": nodes, "rels": rels, "params": params}, default=str)
    )
    logger.info(
        ls.PARITY_DUMPED.format(nodes=len(nodes), rels=len(rels), project=project)
    )


def _load(ingestor: MemgraphIngestor, graph: ParityDump) -> None:
    """Write a dump through the ingestor's own batched MERGE/CREATE path."""
    keys = cs.NODE_UNIQUE_CONSTRAINTS
    ingestor.ensure_constraints()
    for node in graph["nodes"]:
        ingestor.ensure_node_batch(node["labels"][0], node["props"])
    for rel in graph["rels"]:
        fk, tk = keys[rel["from_label"]], keys[rel["to_label"]]
        ingestor.ensure_relationship_batch(
            (rel["from_label"], fk, rel["from_props"][fk]),
            rel["type"],
            (rel["to_label"], tk, rel["to_props"][tk]),
            rel["props"],
        )
    ingestor.flush_all()


def _bag(values: Iterable[JsonValue]) -> list[JsonValue]:
    """Values in a fixed order, for lists whose order Cypher does not promise."""
    out: list[JsonValue] = list(values)
    out.sort(key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return out


def _tagged(*parts: JsonValue) -> JsonValue:
    out: list[JsonValue] = list(parts)
    return out


def _comparable(value: ResultValue) -> JsonValue:
    """A value with engine-specific shapes (node objects, ids) normalised.

    Rows are typed as the ingestor returns them, but the Memgraph driver
    hands back its own Node and Relationship objects, recognised here by
    their attributes.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return round(value, ec.PARITY_FLOAT_DIGITS)
    labels = getattr(value, "labels", None)
    if labels is not None and hasattr(value, "properties"):
        props = dict(getattr(value, "properties"))
        names = _bag(str(x) for x in labels)
        return _tagged(ec.PARITY_TAG_NODE, names, _comparable(props))
    rel_type = getattr(value, "type", None)
    if isinstance(rel_type, str) and hasattr(value, "properties"):
        props = dict(getattr(value, "properties"))
        return _tagged(ec.PARITY_TAG_REL, rel_type, _comparable(props))
    if isinstance(value, dict):
        if "labels" in value and "properties" in value:
            node_labels = value["labels"]
            listed = node_labels if isinstance(node_labels, list) else []
            names = _bag(str(x) for x in listed)
            return _tagged(ec.PARITY_TAG_NODE, names, _comparable(value["properties"]))
        if "type" in value and "start" in value:
            return _tagged(
                ec.PARITY_TAG_REL, str(value["type"]), _comparable(value["properties"])
            )
        return _bag(_tagged(str(k), _comparable(v)) for k, v in value.items())
    return _bag(_comparable(v) for v in value)


@app.command()
def run(
    graph_file: Annotated[Path, typer.Argument(help="A dump written by `dump`")],
    out: Annotated[Path, typer.Argument(help="Where to write the recording")],
    engine: Annotated[ec.ParityEngine, typer.Option()] = ec.ParityEngine.SQLITE,
) -> None:
    graph: ParityDump = json.loads(graph_file.read_text())
    params = graph["params"]
    recording = ParityRecording(engine=engine.value)
    results: dict[str, ParityQueryRecording] = {}
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_path = str(Path(tmp) / cs.EG_DEFAULT_DB_FILENAME)
        with _ingestor(engine, sqlite_path) as ingestor:
            if engine == ec.ParityEngine.SQLITE:
                start = time.perf_counter()
                _load(ingestor, graph)
                recording["load_s"] = time.perf_counter() - start
                logger.info(
                    ls.PARITY_LOADED.format(engine=engine, seconds=recording["load_s"])
                )
            for name, query in read_queries().items():
                needed = ec.PARITY_PARAM_PATTERN.findall(query)
                args = {k: params.get(k, ec.PARITY_PARAM_FALLBACK) for k in needed}
                start = time.perf_counter()
                try:
                    rows = ingestor.fetch_all(query, args)
                except Exception as e:  # recorded, not fatal: one query's error
                    results[name] = ParityQueryRecording(error=str(e).splitlines()[0])
                    continue
                results[name] = ParityQueryRecording(
                    columns=list(rows[0]) if rows else [],
                    rows=[[_comparable(v) for v in row.values()] for row in rows],
                    ms=(time.perf_counter() - start) * ec.PARITY_MS_PER_S,
                )
    recording["results"] = results
    out.write_text(json.dumps(recording, default=str))
    logger.info(ls.PARITY_RAN.format(count=len(results), engine=engine, path=out))


def _canonical(result: ParityQueryRecording) -> list[Row]:
    columns = result.get("columns", [])
    masked = {i for i, c in enumerate(columns) if ec.PARITY_ID_COLUMN_PATTERN.search(c)}
    order = sorted(range(len(columns)), key=lambda i: columns[i])
    return [
        [ec.PARITY_ID_MASK if i in masked else row[i] for i in order]
        for row in result.get("rows", [])
    ]


def same_rows(
    query: str, reference: ParityQueryRecording, candidate: ParityQueryRecording
) -> bool:
    """Whether two recordings of one query agree.

    Columns are matched by name and engine ids masked. Rows compare as a
    multiset: ORDER BY ties may come back in either order. With LIMIT and no
    ORDER BY the engines may pick different rows, so only the count counts.
    """
    if sorted(reference.get("columns", [])) != sorted(candidate.get("columns", [])):
        return not reference.get("rows") and not candidate.get("rows")
    a, b = _canonical(reference), _canonical(candidate)
    if ec.PARITY_LIMIT_PATTERN.search(query) and not ec.PARITY_ORDER_BY_PATTERN.search(
        query
    ):
        return len(a) == len(b)

    def key(row: Row) -> str:
        return json.dumps(row, sort_keys=True, default=str)

    return Counter(map(key, a)) == Counter(map(key, b))


@app.command()
def compare(
    reference_file: Annotated[Path, typer.Argument()],
    candidate_file: Annotated[Path, typer.Argument()],
) -> None:
    reference: ParityRecording = json.loads(reference_file.read_text())
    candidate: ParityRecording = json.loads(candidate_file.read_text())
    queries = read_queries()
    same, differ = 0, []
    errors: list[tuple[str, str | None]] = []
    ref_s = cand_s = 0.0
    candidate_results = candidate.get("results", {})
    for name, ref in reference.get("results", {}).items():
        cand = candidate_results.get(name)
        if "error" in ref or name not in queries:
            continue
        if cand is None or "error" in cand:
            errors.append((name, (cand or {}).get("error")))
            continue
        ref_s += ref.get("ms", 0.0) / ec.PARITY_MS_PER_S
        cand_s += cand.get("ms", 0.0) / ec.PARITY_MS_PER_S
        if same_rows(queries[name], ref, cand):
            same += 1
        else:
            differ.append(name)
    logger.info(
        ls.PARITY_SUMMARY.format(
            engine=candidate.get("engine"),
            reference=reference.get("engine"),
            same=same,
            total=same + len(differ) + len(errors),
            differ=len(differ),
            errors=len(errors),
            engine_s=cand_s,
            reference_s=ref_s,
        )
    )
    for name in differ:
        logger.info(ls.PARITY_DIFFERS.format(name=name))
    for name, error in errors:
        logger.info(ls.PARITY_ERRORS.format(name=name, error=error))
    if differ or errors:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
