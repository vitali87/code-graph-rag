"""Re-attach a project's Glosses to the definitions their records name once a
sync has rebuilt them, and grade each against its subject (issue #1808)."""

from collections.abc import Mapping
from pathlib import Path

from loguru import logger
from tree_sitter import Parser

from .. import constants as cs
from .. import cypher_queries as cq
from .. import logs as ls
from ..gloss.anchor import ParsedSource, parse_source
from ..gloss.repair import repair_unanchored
from ..graph_query import projects_nested_under
from ..services import QueryProtocol
from ..types_defs import PropertyParams


class GlossReanchor:
    """The re-anchor pass that closes every sync and re-ingest."""

    def __init__(
        self,
        graph: QueryProtocol | None,
        parsers: Mapping[cs.SupportedLanguage, Parser],
        repo_path: Path,
        project_name: str,
    ) -> None:
        self._graph = graph
        self._parsers = parsers
        self._repo_path = repo_path
        self._project_name = project_name

    def reanchor(self) -> None:
        """Re-attach this project's Glosses to the definitions their own records
        name, then grade them.

        A gloss lives only in the graph, so a rebuild that deletes and
        recreates a definition, or an inbound-edge capture that could not be
        read, would otherwise leave the note unattached and the next sweep
        would delete it. The note records its subject and mentions by
        qualified name, and this rebuilds the edges from that record after
        every sync (issue #1808). Never raises: the sync has already landed,
        and an unattached note is recoverable by the next run, so a failure
        here is logged rather than reported as a failed sync.
        """
        graph = self._graph
        if graph is None:
            return
        try:
            # Only this project's definitions changed (issue #3236).
            scope: PropertyParams = {
                cs.KEY_PROJECT_NAME: self._project_name,
                cs.KEY_PROJECT_PREFIX: f"{self._project_name}{cs.SEPARATOR_DOT}",
                cs.KEY_LONGER_PROJECT_PREFIXES: list(
                    projects_nested_under(graph.fetch_all, self._project_name)
                ),
            }
            graph.execute_write(cq.CYPHER_REANCHOR_GLOSSES, scope)
            # A note whose name did not come back is placed by content hash
            # (MOVED) or marked AMBIGUOUS / LOST. Before the mentions restore,
            # so a note that moved gets its MENTIONS edges back this run.
            repair_unanchored(
                graph.fetch_all,
                graph.execute_write,
                self.read_project_source,
                project_name=self._project_name,
            )
            graph.execute_write(cq.CYPHER_REANCHOR_GLOSS_MENTIONS, scope)
            # Then grade: the subject's `anchor_hash` was just re-emitted by
            # the parse, so comparing it with the note's recorded hash here
            # is what makes a note about changed code read STALE.
            graph.execute_write(cq.CYPHER_GRADE_GLOSS_ANCHORS, scope)
        except Exception as error:  # noqa: BLE001 -- see docstring
            logger.warning(ls.GLOSS_REANCHOR_FAILED.format(error=error))

    def read_project_source(self, project_name: str, path: str) -> ParsedSource | None:
        """A file of THIS updater's project, parsed, for the gloss quote tier.

        Another project's definition carries a relative path that may exist
        under this checkout too and would read the wrong file, so a note
        about another project gets no source here (the `source_root_for`
        rule). A path that escapes the root, or cannot be read, is None.
        """
        if project_name != self._project_name:
            return None
        root = self._repo_path.resolve()
        target = (root / path).resolve()
        if root not in (target, *target.parents):
            return None
        try:
            text = target.read_text(encoding=cs.ENCODING_UTF8, errors="replace")
        except OSError:
            return None
        return ParsedSource(text, parse_source(self._parsers, target, text))
