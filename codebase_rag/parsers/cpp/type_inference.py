from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


class CppTypeInferenceEngine:
    # Maps local variable / parameter names to their bare C++ type name in a
    # function body, so the resolver binds a member-dispatch call
    # (`obj->method()` / `obj.method()`) to the method on the receiver's class
    # instead of guessing by bare method name. Bare names only: the resolver
    # turns a name into a class qn via _resolve_class_name, so
    # pointer/reference/const/template wrappers are stripped to the underlying
    # type identifier.
    __slots__ = ()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        decls: list[tuple[str, str]] = []
        if declarator := self._function_declarator(caller_node):
            self._collect_parameters(declarator, decls)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_body_declarations(body, decls)
        # The map is keyed by name only, with no knowledge of a call's lexical
        # position, so it cannot tell an outer `Zeta z` from an inner-block
        # `Alpha z` that shadows it. Rather than pick a write order wrong for
        # one scope, decline to infer any name declared with more than one type:
        # such a call falls back to name-only resolution instead of a
        # confidently wrong typed edge. (Same flat-map limitation the Go engine
        # carries; true scoping would need positional call resolution.)
        var_types: dict[str, str] = {}
        conflicting: set[str] = set()
        for name, type_name in decls:
            if name in conflicting:
                continue
            existing = var_types.get(name)
            if existing is not None and existing != type_name:
                del var_types[name]
                conflicting.add(name)
                continue
            var_types[name] = type_name
        return var_types

    def collect_type_aliases(
        self, root_node: Node, aliases: dict[str, str], conflicts: set[str]
    ) -> None:
        # Map each C++ `typedef X Y;` / `using Y = X;` alias to its underlying
        # bare type name, so a field/local declared with the alias resolves to
        # the aliased class. Collected across all files into one map (a header
        # alias is used in a .cc), keyed by bare name. Aliases to a
        # primitive/template-arg-only type reduce to no bare name and are
        # skipped (never a first-party method-call receiver).
        for child in root_node.children:
            # An alias inside a function/method/lambda body is local to it and
            # can never type a cross-file field/local, so skip the body (this
            # also avoids traversing the bulk of the AST).
            if child.type in cs.CPP_NESTED_SCOPE_NODE_TYPES:
                continue
            match child.type:
                case cs.CppNodeType.TYPE_DEFINITION:
                    self._record_alias(self._typedef_pair(child), aliases, conflicts)
                case cs.CppNodeType.ALIAS_DECLARATION:
                    self._record_alias(self._using_pair(child), aliases, conflicts)
                case _:
                    # typedef/using appear at file scope and inside namespaces
                    # (and extern "C" blocks), so recurse to reach nested ones.
                    self.collect_type_aliases(child, aliases, conflicts)

    def collect_local_type_aliases(
        self, caller_node: Node
    ) -> dict[str, list[tuple[str, int, int]]]:
        # Aliases declared INSIDE a caller's body (`void f() { using w =
        # basic_writer; w(1); }`) are exactly what collect_type_aliases
        # skips, so construction-site resolution collects them per caller.
        # Each entry carries the declaration's end byte and the enclosing
        # block's end byte: C++ name lookup is declaration-ordered AND
        # lexically scoped, so a call before the alias or after its block
        # closes must not bind to it. Same-name entries from different blocks
        # all survive; disjoint windows never compete for one call, and among
        # overlapping windows the binder picks the latest declaration, which is
        # exactly C++ shadowing.
        aliases: dict[str, list[tuple[str, int, int]]] = {}
        stack = [(child, caller_node.end_byte) for child in caller_node.children]
        while stack:
            node, scope_end = stack.pop()
            match node.type:
                case cs.CppNodeType.TYPE_DEFINITION:
                    pair = self._typedef_pair(node)
                case cs.CppNodeType.ALIAS_DECLARATION:
                    pair = self._using_pair(node)
                case _:
                    # blocks and lambdas narrow visibility to their span, as
                    # does a local class/struct body, whose member aliases
                    # never escape it (only its member functions, in the same
                    # span, can use them).
                    if (
                        node.type == cs.CppNodeType.COMPOUND_STATEMENT
                        or node.type in cs.CPP_COMPOUND_TYPES
                    ):
                        scope_end = node.end_byte
                    stack.extend((child, scope_end) for child in node.children)
                    continue
            if pair is None:
                continue
            alias_name, underlying = pair
            aliases.setdefault(alias_name, []).append(
                (underlying, node.end_byte, scope_end)
            )
        return aliases

    def _record_alias(
        self,
        pair: tuple[str, str] | None,
        aliases: dict[str, str],
        conflicts: set[str],
    ) -> None:
        # Bare alias names can collide across scopes/files (two namespaces each
        # `using It = ...;` for a different type). Rather than keep whichever
        # was seen first, drop a name seen with conflicting underlying types so
        # its receivers fall back to name-only resolution. Mirrors
        # build_local_variable_type_map's drop-on-conflict.
        if pair is None:
            return
        alias, underlying = pair
        if alias in conflicts:
            return
        existing = aliases.get(alias)
        if existing is not None and existing != underlying:
            del aliases[alias]
            conflicts.add(alias)
            return
        aliases[alias] = underlying

    def _typedef_pair(self, node: Node) -> tuple[str, str] | None:
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        declarator = node.child_by_field_name(cs.FIELD_DECLARATOR)
        if type_node is None or declarator is None:
            return None
        underlying = self._bare_type_name(type_node)
        # A plain `typedef X Y;` declarator is a bare type_identifier (the new
        # type name); pointer/array/function typedefs wrap it, so fall back to
        # the variable declarator-unwrap. Only the bare-name form types a class
        # receiver, so the unwrap returning None for the rest is fine.
        alias = (
            safe_decode_text(declarator)
            if declarator.type == cs.CppNodeType.TYPE_IDENTIFIER
            else self._declarator_name(declarator)
        )
        if underlying and alias and alias != underlying:
            return (alias, underlying)
        return None

    def _using_pair(self, node: Node) -> tuple[str, str] | None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        if name_node is None or type_node is None:
            return None
        # `using Y = X;` wraps X in a type_descriptor whose `type` child is the type.
        if type_node.type == cs.CppNodeType.TYPE_DESCRIPTOR:
            inner = type_node.child_by_field_name(cs.FIELD_TYPE)
            type_node = inner if inner is not None else type_node
        underlying = self._bare_type_name(type_node)
        alias = safe_decode_text(name_node)
        if underlying and alias and alias != underlying:
            return (alias, underlying)
        return None

    def collect_template_param_names(self, caller_node: Node) -> frozenset[str]:
        # Names of the template type parameters in scope at a function
        # (`template<typename SAX>` -> {"SAX"}), from the function's own
        # template_declaration wrapper and every enclosing class/struct
        # template. A receiver typed to one of these has NO concrete type here
        # (`SAX* sax`), so the resolver fans it out to all implementers.
        # Concrete external types (`std::string`) are NOT in this set, so they
        # still suppress the fan-out.
        names: set[str] = set()
        node: Node | None = caller_node
        while node is not None:
            if node.type == cs.TS_CPP_TEMPLATE_DECLARATION:
                for param_list in node.children:
                    if param_list.type == cs.TS_CPP_TEMPLATE_PARAMETER_LIST:
                        self._collect_type_param_names(param_list, names)
            node = node.parent
        return frozenset(names)

    def _collect_type_param_names(self, param_list: Node, names: set[str]) -> None:
        # One name per parameter declaration. An optional param (`typename SAX =
        # Real`) carries its DEFAULT type as a sibling child, so collecting every
        # descendant type_identifier would wrongly pull the default `Real` into
        # the set and fan a real `Real r; r.work()` out. Take the `name` field
        # when present, else the declaration's own type_identifier (`typename T`,
        # `typename... Ts`). Only genuine TYPE-param declarations are read: a
        # value param (`int N`) or template-template param names a concrete type,
        # not a stand-in a receiver could be instantiated as, so it never enters
        # the set.
        for decl in param_list.named_children:
            if decl.type not in cs.CPP_TYPE_PARAMETER_DECL_TYPES:
                continue
            if (name_node := decl.child_by_field_name(cs.FIELD_NAME)) is not None:
                if name := safe_decode_text(name_node):
                    names.add(name)
                continue
            type_id = next(
                (c for c in decl.children if c.type == cs.TS_TYPE_IDENTIFIER), None
            )
            if type_id and (name := safe_decode_text(type_id)):
                names.add(name)

    def build_field_type_map(self, class_node: Node) -> dict[str, str]:
        # Map each data member of a C++ class to its bare type name, so a member
        # call `field_.method()` in the class's methods resolves via the field's
        # type. Fields live in the class body (often a header) separate from
        # out-of-line method bodies, so this is captured once at ingestion and
        # looked up by enclosing class qn at call resolution.
        field_types: dict[str, str] = {}
        if body := class_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_fields(body, field_types)
        return field_types

    def _collect_fields(self, node: Node, field_types: dict[str, str]) -> None:
        for child in node.children:
            # A nested type / function / lambda opens its own member scope; its
            # declarations are not this class's fields. Preprocessor blocks
            # (`#ifdef ... #endif`) are transparent, so recurse to reach fields
            # declared conditionally.
            if child.type in cs.CPP_NESTED_SCOPE_NODE_TYPES:
                continue
            if child.type == cs.CppNodeType.FIELD_DECLARATION:
                self._record_field(child, field_types)
                continue
            self._collect_fields(child, field_types)

    def _record_field(self, node: Node, field_types: dict[str, str]) -> None:
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or not (type_name := self._bare_type_name(type_node)):
            return
        for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR):
            # A member function declaration (`void Lock();`) is also a
            # field_declaration, but its declarator is a function_declarator;
            # only data members are fields.
            if declarator.type == cs.CppNodeType.FUNCTION_DECLARATOR:
                continue
            if (name := self._declarator_name(declarator)) is not None:
                field_types.setdefault(name, type_name)

    def _function_declarator(self, caller_node: Node) -> Node | None:
        # The parameter_list hangs off the (possibly pointer/reference-wrapped)
        # function_declarator in the definition's declarator chain.
        declarator = caller_node.child_by_field_name(cs.FIELD_DECLARATOR)
        while declarator is not None:
            if declarator.type == cs.CppNodeType.FUNCTION_DECLARATOR:
                return declarator
            declarator = declarator.child_by_field_name(cs.FIELD_DECLARATOR)
        return None

    def _collect_parameters(
        self, declarator: Node, decls: list[tuple[str, str]]
    ) -> None:
        params = declarator.child_by_field_name(cs.KEY_PARAMETERS)
        if params is None:
            return
        for param in params.children:
            if param.type not in (
                cs.CppNodeType.PARAMETER_DECLARATION,
                cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
            ):
                continue
            self._record_declaration(param, decls)

    def _collect_body_declarations(
        self, node: Node, decls: list[tuple[str, str]]
    ) -> None:
        for child in node.children:
            # A lambda / nested function / local class body opens its own scope;
            # its declarations are not locals of the enclosing function, so stop
            # here or an inner `x` would be attributed to the outer `x`.
            if child.type in cs.CPP_NESTED_SCOPE_NODE_TYPES:
                continue
            if child.type == cs.CppNodeType.DECLARATION:
                self._record_declaration(child, decls)
            # Recurse into ordinary nested blocks (if/for/while/try bodies) so a
            # variable declared only in an inner block still resolves; conflicting
            # redecls across scopes are reconciled by the caller (drop-on-conflict).
            self._collect_body_declarations(child, decls)

    def _record_declaration(self, node: Node, decls: list[tuple[str, str]]) -> None:
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or not (type_name := self._bare_type_name(type_node)):
            return
        # One statement may declare several variables sharing the leading type
        # (`Zeta a, b;`), each its own `declarator` field child; record them all.
        for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR):
            if (name := self._declarator_name(declarator)) is not None:
                decls.append((name, type_name))

    def _bare_type_name(self, type_node: Node) -> str | None:
        match type_node.type:
            case cs.CppNodeType.TYPE_IDENTIFIER:
                return safe_decode_text(type_node)
            case cs.CppNodeType.QUALIFIED_IDENTIFIER:
                # `ns::Foo` -> `Foo`: the resolver maps the bare class name to its
                # namespaced node qn via find_ending_with.
                return self._rightmost_name(type_node)
            case cs.CppNodeType.TEMPLATE_TYPE:
                inner = type_node.child_by_field_name(cs.KEY_NAME)
                return self._bare_type_name(inner) if inner is not None else None
            case _:
                return None

    def _rightmost_name(self, node: Node) -> str | None:
        name_node = node.child_by_field_name(cs.KEY_NAME)
        if name_node is not None and name_node.type in (
            cs.CppNodeType.TYPE_IDENTIFIER,
            cs.CppNodeType.IDENTIFIER,
        ):
            return safe_decode_text(name_node)
        text = safe_decode_text(node)
        if not text:
            return None
        return text.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1] or None

    def _declarator_name(self, declarator: Node | None) -> str | None:
        # Unwrap pointer/reference/init declarators down to the bound identifier.
        current = declarator
        while current is not None:
            if current.type in (
                cs.CppNodeType.IDENTIFIER,
                cs.CppNodeType.FIELD_IDENTIFIER,
            ):
                return safe_decode_text(current)
            if inner := current.child_by_field_name(cs.FIELD_DECLARATOR):
                current = inner
                continue
            # `reference_declarator` (`T& x`) holds its identifier as a positional
            # child, not under the `declarator` field pointer/init declarators
            # expose, so the field unwrap stalls; descend into the first named
            # declarator-bearing child instead.
            current = self._first_declarator_child(current)
        return None

    def _first_declarator_child(self, node: Node) -> Node | None:
        for child in node.children:
            if child.type in (
                cs.CppNodeType.IDENTIFIER,
                cs.CppNodeType.FIELD_IDENTIFIER,
                cs.CppNodeType.REFERENCE_DECLARATOR,
                cs.CppNodeType.POINTER_DECLARATOR,
                cs.CppNodeType.INIT_DECLARATOR,
            ):
                return child
        return None
