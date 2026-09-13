"""Writing and reading `Gloss` nodes: agent-authored notes about code.

Stage two of issue #1808. Stage one (#1830) registered the node, its
`ANNOTATES` / `MENTIONS` edges and the wire format; nothing wrote or read one.
This module is the write and the read, over the same fixed-query surface the
deterministic graph tools use, so a note is a pure function of what the agent
said and what the graph holds.

Three decisions from the issue are load-bearing here:

* A note is never written into a source file. Its only copy is the graph.
* Its subject is a definition `graph_query.resolve()` can return, named the
  way every other tool names one. A NAME that resolves to several definitions
  is refused with the candidates rather than bound to the first: a note on
  the wrong function is a false invariant, which is worse than no note.
  A `path:line` LOCATION takes the innermost definition spanning the line,
  which is what "the line is in" means and is how `resolve` orders them.
* The node and its `ANNOTATES` edge are one statement, and the write is
  confirmed by reading the node back. A subject that left the graph between
  resolving and writing therefore reports "not written", not success.

Anchoring beyond the qualified name (the text quote, the graded repair chain
that turns a moved or edited subject into MOVED / STALE / LOST) is the next
stage; every gloss written here is EXACT at the moment it is written.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypedDict

from . import constants as cs
from . import cypher_queries as cq
from . import graph_query
from .graph_query import QueryFn, SymbolRow
from .types_defs import PropertyDict, ResultRow

WriteFn = Callable[[str, PropertyDict | None], None]

_ID_SEPARATOR = "\x00"


class GlossRow(TypedDict):
    qualified_name: str
    kind: str
    status: str
    body: str
    created_by: str
    created_at: str
    commit_sha: str | None
    target_qn: str
    target_hash: str | None
    anchor_state: str
    mentions: list[str]


class GlossRefusal(TypedDict, total=False):
    error: str
    candidates: list[SymbolRow]


class GlossesResult(TypedDict):
    target: SymbolRow
    annotating: list[GlossRow]
    mentioning: list[GlossRow]


def _prefix(project_name: str) -> str:
    return f"{project_name}{cs.SEPARATOR_DOT}"


def gloss_id(target_qn: str, kind: str, body: str) -> str:
    """The node key: deterministic in (subject, kind, body).

    Writing the same note twice therefore MERGEs onto one node instead of
    filing a duplicate; a note that differs in a word is a different gloss,
    and near-duplicate folding is deliberately not attempted here.
    """
    digest = hashlib.sha256(
        _ID_SEPARATOR.join((target_qn, kind, body)).encode(cs.ENCODING_UTF8)
    ).hexdigest()
    return f"{cs.GLOSS_ID_PREFIX}{digest[: cs.GLOSS_ID_HEX_LENGTH]}"


def resolve_one(
    fetch_all: QueryFn, project_name: str, target: str
) -> SymbolRow | GlossRefusal:
    """The single definition `target` names, or why there is not one.

    An exact qualified-name match wins outright, then a unique dotted-suffix
    match (`Store.get`), then a unique match of any kind. A location is an
    exception by design:
    `resolve` orders its rows innermost first, and the innermost is the one
    the line "is in".
    """
    rows = graph_query.resolve(fetch_all, project_name, target)
    if not rows:
        return GlossRefusal(
            error=cs.MCP_GLOSS_TARGET_NOT_FOUND.format(
                target=target, project=project_name
            )
        )
    if graph_query.parse_location(target) is not None:
        return rows[0]
    exact = [row for row in rows if row["qualified_name"] == target]
    if len(exact) == 1:
        return exact[0]
    # `Store.get` is a dotted suffix of exactly one definition even when
    # other `get`s exist; `resolve` returns those too (matched by bare name),
    # so the suffix tier is what makes the documented target form usable.
    dotted = f"{cs.SEPARATOR_DOT}{target}"
    suffix = [row for row in rows if row["qualified_name"].endswith(dotted)]
    if not exact and len(suffix) == 1:
        return suffix[0]
    if not exact and not suffix and len(rows) == 1:
        return rows[0]
    return GlossRefusal(
        error=cs.MCP_GLOSS_TARGET_AMBIGUOUS.format(target=target, count=len(rows)),
        candidates=rows,
    )


def _is_refusal(value: object) -> bool:
    return isinstance(value, dict) and cs.DICT_KEY_ERROR in value


def _split_mentions(mentions: str | None) -> list[str]:
    if not mentions:
        return []
    return [part.strip() for part in mentions.split(cs.CHAR_COMMA) if part.strip()]


def _target_hash(fetch_all: QueryFn, project_name: str, target_qn: str) -> str | None:
    rows = fetch_all(
        cq.CYPHER_GLOSS_TARGET,
        {cs.KEY_QN: target_qn, cs.KEY_PROJECT_PREFIX: _prefix(project_name)},
    )
    value = rows[0].get(cs.KEY_TARGET_HASH) if rows else None
    return value if isinstance(value, str) else None


def _gloss_row(row: ResultRow) -> GlossRow:
    raw_mentions = row.get(cs.KEY_MENTIONS)
    mentions = (
        sorted(str(m) for m in raw_mentions if m is not None)
        if isinstance(raw_mentions, list)
        else []
    )
    return GlossRow(
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME, "")),
        kind=str(row.get(cs.KEY_KIND, "")),
        status=str(row.get(cs.KEY_STATUS, "")),
        body=str(row.get(cs.KEY_BODY, "")),
        created_by=str(row.get(cs.KEY_CREATED_BY, "")),
        created_at=str(row.get(cs.KEY_CREATED_AT, "")),
        commit_sha=_opt_str(row.get(cs.KEY_COMMIT_SHA)),
        target_qn=str(row.get(cs.KEY_TARGET_QN, "")),
        target_hash=_opt_str(row.get(cs.KEY_TARGET_HASH)),
        anchor_state=str(row.get(cs.KEY_ANCHOR_STATE, "")),
        mentions=mentions,
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _sorted(rows: list[ResultRow]) -> list[GlossRow]:
    # Oldest first, then by key: the store's return order is not a contract.
    return sorted(
        (_gloss_row(row) for row in rows),
        key=lambda g: (g["created_at"], g["qualified_name"]),
    )


def write_gloss(
    fetch_all: QueryFn,
    execute_write: WriteFn,
    project_name: str,
    target: str,
    body: str,
    kind: str,
    mentions: str | None = None,
    author: str | None = None,
    commit_sha: str | None = None,
) -> GlossRow | GlossRefusal:
    """Attach a note to the definition `target` names and return it as stored.

    Every input is resolved and validated BEFORE anything is written, so a
    refusal always means the graph is unchanged. `mentions` is a
    comma-separated list of further definitions the note talks about; each
    becomes a `MENTIONS` edge and each is held to the same resolution rule as
    the subject.
    """
    text = body.strip()
    if not text:
        return GlossRefusal(error=cs.MCP_GLOSS_BODY_EMPTY)
    kinds = [k.value for k in cs.GlossKind]
    if kind not in kinds:
        return GlossRefusal(
            error=cs.MCP_GLOSS_KIND_UNKNOWN.format(
                kind=kind, kinds=cs.SEPARATOR_COMMA_SPACE.join(kinds)
            )
        )
    subject = resolve_one(fetch_all, project_name, target)
    if _is_refusal(subject):
        return subject  # type: ignore[return-value]
    subject_row: SymbolRow = subject  # type: ignore[assignment]
    mentioned: list[SymbolRow] = []
    for name in _split_mentions(mentions):
        found = resolve_one(fetch_all, project_name, name)
        if _is_refusal(found):
            refusal: GlossRefusal = found  # type: ignore[assignment]
            refusal[cs.DICT_KEY_ERROR] = cs.MCP_GLOSS_MENTION_REFUSED.format(
                name=name, error=refusal[cs.DICT_KEY_ERROR]
            )
            return refusal
        mentioned.append(found)  # type: ignore[arg-type]

    target_qn = subject_row["qualified_name"]
    key = gloss_id(target_qn, kind, text)
    prefix = _prefix(project_name)
    # A None here unsets the property (Cypher SET with null), so a gloss on a
    # target without a fingerprint, or written outside a checkout, simply
    # lacks that property rather than carrying a placeholder.
    params: PropertyDict = {
        cs.KEY_QN: key,
        cs.KEY_PROJECT_PREFIX: prefix,
        cs.KEY_TARGET_QN: target_qn,
        cs.KEY_KIND: kind,
        cs.KEY_STATUS: cs.GLOSS_STATUS_ACCEPTED,
        cs.KEY_BODY: text,
        cs.KEY_CREATED_BY: (author or "").strip() or cs.GLOSS_DEFAULT_AUTHOR,
        cs.KEY_CREATED_AT: datetime.now(UTC).isoformat(timespec="seconds"),
        cs.KEY_COMMIT_SHA: commit_sha or None,
        cs.KEY_TARGET_HASH: _target_hash(fetch_all, project_name, target_qn),
        cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.EXACT.value,
    }
    existed = bool(fetch_all(cq.CYPHER_GLOSS_READ, {cs.KEY_QN: key}))
    execute_write(cq.CYPHER_GLOSS_WRITE, params)
    # Read back before adding mentions: a MATCH on a vanished subject writes
    # nothing and raises nothing, so the node's presence is the only evidence
    # the write landed, and a MENTIONS edge must not be hung on a node that
    # was never created.
    stored = fetch_all(cq.CYPHER_GLOSS_READ, {cs.KEY_QN: key})
    if not stored:
        return GlossRefusal(error=cs.MCP_GLOSS_NOT_WRITTEN.format(target=target))
    try:
        for mention in mentioned:
            execute_write(
                cq.CYPHER_GLOSS_MENTION,
                {
                    cs.KEY_QN: key,
                    cs.KEY_TARGET_QN: mention["qualified_name"],
                    cs.KEY_PROJECT_PREFIX: prefix,
                },
            )
    except Exception:
        # "An error means nothing was written" must stay true: a node this
        # call created is removed before the error surfaces. A note that
        # already existed is not this call's to delete and is left as it was.
        if not existed:
            execute_write(cq.CYPHER_GLOSS_DELETE, {cs.KEY_QN: key})
        raise
    if mentioned:
        stored = fetch_all(cq.CYPHER_GLOSS_READ, {cs.KEY_QN: key})
    return _gloss_row(stored[0])


def glosses_for(
    fetch_all: QueryFn, project_name: str, target: str
) -> GlossesResult | GlossRefusal:
    """Every gloss about the definition `target` names.

    `annotating` are the notes filed ON it; `mentioning` are notes filed on
    something else that refer to it. Keeping them apart is the point of the
    node over a text field. Per-symbol by construction, so retrieval is
    relevance-gated rather than a dump.
    """
    subject = resolve_one(fetch_all, project_name, target)
    if _is_refusal(subject):
        return subject  # type: ignore[return-value]
    subject_row: SymbolRow = subject  # type: ignore[assignment]
    params: PropertyDict = {cs.KEY_QN: subject_row["qualified_name"]}
    return GlossesResult(
        target=subject_row,
        annotating=_sorted(fetch_all(cq.CYPHER_GLOSSES_ANNOTATING, params)),
        mentioning=_sorted(fetch_all(cq.CYPHER_GLOSSES_MENTIONING, params)),
    )
