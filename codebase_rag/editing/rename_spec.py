"""The site, refusal and report types of `rename` (issue #1532), and the
resolutions its own site collection marks sites with. Split from
`rename.py`; no graph reads or tree-sitter walking here.
"""

from typing import NamedTuple

from .. import constants as cs
from ..function_registry import TYPE_SPACE_KINDS
from .contract import Verdict

IDENTIFIER_RE = r"(?<![\w])%s(?![\w])"

STRUCTURAL = "structural"
SITELESS = "siteless"
# A same-named link of a fluent chain the index has no row for.
CHAIN = "chain"
# An occurrence of the name in code that no graph site accounts for (#2564).
UNPLANNED = "unplanned"
# One that may not even be the symbol: a method call through an object the
# source does not show to be of its class. `allow_heuristic` never rewrites it.
RECEIVER_UNKNOWN = "receiver_unknown"

# The labels of TypeScript's type-only declarations, which may share their
# qualified name with a value (issue #2520).
TYPE_SPACE_LABELS = frozenset(str(kind) for kind in TYPE_SPACE_KINDS)
TS_LANGUAGES = frozenset({cs.SupportedLanguage.TS, cs.SupportedLanguage.TSX})


class RenameSite(NamedTuple):
    """One place the old name is written and must become the new one."""

    kind: cs.RenameSiteKind
    path: str
    line: int
    col: int
    owner: str  # the qualified name the site belongs to
    resolution: str | None


class RenameRefused(ValueError):
    """The rename would rewrite through a guess; nothing was changed."""

    def __init__(
        self,
        message: str,
        ambiguous: list[RenameSite],
        unlocatable: list[str],
        unplanned: list[RenameSite] | None = None,
    ) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous
        self.unlocatable = unlocatable
        self.unplanned = unplanned or []


class RenameReport(NamedTuple):
    qualified_name: str
    old_name: str
    new_name: str
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    sites: tuple[RenameSite, ...]
    ambiguous: tuple[RenameSite, ...]
    unlocatable: tuple[str, ...]
    doc_mentions: tuple[str, ...]
    hierarchy: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None
    # True when a rollback re-ingest failed after the files were restored:
    # the graph may hold a partial picture and must be rebuilt.
    graph_incomplete: bool = False
    undone: bool | None = None
    # Occurrences of the old name the graph has no site for, rewritten as
    # guessed sites under `allow_heuristic` (issue #2564).
    unplanned: tuple[RenameSite, ...] = ()
    # The graph's own symbols for the target's declaration in a `.d.ts`
    # beside its module (`proj.src.util.d.ts.helper`), renamed with it
    # through `unplanned`.
    companions: tuple[str, ...] = ()
