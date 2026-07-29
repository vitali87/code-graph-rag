# NestJS-aware dead-code reachability roots (issue #973): a class decorated
# @Injectable/@Controller/@Module is instantiated by the DI container and driven
# by the framework, so its constructor and lifecycle/interface-contract methods
# are roots; a class implementing an external `...OptionsFactory` interface has
# its methods invoked by Nest. Same-named ordinary methods on a plain class must
# NOT be force-rooted.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_MODULE = cs.NodeLabel.MODULE.value
_CLASS = cs.NodeLabel.CLASS.value
_METHOD = cs.NodeLabel.METHOD.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_IMPLEMENTS = cs.RelationshipType.IMPLEMENTS.value

_PATH = "proj/app.module.ts"


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _node(
    label: str, qn: str, name: str, decorators: list[str] | None = None
) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": name,
        "path": _PATH,
        "start_line": 1,
        "end_line": 2,
        "decorators": decorators or [],
        "is_exported": False,
        "overrides_external": False,
    }


def _rel(
    from_label: str, from_qn: str, rel_type: str, to_label: str, to_qn: str
) -> ResultRow:
    return {
        "from_label": from_label,
        "from_qn": from_qn,
        "rel_type": rel_type,
        "to_label": to_label,
        "to_qn": to_qn,
    }


def _dead(
    nodes: list[ResultRow], rels: list[ResultRow], include_classes: bool = False
) -> set[str]:
    config = default_dead_code_config(
        include_tests=True, include_classes=include_classes
    )
    return {
        row["qualified_name"]
        for row in collect_dead_code(FakeIngestor(nodes, rels), "proj", config)
    }


def test_injectable_constructor_and_framework_methods_are_roots() -> None:
    # A DI class: its constructor is container-invoked; `use`/`onModuleInit` are
    # framework-invoked. Only the plain private helper is genuinely dead.
    nodes = [
        _node(_MODULE, "proj.app.module", "app.module"),
        _node(_CLASS, "proj.app.module.Svc", "Svc", ["@Injectable()"]),
        _node(_METHOD, "proj.app.module.Svc.constructor", "constructor"),
        _node(_METHOD, "proj.app.module.Svc.use", "use"),
        _node(_METHOD, "proj.app.module.Svc.onModuleInit", "onModuleInit"),
        _node(_METHOD, "proj.app.module.Svc.helper", "helper"),
    ]
    rels = [
        _rel(_CLASS, "proj.app.module.Svc", _DEFINES_METHOD, _METHOD, m)
        for m in (
            "proj.app.module.Svc.constructor",
            "proj.app.module.Svc.use",
            "proj.app.module.Svc.onModuleInit",
            "proj.app.module.Svc.helper",
        )
    ]
    dead = _dead(nodes, rels)
    assert "proj.app.module.Svc.constructor" not in dead
    assert "proj.app.module.Svc.use" not in dead
    assert "proj.app.module.Svc.onModuleInit" not in dead
    # A non-framework private helper on a DI class is still genuinely dead.
    assert "proj.app.module.Svc.helper" in dead


def test_external_options_factory_methods_are_roots() -> None:
    # A class implementing an EXTERNAL `...OptionsFactory` interface has its
    # methods invoked by Nest.
    nodes = [
        _node(_MODULE, "proj.app.module", "app.module"),
        _node(_CLASS, "proj.app.module.ConfigService", "ConfigService"),
        _node(
            _METHOD,
            "proj.app.module.ConfigService.createTypeOrmOptions",
            "createTypeOrmOptions",
        ),
    ]
    rels = [
        _rel(
            _CLASS,
            "proj.app.module.ConfigService",
            _DEFINES_METHOD,
            _METHOD,
            "proj.app.module.ConfigService.createTypeOrmOptions",
        ),
        _rel(
            _CLASS,
            "proj.app.module.ConfigService",
            _IMPLEMENTS,
            _CLASS,
            "@nestjs.typeorm.TypeOrmOptionsFactory",
        ),
    ]
    dead = _dead(nodes, rels)
    assert "proj.app.module.ConfigService.createTypeOrmOptions" not in dead


def test_dead_code_rels_query_fetches_implements_edges() -> None:
    # The external-interface (`...OptionsFactory`) root rule is fed by IMPLEMENTS
    # edges, so the rel-fetch query must include that relationship type.
    assert cs.RelationshipType.IMPLEMENTS.value in cq.CYPHER_DEAD_CODE_RELS


def test_component_class_node_is_rooted_with_include_classes() -> None:
    # With --classes, the CLASS node is a candidate. A non-exported @Injectable
    # component class is instantiated by the container, so the class node itself
    # must be a root (a rooted constructor cannot revive it: DEFINES_METHOD is
    # not a reachability edge).
    nodes = [
        _node(_MODULE, "proj.app.module", "app.module"),
        _node(_CLASS, "proj.app.module.Svc", "Svc", ["@Injectable()"]),
        _node(_METHOD, "proj.app.module.Svc.constructor", "constructor"),
    ]
    rels = [
        _rel(
            _CLASS,
            "proj.app.module.Svc",
            _DEFINES_METHOD,
            _METHOD,
            "proj.app.module.Svc.constructor",
        )
    ]
    dead = _dead(nodes, rels, include_classes=True)
    assert "proj.app.module.Svc" not in dead


def test_non_nest_external_interface_helper_stays_dead() -> None:
    # Implementing an unrelated third-party interface (NOT a NestJS
    # `...OptionsFactory`) must NOT root the class's private helpers.
    nodes = [
        _node(_MODULE, "proj.app.module", "app.module"),
        _node(_CLASS, "proj.app.module.Widget", "Widget"),
        _node(_METHOD, "proj.app.module.Widget.helper", "helper"),
    ]
    rels = [
        _rel(
            _CLASS,
            "proj.app.module.Widget",
            _DEFINES_METHOD,
            _METHOD,
            "proj.app.module.Widget.helper",
        ),
        _rel(
            _CLASS,
            "proj.app.module.Widget",
            _IMPLEMENTS,
            _CLASS,
            "some.thirdparty.Comparable",
        ),
    ]
    dead = _dead(nodes, rels)
    assert "proj.app.module.Widget.helper" in dead


def test_plain_class_framework_named_method_is_not_rooted() -> None:
    # `use` on a plain (undecorated, non-implementing) class must stay dead:
    # the NestJS name set only roots on a NestJS component class.
    nodes = [
        _node(_MODULE, "proj.app.module", "app.module"),
        _node(_CLASS, "proj.app.module.Plain", "Plain"),
        _node(_METHOD, "proj.app.module.Plain.use", "use"),
    ]
    rels = [
        _rel(
            _CLASS,
            "proj.app.module.Plain",
            _DEFINES_METHOD,
            _METHOD,
            "proj.app.module.Plain.use",
        )
    ]
    dead = _dead(nodes, rels)
    assert "proj.app.module.Plain.use" in dead
