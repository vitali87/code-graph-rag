"""Definition and call-site rewrite helpers for change_signature."""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from .. import constants as cs
from .. import graph_query
from ..parsers.call_processor import _find_call_arguments_node
from ..types_defs import ResultRow
from .patcher import Patcher, PatcherError
from .signature_calls import _call_at, _map_arguments, _site_end
from .signature_params import (
    _check_default_order,
    _parameters,
    _render_param,
    _rendered_old,
    _restore_positional_only,
    _text,
)
from .signature_types import ParamSpec, RewrittenSite, SignatureRefused, UnmappedSite

_AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)


def rewrite_definition(
    patcher: Patcher,
    path: str,
    node: Node,
    language: cs.SupportedLanguage | None,
    specs: list[ParamSpec],
) -> None:
    params_node = node.child_by_field_name(cs.FIELD_PARAMETERS)
    assert params_node is not None
    old = _parameters(node, language)
    positional = [p for p in old if not p.receiver and not p.keyword_only]
    rendered = [_text(p.node) for p in old if p.receiver]
    for spec in specs:
        if spec.from_index is not None:
            if spec.from_index >= len(positional):
                # An override with fewer parameters than the target
                # cannot supply the old value the spec maps from.
                raise SignatureRefused(
                    cs.SIGNATURE_UNKNOWN_SOURCE.format(
                        source=spec.from_index,
                        names=", ".join(p.name for p in positional),
                    )
                )
            rendered.append(_rendered_old(positional[spec.from_index], spec, language))
        else:
            rendered.append(_render_param(spec, language))
    if language == cs.SupportedLanguage.PYTHON:
        _check_default_order(positional, specs)
        _restore_positional_only(rendered, node, old, specs)
    keyword_only = [p for p in old if p.keyword_only]
    if keyword_only:
        # The keyword-only section is not positional: it follows the
        # rendered positionals behind its own `*`, exactly as written.
        rendered.append("*")
        rendered.extend(_text(p.node) for p in keyword_only)
    text = "(" + ", ".join(rendered) + ")"
    patcher.replace_span(path, (params_node.start_byte, params_node.end_byte), text)


def rewrite_site(
    patcher: Patcher,
    row: graph_query.CallSiteRow | ResultRow,
    owner_qn: str,
    old_names: list[str],
    specs: list[ParamSpec],
    allow_heuristic: bool,
    parse_fn: Callable[[str, bytes], tuple[cs.SupportedLanguage | None, Node | None]],
    keyword_only: frozenset[str],
    sites: list[RewrittenSite],
    unmapped: list[UnmappedSite],
) -> None:
    path, line, col = row.get("path"), row.get("line"), row.get("col")
    caller = str(row.get("qualified_name") or "")
    resolution = row.get("resolution")
    resolution_text = resolution if isinstance(resolution, str) else None
    if (
        not isinstance(path, str)
        or not isinstance(line, int)
        or not isinstance(col, int)
    ):
        unmapped.append(
            UnmappedSite(
                path if isinstance(path, str) else "",
                0,
                0,
                caller,
                cs.SIGNATURE_UNLOCATABLE,
            )
        )
        return
    if resolution_text in _AMBIGUOUS and not allow_heuristic:
        unmapped.append(
            UnmappedSite(
                path,
                line,
                col,
                caller,
                cs.SIGNATURE_GUESSED.format(resolution=resolution_text),
            )
        )
        return
    try:
        source = patcher.source(path)
    except PatcherError:
        unmapped.append(
            UnmappedSite(path, line, col, caller, cs.SIGNATURE_MISSING_FILE)
        )
        return
    language, root = parse_fn(path, source)
    call = _call_at(root, line, col, _site_end(row)) if root is not None else None
    args_node = _find_call_arguments_node(call) if call is not None else None
    if args_node is None:
        unmapped.append(UnmappedSite(path, line, col, caller, cs.SIGNATURE_NO_CALL))
        return
    new_args = _map_arguments(args_node, old_names, specs, language, keyword_only)
    if isinstance(new_args, str):
        unmapped.append(UnmappedSite(path, line, col, caller, new_args))
        return
    before = _text(args_node)
    after = "(" + ", ".join(new_args) + ")"
    if after != before:
        patcher.replace_span(path, (args_node.start_byte, args_node.end_byte), after)
    sites.append(RewrittenSite(path, line, col, caller, resolution_text, before, after))


# --- applying ---------------------------------------------------------------------
