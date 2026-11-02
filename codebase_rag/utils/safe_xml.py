"""XML documents read without expanding anything they declare.

cgr reads XML project files (`.csproj`, `.slnx`) out of the repositories it
indexes, and a hostile one can declare entities that expand exponentially
(an XML bomb) or that read a file from disk. Neither is ever expanded: a
document that declares an entity, or references an external one, raises
`XmlEntityRefusedError`, as defusedxml did before cgr dropped it.

The Rust extension reads plain documents (rust/cgr_graph_rs/src/formats/
xml.rs) and declines every other one, any with a DTD included. This module
is the reference: it reads whatever the extension declines, and everything
where the extension is not installed.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from xml.etree.ElementTree import Element, ParseError, TreeBuilder

from .. import exceptions as ex
from ..constants.embedded_graph import EG_RUST_XML_FIND_ALL
from ..embedded_graph.extension import rust_extension

__all__ = ["XML_CONTENT_ERRORS", "find_all", "find_all_python"]

# What a document's content can raise. A file that cannot be opened raises
# OSError, as `ElementTree.parse` does.
XML_CONTENT_ERRORS = (ParseError, ex.XmlEntityRefusedError)

_ETREE = "xml.etree.ElementTree"
_ETREE_ACCELERATOR = "_elementtree"


def _pure_etree() -> ModuleType:
    # The C parser hides the expat parser the refusals hook into, so this is
    # a private copy of ElementTree loaded without its accelerator (as
    # test.support.import_fresh_module does), and the shared modules are put
    # back untouched. Its errors are made the ones every caller catches.
    shared = sys.modules.pop(_ETREE)
    accelerator = sys.modules.pop(_ETREE_ACCELERATOR, None)
    sys.modules[_ETREE_ACCELERATOR] = None  # type: ignore
    try:
        pure = importlib.import_module(_ETREE)
    finally:
        sys.modules[_ETREE] = shared
        if accelerator is None:
            del sys.modules[_ETREE_ACCELERATOR]
        else:
            sys.modules[_ETREE_ACCELERATOR] = accelerator
        setattr(sys.modules["xml.etree"], "ElementTree", shared)
    pure.ParseError = ParseError
    return pure


def _refuse_entity(name: str, *_: object) -> None:
    raise ex.XmlEntityRefusedError(ex.XML_ENTITY_DECLARED.format(name=name))


def _refuse_external(
    _context: str | None, _base: str | None, system_id: str | None, *_: object
) -> None:
    raise ex.XmlEntityRefusedError(ex.XML_EXTERNAL_ENTITY.format(system_id=system_id))


_PURE_ETREE = _pure_etree()


def _parse(data: bytes) -> Element:
    parser = _PURE_ETREE.XMLParser(target=TreeBuilder())
    expat = parser.parser
    expat.EntityDeclHandler = _refuse_entity
    expat.UnparsedEntityDeclHandler = _refuse_entity
    expat.ExternalEntityRefHandler = _refuse_external
    parser.feed(data)
    return parser.close()


def find_all(path: Path, name: str) -> list[dict[str, str]]:
    """The attributes of every element named `name` in the XML file at
    `path`, in document order: `[e.attrib for e in root.iter(name)]`."""
    data = path.read_bytes()
    native = rust_extension()
    find = getattr(native, EG_RUST_XML_FIND_ALL, None)
    if find is not None and (found := find(data, name)) is not None:
        return found
    return find_all_python(data, name)


def find_all_python(data: bytes, name: str) -> list[dict[str, str]]:
    """`find_all` on a document's bytes, read by the reference parser."""
    return [dict(element.attrib) for element in _parse(data).iter(name)]
