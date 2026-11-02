"""XML project files, read without expanding anything they declare.

cgr reads XML project files (`.csproj`, `.slnx`) out of the repositories it
indexes, and a hostile one can declare entities that expand exponentially
(an XML bomb) or that read a file from disk. Neither is ever expanded: a
document whose DTD declares an entity raises `XmlEntityError`.

The reading is the Rust extension's (rust/cgr_graph_rs/src/formats/xml.rs),
which reports names and attributes as `xml.etree.ElementTree` does.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..constants.embedded_graph import (
    EG_RUST_XML_ENTITY_ERROR,
    EG_RUST_XML_FIND_ALL,
    EG_RUST_XML_SYNTAX_ERROR,
)
from ..embedded_graph.extension import require_rust_extension

__all__ = [
    "XML_CONTENT_ERRORS",
    "XmlEntityError",
    "XmlSyntaxError",
    "find_all",
    "find_all_in",
]

_NATIVE = require_rust_extension()
find_all_in: Callable[[bytes, str], list[dict[str, str]]] = getattr(
    _NATIVE, EG_RUST_XML_FIND_ALL
)
# Not well-formed, or outside what the reader reads (an attribute-list
# declaration, an encoding it does not decode).
XmlSyntaxError: type[ValueError] = getattr(_NATIVE, EG_RUST_XML_SYNTAX_ERROR)
# The DTD declares an entity.
XmlEntityError: type[ValueError] = getattr(_NATIVE, EG_RUST_XML_ENTITY_ERROR)

# What a document's content can raise. A file that cannot be opened raises
# OSError, as `ElementTree.parse` does.
XML_CONTENT_ERRORS = (XmlSyntaxError, XmlEntityError)


def find_all(path: Path, name: str) -> list[dict[str, str]]:
    """The attributes of every element named `name` in the XML file at
    `path`, in document order: `[e.attrib for e in root.iter(name)]`."""
    return find_all_in(path.read_bytes(), name)
