# A static factory whose return value flows through a reassigned local
# (`let x = cache.get(...); if (x) return x; x = new C(...); return x`)
# defeated return-type inference, so locals typed from the factory stayed
# untyped and downstream getter reads emitted nothing. Found dogfooding
# fastify: `ContentType.from` (lib/content-type.js) with the `parameters`
# getter read in lib/reply.js. Issue #992.
from __future__ import annotations

from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

CT = """'use strict'
const cache = new Map()
class ContentType {
  #parameters = new Map()
  static from (headerValue) {
    let contentType = cache.get(headerValue)
    if (contentType !== undefined) return contentType
    contentType = new ContentType(headerValue)
    cache.set(headerValue, contentType)
    return contentType
  }
  get parameters () { return this.#parameters }
}
module.exports = ContentType
"""

REPLY = """'use strict'
const ContentType = require('./content-type.js')
function send (header) {
  const contentType = ContentType.from(header)
  return contentType.parameters.has('charset')
}
module.exports = { send }
"""


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _run(tmp_path: Path, files: dict[str, str]) -> _Capture:
    for name, src in files.items():
        (tmp_path / name).write_text(src)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return cap


def test_reassigned_local_factory_return_types_the_receiver(tmp_path: Path) -> None:
    cap = _run(tmp_path, {"content-type.js": CT, "reply.js": REPLY})
    assert any(
        str(frm).endswith(".send")
        and str(to) == "p.content-type.ContentType.parameters"
        for frm, _rel, to in cap.rels
    )


def test_conflicting_constructions_stay_untyped(tmp_path: Path) -> None:
    # Two different classes assigned to the returned local: the return type
    # is unknowable and no getter edge may be guessed.
    files = {
        "content-type.js": """'use strict'
class Other {
  get parameters () { return new Map() }
}
class ContentType {
  static from (v) {
    let x = new Other(v)
    x = new ContentType(v)
    return x
  }
  get parameters () { return new Map() }
}
module.exports = ContentType
""",
        "reply.js": REPLY,
    }
    cap = _run(tmp_path, files)
    # The veto means NO type at all: neither class's getter may be
    # referenced (a no-veto implementation would emit ContentType's).
    assert not any(
        str(frm).endswith(".send") and str(to).endswith(".parameters")
        for frm, _rel, to in cap.rels
    )


def test_factory_building_a_different_class_types_that_class(tmp_path: Path) -> None:
    # The constructed class types the return, never the factory's own class.
    files = {
        "w.js": """'use strict'
class Widget {
  get parameters () { return new Map() }
}
class Factory {
  static build (h) { const w = new Widget(h); return w }
  get parameters () { return new Map() }
}
module.exports = { Factory, Widget }
""",
        "reply.js": """'use strict'
const { Factory } = require('./w.js')
function send (header) {
  const o = Factory.build(header)
  return o.parameters.has('charset')
}
module.exports = { send }
""",
    }
    cap = _run(tmp_path, files)
    assert any(
        str(frm).endswith(".send") and str(to).endswith("Widget.parameters")
        for frm, _rel, to in cap.rels
    )
    assert not any(
        str(frm).endswith(".send") and str(to).endswith("Factory.parameters")
        for frm, _rel, to in cap.rels
    )


def test_nested_closure_local_does_not_veto(tmp_path: Path) -> None:
    # An unrelated same-named local inside a callback is a different
    # variable; it must not veto the outer inference.
    files = {
        "content-type.js": CT.replace(
            "cache.set(headerValue, contentType)",
            "cache.set(headerValue, contentType)\n"
            "    setTimeout(function () { let contentType = new Map(); "
            "return contentType }, 0)",
        ),
        "reply.js": REPLY,
    }
    cap = _run(tmp_path, files)
    assert any(
        str(frm).endswith(".send")
        and str(to) == "p.content-type.ContentType.parameters"
        for frm, _rel, to in cap.rels
    )


def test_nested_closure_construction_does_not_type_outer_return(
    tmp_path: Path,
) -> None:
    # A construction bound to a CALLBACK's local, with the method returning
    # its parameter, must not type the return.
    files = {
        "content-type.js": """'use strict'
function run (cb) { return cb() }
class ContentType {
  static from (x) {
    run(function () { let x = new ContentType('a'); return x.toString() })
    return x
  }
  get parameters () { return new Map() }
}
module.exports = ContentType
""",
        "reply.js": REPLY,
    }
    cap = _run(tmp_path, files)
    assert not any(
        str(frm).endswith(".send")
        and str(to) == "p.content-type.ContentType.parameters"
        for frm, _rel, to in cap.rels
    )


def test_nested_callback_return_does_not_type_the_method(tmp_path: Path) -> None:
    # `return list` where list is an ARRAY built by a mapped callback that
    # returns constructions: the callback's return is not the method's.
    files = {
        "content-type.js": """'use strict'
class Other {
  get parameters () { return new Map() }
}
class ContentType {
  static from (v) {
    const list = v.split(',').map(function (p) { const y = new Other(p); return y })
    return list
  }
  get parameters () { return new Map() }
}
module.exports = ContentType
""",
        "reply.js": REPLY,
    }
    cap = _run(tmp_path, files)
    assert not any(
        str(frm).endswith(".send") and "parameters" in str(to)
        for frm, _rel, to in cap.rels
    )
