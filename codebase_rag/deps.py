from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from .services import QueryProtocol


@dataclass
class RAGDeps:
    project_root: Path
    ingestor: QueryProtocol
    cypher_generator: Any
    code_retriever: Any
    file_reader: Any
    file_writer: Any
    file_editor: Any
    shell_commander: Any
    directory_lister: Any
    document_analyzer: Any
    console: Console
