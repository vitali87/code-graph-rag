"""Jupyter notebook (`.ipynb`) reading (issue #2480)."""

from enum import StrEnum

EXT_IPYNB = ".ipynb"

# nbformat 4 keys the reader looks at. Everything else in the file, outputs
# and attachments above all, is skipped without being decoded.
NB_KEY_CELLS = "cells"
NB_KEY_NBFORMAT = "nbformat"
NB_KEY_CELL_TYPE = "cell_type"
NB_KEY_SOURCE = "source"
NB_KEY_METADATA = "metadata"
NB_KEY_LANGUAGE_INFO = "language_info"
NB_KEY_KERNELSPEC = "kernelspec"
NB_KEY_NAME = "name"
NB_KEY_LANGUAGE = "language"
NB_CELL_TYPE_CODE = "code"
# The one nbformat major version the reader lays out, as its JSON text.
NB_FORMAT_VERSION = b"4"

# The kernel languages whose cells are Python. `language_info.name` is what
# the kernel reported when it last ran; `kernelspec.language` is what the
# notebook was created for. A notebook that declares neither is read as
# Python, the language of Jupyter's default kernel.
NB_PYTHON_LANGUAGES: frozenset[str] = frozenset(
    {"python", "python2", "python3", "ipython", "ipython2", "ipython3"}
)

# A line IPython rewrites before Python sees it, when the line starts a
# statement: a line magic (`%timeit f()`, also a `%%cell` magic's own line),
# a shell escape (`!pip install x`, `!!ls`), a help request (`?obj`), or the
# capturing forms `x = !ls` and `x = %sx ls`. The magic name must follow `%`
# directly, as IPython requires, and `!=` is never a shell escape.
NB_MAGIC_LINE_PATTERN = (
    r"^\s*(?:%{1,2}[A-Za-z_]|!(?!=)|\?|[\w.\[\], ]+?=\s*(?:%[A-Za-z_]|!(?!=)))"
)
# A cell magic owns its whole cell and must open it.
NB_CELL_MAGIC_PATTERN = r"^\s*%%([A-Za-z_]\w*)"
# Cell magics that run their body as Python in this kernel; every other one
# (`%%bash`, `%%html`, `%%sql`, `%%writefile`, ...) hands the body to
# something else, so its cell is not Python and is skipped whole.
NB_PYTHON_BODY_CELL_MAGICS: frozenset[str] = frozenset(
    {"time", "timeit", "capture", "prun", "debug", "python", "python3"}
)
# What a magic line becomes: a statement that leaves a block it sits in
# still a block, and adds nothing to the graph.
NB_NEUTRAL_STATEMENT = "pass"


class NotebookSkip(StrEnum):
    """Why a notebook is kept as a plain `File`."""

    MALFORMED = "not a readable nbformat 4 notebook"
    NOT_PYTHON = "its kernel language is not Python"
