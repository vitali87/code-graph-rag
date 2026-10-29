"""`cgr export`: write a project's graph to a JSON file."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import logs as ls
from .cli_runtime import app_context
from .services.graph_service import MemgraphIngestor
from .types_defs import GraphData


def _write_graph_json(
    ingestor: MemgraphIngestor, output_path: Path, project_names: Sequence[str]
) -> GraphData:
    graph_data: GraphData = ingestor.export_graph_to_dict(project_names)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding=cs.ENCODING_UTF8) as f:
        json.dump(graph_data, f, indent=cs.JSON_INDENT, ensure_ascii=False)

    return graph_data


def export_graph_to_file(
    ingestor: MemgraphIngestor, output: str, project_names: Sequence[str] = ()
) -> bool:
    output_path = Path(output)

    try:
        graph_data = _write_graph_json(ingestor, output_path, project_names)
        metadata = graph_data[cs.KEY_METADATA]
        app_context.console.print(
            cs.UI_GRAPH_EXPORT_SUCCESS.format(path=output_path.absolute())
        )
        app_context.console.print(
            cs.UI_GRAPH_EXPORT_STATS.format(
                nodes=metadata[cs.KEY_TOTAL_NODES],
                relationships=metadata[cs.KEY_TOTAL_RELATIONSHIPS],
            )
        )
        return True

    except Exception as e:
        app_context.console.print(cs.UI_ERR_EXPORT_FAILED.format(error=e))
        # The one line above is the report; a traceback at ERROR was ~200
        # lines of frames and locals for a mistyped path (issue #2410).
        logger.opt(exception=e).debug(ls.EXPORT_ERROR.format(error=e))
        return False
