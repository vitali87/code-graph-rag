"""Writing an eval report so that no reader ever sees half of one.

Reports land in `evals/results/`, where they are committed: a run killed
mid-write must leave the previous report in place, not a truncated one.
"""

import os
from pathlib import Path

from codebase_rag import constants as cs


def write_text_atomic(path: Path, text: str) -> None:
    # A sibling in the same directory, so the rename never crosses a
    # filesystem; the pid keeps two concurrent runs off each other's file.
    temp = path.with_name(f".{path.name}.{os.getpid()}{cs.TMP_EXTENSION}")
    try:
        temp.write_text(text, encoding=cs.ENCODING_UTF8)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
