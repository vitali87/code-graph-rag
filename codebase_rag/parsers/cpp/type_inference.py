from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


class CppTypeInferenceEngine:
    # (H) Maps local variable / parameter names to their bare C++ type name within a
    # (H) function or method body, so the resolver can bind a member-dispatch call
    # (H) (`obj->method()` / `obj.method()`) to the method node on the receiver's
    # (H) class instead of guessing by the bare method name. Bare names only: the
    # (H) resolver turns a name into a class qn via the same _resolve_class_name path
    # (H) the definition pass uses, so pointer/reference/const/template wrappers are
    # (H) stripped here down to the underlying type identifier.
    __slots__ = ()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        decls: list[tuple[str, str]] = []
        if declarator := self._function_declarator(caller_node):
            self._collect_parameters(declarator, decls)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_body_declarations(body, decls)
        # (H) The map is keyed by name only, with no knowledge of a call's lexical
        # (H) position, so it cannot tell an outer `Zeta z` from an inner-block
        # (H) `Alpha z` that shadows it. Rather than pick a write order that is wrong
        # (H) for one of the two scopes, decline to infer any name declared with more
        # (H) than one type: such a call falls back to name-only resolution instead of
        # (H) getting a confidently wrong typed edge. (Same flat-map limitation the Go
        # (H) engine carries; true scoping would need positional call resolution.)
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
        # (H) Map each C++ `typedef X Y;` / `using Y = X;` alias name to its underlying
        # (H) bare type name, so a field/local declared with the alias resolves to the
        # (H) aliased class. Collected across all files into one map (an alias in a
        # (H) header is used in a .cc), keyed by bare name like the flat type maps.
        # (H) Aliases to a primitive/template-arg-only type reduce to no bare name and
        # (H) are skipped (never a first-party method-call receiver).
        for child in root_node.children:
            # (H) An alias inside a function/method/lambda body is local to that body
            # (H) and can never type a cross-file field/local, so skip the body (this
            # (H) also avoids traversing the bulk of the AST -- statements/exprs).
            if child.type in cs.CPP_NESTED_SCOPE_NODE_TYPES:
                continue
            match child.type:
                case cs.CppNodeType.TYPE_DEFINITION:
                    self._record_alias(self._typedef_pair(child), aliases, conflicts)
                case cs.CppNodeType.ALIAS_DECLARATION:
                    self._record_alias(self._using_pair(child), aliases, conflicts)
                case _:
                    # (H) typedef/using appear at file scope and inside namespaces
                    # (H) (and extern "C" blocks), so recurse to reach nested ones.
                    self.collect_type_aliases(child, aliases, conflicts)

    def collect_local_type_aliases(
        self, caller_node: Node
    ) -> dict[str, tuple[str, int]]:
        # (H) Aliases declared INSIDE a caller's body (`void f() { using w =
        # (H) basic_writer; w(1); }`) are exactly what collect_type_aliases
        # (H) skips, so construction-site resolution collects them per caller.
        # (H) Each entry carries the declaration's end byte: C++ name lookup is
        # (H) declaration-ordered, so a call BEFORE the alias must not bind to
        # (H) it. Conflicting redefinitions (a nested lambda re-aliasing the
        # (H) name) drop, mirroring the cross-file map's conflict rule.
        aliases: dict[str, tuple[str, int]] = {}
        conflicts: set[str] = set()
        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            match node.type:
                case cs.CppNodeType.TYPE_DEFINITION:
                    pair = self._typedef_pair(node)
                case cs.CppNodeType.ALIAS_DECLARATION:
                    pair = self._using_pair(node)
                case _:
                    stack.extend(node.children)
                    continue
            if pair is None:
                continue
            alias_name, underlying = pair
            if alias_name in conflicts:
                continue
            if alias_name in aliases and aliases[alias_name][0] != underlying:
                conflicts.add(alias_name)
                del aliases[alias_name]
                continue
            aliases[alias_name] = (underlying, node.end_byte)
        return aliases

    def _record_alias(
        self,
        pair: tuple[str, str] | None,
        aliases: dict[str, str],
        conflicts: set[str],
    ) -> None:
        # (H) Bare alias names can collide across scopes/files (two namespaces each
        # (H) `using It = ...;` for a different type). Rather than keep whichever was
        # (H) seen first (a confidently-wrong typed edge for the other), drop a name
        # (H) seen with conflicting underlying types so its receivers fall back to
        # (H) name-only resolution. Mirrors build_local_variable_type_map's
        # (H) drop-on-conflict.
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
        # (H) A plain `typedef X Y;` declarator is a bare type_identifier (the new
        # (H) type name); pointer/array/function typedefs wrap it, so fall back to the
        # (H) declarator-unwrap used for variables. Only the bare-name form types a
        # (H) class receiver, so the unwrap returning None for the rest is fine.
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
        # (H) `using Y = X;` wraps X in a type_descriptor whose `type` child is the type.
        if type_node.type == cs.CppNodeType.TYPE_DESCRIPTOR:
            inner = type_node.child_by_field_name(cs.FIELD_TYPE)
            type_node = inner if inner is not None else type_node
        underlying = self._bare_type_name(type_node)
        alias = safe_decode_text(name_node)
        if underlying and alias and alias != underlying:
            return (alias, underlying)
        return None

    def collect_template_param_names(self, caller_node: Node) -> frozenset[str]:
        # (H) Names of the template type parameters in scope at a function/method
        # (H) (`template<typename SAX>` -> {"SAX"}), gathered from the function's own
        # (H) template_declaration wrapper and every enclosing class/struct template. A
        # (H) call receiver typed to one of these has NO concrete type at this site
        # (H) (`SAX* sax`), so the resolver fans it out to all implementers instead of
        # (H) treating it as an external type. Concrete external types (`std::string`) are
        # (H) NOT in this set, so they still suppress the fan-out.
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
        # (H) One name per parameter declaration. An optional param (`typename SAX =
        # (H) Real`) carries its DEFAULT type as a sibling child -- collecting every
        # (H) descendant type_identifier would wrongly pull the concrete default `Real`
        # (H) into the template-param set and fan a real `Real r; r.work()` out. Take the
        # (H) `name` field when present (optional params), else the declaration's own
        # (H) type_identifier (`typename T`, `typename... Ts`). Only genuine TYPE-param
        # (H) declarations are read: a value param (`int N`, `MyEnum E`) or a
        # (H) template-template param names a concrete type, not a stand-in a receiver
        # (H) could be instantiated as, so it must never enter the set.
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
        # (H) Map each data member of a C++ class to its bare type name, so a member
        # (H) call `field_.method()` inside the class's methods can resolve via the
        # (H) field's type. Fields live in the class body (often a header) separate from
        # (H) out-of-line method bodies, so this is captured once at class ingestion and
        # (H) looked up by the enclosing class qn at call resolution.
        field_types: dict[str, str] = {}
        if body := class_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_fields(body, field_types)
        return field_types

    def _collect_fields(self, node: Node, field_types: dict[str, str]) -> None:
        for child in node.children:
            # (H) A nested type / function / lambda opens its own member scope; its
            # (H) declarations are not this class's fields. Preprocessor blocks
            # (H) (`#ifdef ... #endif`) are transparent, so recurse through them to
            # (H) reach fields declared conditionally.
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
            # (H) A member function declaration (`void Lock();`) is also a
            # (H) field_declaration, but its declarator is a function_declarator;
            # (H) only data members are fields.
            if declarator.type == cs.CppNodeType.FUNCTION_DECLARATOR:
                continue
            if (name := self._declarator_name(declarator)) is not None:
                field_types.setdefault(name, type_name)

    def _function_declarator(self, caller_node: Node) -> Node | None:
        # (H) The parameter_list hangs off the (possibly pointer/reference-wrapped)
        # (H) function_declarator in the definition's declarator chain.
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
            # (H) A lambda / nested function / local class body opens its own scope;
            # (H) its declarations are not locals of the enclosing function, so descend
            # (H) no further or an inner `x` would be attributed to the outer `x`.
            if child.type in cs.CPP_NESTED_SCOPE_NODE_TYPES:
                continue
            if child.type == cs.CppNodeType.DECLARATION:
                self._record_declaration(child, decls)
            # (H) Recurse into ordinary nested blocks (if/for/while/try bodies) so a
            # (H) variable declared only in an inner block still resolves; conflicting
            # (H) redecls across scopes are reconciled by the caller (drop-on-conflict).
            self._collect_body_declarations(child, decls)

    def _record_declaration(self, node: Node, decls: list[tuple[str, str]]) -> None:
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None or not (type_name := self._bare_type_name(type_node)):
            return
        # (H) One statement may declare several variables sharing the leading type
        # (H) (`Zeta a, b;`), each its own `declarator` field child; record them all.
        for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR):
            if (name := self._declarator_name(declarator)) is not None:
                decls.append((name, type_name))

    def _bare_type_name(self, type_node: Node) -> str | None:
        match type_node.type:
            case cs.CppNodeType.TYPE_IDENTIFIER:
                return safe_decode_text(type_node)
            case cs.CppNodeType.QUALIFIED_IDENTIFIER:
                # (H) `ns::Foo` -> `Foo`: the resolver maps the bare class name to its
                # (H) namespaced node qn via find_ending_with.
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
        # (H) Unwrap pointer/reference/init declarators down to the bound identifier.
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
            # (H) `reference_declarator` (`T& x`) holds its identifier as a positional
            # (H) child, not under the `declarator` field that pointer/init declarators
            # (H) expose, so the field-based unwrap stalls; descend into the first
            # (H) named declarator-bearing child instead.
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
