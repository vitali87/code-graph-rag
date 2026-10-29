"""The C# joins a sync runs once the passes have registered every type and
function: the Roslyn partial groups after Pass 2, and its LINQ query-operator
calls after Pass 3."""

from pathlib import Path

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...services import IngestorProtocol
from ...types_defs import FunctionLocation
from ...utils.path_utils import cached_relative_path
from ..definition_processor import DefinitionProcessor


def join_partials(dp: DefinitionProcessor) -> None:
    # Replace the directory-keyed syntactic partial grouping with the
    # Roslyn symbol-identity groups wherever Roslyn saw the type: parts
    # in DIFFERENT directories of one project merge (the syntactic rule
    # deliberately under-merges there), and unrelated same-name types a
    # syntactic merge would conflate split apart (each arrives as its
    # own group). Group lists stay SHARED objects because the resolver
    # compares them by identity.
    partial_decls = dp.facts.frontend_facts[cs.SupportedLanguage.CSHARP].partial_groups
    if not partial_decls:
        return
    groups: list[list[str]] = []
    covered: set[str] = set()
    for decls in partial_decls:
        qns = sorted({qn for d in decls if (qn := dp.csharp_type_locations.get(d))})
        covered.update(qns)
        if len(qns) > 1:
            groups.append(qns)
    for qn in covered:
        old = dp.facts.csharp_partial_groups.pop(qn, None)
        if old is not None and qn in old:
            # Also shrink the shared syntactic list so members NOT
            # covered by Roslyn stop spanning to this part.
            old.remove(qn)
    for group in groups:
        for qn in group:
            dp.facts.csharp_partial_groups[qn] = group
    if groups:
        logger.info(ls.CSHARP_FRONTEND_PARTIALS_JOINED.format(count=len(groups)))


def emit_query_calls(
    dp: DefinitionProcessor, repo_path: Path, sink: IngestorProtocol
) -> None:
    # LINQ query syntax has no invocation nodes for tree-sitter to see;
    # each Roslyn query-operator fact becomes a direct CALLS edge, both
    # ends resolved through the Pass-2 function-location registry (a miss
    # on either end drops the fact rather than risk a dangling edge).
    query_calls = dp.facts.frontend_facts[cs.SupportedLanguage.CSHARP].query_calls
    if not query_calls:
        return
    rel_to_module = {
        cached_relative_path(path, repo_path).as_posix(): qn
        for qn, path in dp.module_qn_to_file_path.items()
    }

    def located(rel_file: str, line: int, col: int) -> FunctionLocation | None:
        module_qn = rel_to_module.get(rel_file)
        if module_qn is None:
            return None
        return dp.facts.function_locations.get((module_qn, line, col))

    emitted = 0
    for fact in query_calls:
        caller = located(fact.caller_file, fact.caller_line, fact.caller_col)
        target = located(fact.target_file, fact.target_line, fact.target_col)
        if caller is None or target is None:
            continue
        # Through `sink` (the capture-filtering wrapper), not the raw
        # ingestor, so a capture that disables CALLS suppresses these
        # synthesized LINQ query-operator edges too (issue #1236).
        sink.ensure_relationship_batch(
            (caller.label, cs.KEY_QUALIFIED_NAME, caller.qualified_name),
            cs.RelationshipType.CALLS,
            (target.label, cs.KEY_QUALIFIED_NAME, target.qualified_name),
        )
        emitted += 1
    if emitted:
        logger.info(ls.CSHARP_FRONTEND_QUERY_EDGES.format(count=emitted))
