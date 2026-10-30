"""`class_ingest.base_targets`: which declaration kinds a written base may
bind to, and the C# scope walk that binds one (issue #2534): the class's
namespace and each enclosing one, innermost first, then its `using`s, a
`using` alias with its own type arguments included, with the written arity
choosing between twins (issue #2579)."""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.class_ingest.base_targets import (
    CSharpTypeIndex,
    base_target_kinds,
    resolve_csharp_scoped_base,
)

_CHILD = "proj.Tests.Pong"
_CHILD_DECLARED = "Acme.Tests.Pong"


class _Types:
    """C# declarations by their declared (namespace-qualified) name."""

    def __init__(self) -> None:
        self.registry = FunctionRegistryTrie()
        self.namespaced: dict[str, set[str]] = {}
        self.arity: dict[str, int] = {}
        self.partials: dict[str, list[str]] = {}

    def declare(
        self,
        qn: str,
        declared: str,
        kind: cs.NodeLabel = cs.NodeLabel.CLASS,
        arity: int = 0,
    ) -> None:
        self.registry[qn] = kind
        self.namespaced.setdefault(declared, set()).add(qn)
        if arity:
            self.arity[qn] = arity

    def resolve(
        self,
        written_ref: str,
        usings: dict[str, str] | None = None,
        child_declared: str | None = _CHILD_DECLARED,
    ) -> str | None:
        return resolve_csharp_scoped_base(
            written_ref,
            _CHILD,
            child_declared,
            usings or {},
            CSharpTypeIndex(self.registry, self.namespaced, self.arity, self.partials),
        )


def test_only_js_and_ts_accept_a_function_as_a_base() -> None:
    # An ES5 constructor function is a legal `extends` target there only.
    for language in cs.JS_TS_LANGUAGES:
        assert cs.NodeLabel.FUNCTION in base_target_kinds(language)
    for language in (cs.SupportedLanguage.PYTHON, cs.SupportedLanguage.CSHARP, None):
        kinds = base_target_kinds(language)
        assert cs.NodeLabel.FUNCTION not in kinds
        assert cs.NodeLabel.METHOD not in kinds
        assert {cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE} <= kinds


def test_the_innermost_namespace_declaring_the_name_wins() -> None:
    types = _Types()
    types.declare("proj.Base", "Acme.Base")
    types.declare("proj.Tests.Base", "Acme.Tests.Base")
    assert types.resolve("Base") == "proj.Tests.Base"


def test_a_same_named_member_does_not_answer_for_a_type() -> None:
    types = _Types()
    # `bool NotificationHandler { get; set; }` in the nearer namespace, which
    # the registry holds as a member.
    types.declare(
        "proj.Tests.Holder.NotificationHandler",
        "Acme.Tests.NotificationHandler",
        kind=cs.NodeLabel.METHOD,
    )
    types.declare("proj.NotificationHandler", "Acme.NotificationHandler")
    assert types.resolve("NotificationHandler") == "proj.NotificationHandler"


def test_an_ambiguity_in_the_innermost_scope_is_not_settled_further_out() -> None:
    types = _Types()
    types.declare("proj.a.Base", "Acme.Tests.Base")
    types.declare("proj.b.Base", "Acme.Tests.Base")
    types.declare("proj.Base", "Acme.Base")
    # C# binds the inner scope and reports the clash; it never looks past it.
    assert types.resolve("Base") is None


def test_one_partial_type_split_across_files_is_not_an_ambiguity() -> None:
    types = _Types()
    types.declare("proj.a.Base", "Acme.Base")
    types.declare("proj.b.Base", "Acme.Base")
    group = ["proj.a.Base", "proj.b.Base"]
    types.partials.update({qn: group for qn in group})
    assert types.resolve("Base") == "proj.a.Base"


def test_the_written_arity_picks_between_twins() -> None:
    types = _Types()
    types.declare("proj.PB", "Acme.PB")
    types.declare("proj.generic.PB", "Acme.PB", arity=1)
    assert types.resolve("PB`1") == "proj.generic.PB"
    assert types.resolve("PB") == "proj.PB"


def test_the_class_is_never_its_own_base() -> None:
    types = _Types()
    # `namespace Acme.Tests { class Pong : Pong {} }` names the outer
    # `Acme.Pong`: the class's own scope declares the name only through it.
    types.declare(_CHILD, _CHILD_DECLARED)
    types.declare("proj.Pong", "Acme.Pong")
    assert types.resolve("Pong") == "proj.Pong"


def test_a_name_no_scope_declares_falls_to_the_one_using_that_does() -> None:
    types = _Types()
    types.declare("proj.lib.Handler", "Lib.Handler")
    assert types.resolve("Handler", usings={"Lib": "Lib"}) == "proj.lib.Handler"
    # Nothing declares it in scope or through a using: the caller decides.
    assert types.resolve("Handler") is None


def test_two_usings_declaring_the_name_are_an_ambiguity() -> None:
    types = _Types()
    types.declare("proj.one.Handler", "One.Handler")
    types.declare("proj.two.Handler", "Two.Handler")
    assert types.resolve("Handler", usings={"One": "One", "Two": "Two"}) is None


def test_a_using_alias_carries_its_own_type_arguments() -> None:
    types = _Types()
    types.declare("proj.NotificationHandler", "Acme.NotificationHandler")
    types.declare(
        "proj.generic.NotificationHandler", "Acme.NotificationHandler", arity=1
    )
    # `using NH = Acme.NotificationHandler<string>;` then `class Pong : NH`.
    usings = {"NH": "Acme.NotificationHandler<string>"}
    assert types.resolve("NH", usings=usings) == "proj.generic.NotificationHandler"


def test_a_class_outside_any_namespace_looks_in_the_global_scope() -> None:
    types = _Types()
    types.declare("proj.Base", "Base")
    types.declare("proj.acme.Base", "Acme.Base")
    assert types.resolve("Base", child_declared=None) == "proj.Base"
