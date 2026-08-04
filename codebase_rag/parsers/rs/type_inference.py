from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .utils import tuple_group_inner


class RustTypeInferenceEngine:
    # Maps local names (parameters, `let` bindings, enum-match variant bindings)
    # to their bare Rust type within a function/method body, so the resolver binds
    # a receiver-dispatch call (`cmd.apply()`) to the method on the type instead of
    # the ambiguous name-only trie fallback. Directly knowable types only;
    # call-return bindings (`let x = T::new()`) are typed separately by the unified
    # engine (which has the return-type map).
    __slots__ = ()

    def build_local_variable_type_map(
        self, caller_node: Node, module_qn: str
    ) -> dict[str, str]:
        var_types: dict[str, str] = {}
        self._collect_parameters(caller_node, var_types)
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_bindings(body, var_types)
        return var_types

    def build_field_type_map(self, class_node: Node) -> dict[str, str]:
        # Map a struct's field names to their bare type names (`struct Handler
        # { shutdown: Shutdown }` -> {"shutdown": "Shutdown"}), so a field-hop
        # receiver (`self.shutdown.is_shutdown()`) resolves.
        fields: dict[str, str] = {}
        field_list = class_node.child_by_field_name(cs.FIELD_BODY)
        if field_list is None or field_list.type != cs.TS_RS_FIELD_DECLARATION_LIST:
            return fields
        for decl in field_list.children:
            if decl.type != cs.TS_RS_FIELD_DECLARATION:
                continue
            name_node = decl.child_by_field_name(cs.FIELD_NAME)
            type_node = decl.child_by_field_name(cs.FIELD_TYPE)
            if name_node is None or type_node is None:
                continue
            if (name := safe_decode_text(name_node)) and (
                type_name := self._bare_type_name(type_node)
            ):
                fields[name] = type_name
        return fields

    def build_field_guard_inner_map(self, class_node: Node) -> dict[str, str]:
        # For struct fields whose type is a guard container (`state: Mutex<State>`),
        # record field -> inner type (`state` -> State). The field map keeps the
        # WRAPPER (`Mutex`), so a direct `self.state.is_poisoned()` resolves against
        # it (correct); the inner applies ONLY when a chain reaches a lock/read/borrow
        # guard accessor. Guard containers do not deref-coerce, so this is the only
        # sound place to unwrap.
        inners: dict[str, str] = {}
        field_list = class_node.child_by_field_name(cs.FIELD_BODY)
        if field_list is None or field_list.type != cs.TS_RS_FIELD_DECLARATION_LIST:
            return inners
        for decl in field_list.children:
            if decl.type != cs.TS_RS_FIELD_DECLARATION:
                continue
            name_node = decl.child_by_field_name(cs.FIELD_NAME)
            type_node = decl.child_by_field_name(cs.FIELD_TYPE)
            if name_node is None or type_node is None:
                continue
            name = safe_decode_text(name_node)
            if name and (inner := self._guard_inner_type(type_node)):
                inners[name] = inner
        return inners

    def build_field_element_map(self, class_node: Node) -> dict[str, str]:
        # For struct fields holding a sequence (`workers: Vec<Worker>`),
        # record field -> element type (`workers` -> Worker). The field map
        # keeps the container (`Vec`); the element applies only when an
        # iterator adaptor's closure parameter binds it (issue #1045).
        elements: dict[str, str] = {}
        field_list = class_node.child_by_field_name(cs.FIELD_BODY)
        if field_list is None or field_list.type != cs.TS_RS_FIELD_DECLARATION_LIST:
            return elements
        for decl in field_list.children:
            if decl.type != cs.TS_RS_FIELD_DECLARATION:
                continue
            name_node = decl.child_by_field_name(cs.FIELD_NAME)
            type_node = decl.child_by_field_name(cs.FIELD_TYPE)
            if name_node is None or type_node is None:
                continue
            name = safe_decode_text(name_node)
            if name and (elem := _rust_element_type_name(type_node)):
                elements[name] = elem
        return elements

    def collect_element_entries(
        self, caller_node: Node
    ) -> list[tuple[str, str, int, int, int]]:
        # Collection-typed parameters and annotated lets -> element type
        # (`workers: Vec<Worker>` / `&[Worker]` -> Worker), feeding the
        # iterator-adaptor closure-parameter bindings. Entries are
        # (name, element, scope_start, scope_end, decl_end): a let is in
        # scope only within its enclosing block and only AFTER its own
        # declaration (Rust shadowing is positional), while parameters
        # span the whole body from position zero.
        entries: list[tuple[str, str, int, int, int]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return entries
        body_span = (body.start_byte, body.end_byte)
        self._collect_element_params(caller_node, body_span, entries)
        self._collect_element_lets(body, body_span, entries)
        return entries

    def _collect_element_params(
        self,
        caller_node: Node,
        span: tuple[int, int],
        entries: list[tuple[str, str, int, int, int]],
    ) -> None:
        params = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is None:
            return
        for param in params.children:
            if param.type != cs.TS_RS_PARAMETER:
                continue
            pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            if pattern is None or pattern.type != cs.TS_IDENTIFIER or type_node is None:
                continue
            if (name := safe_decode_text(pattern)) and (
                elem := _rust_element_type_name(type_node)
            ):
                entries.append((name, elem, *span, 0))

    def _collect_element_lets(
        self,
        node: Node,
        scope: tuple[int, int],
        entries: list[tuple[str, str, int, int, int]],
    ) -> None:
        if node.type == cs.TS_RS_FUNCTION_ITEM:
            # A nested fn item shares no scope with the enclosing body:
            # its lets must not leak out (closures DO share attribution
            # scope and keep descending, span-gated by their block).
            return
        if node.type == cs.TS_RS_LET_DECLARATION:
            self._element_let_entry(node, scope, entries)
        child_scope = (
            (node.start_byte, node.end_byte) if node.type == cs.TS_RS_BLOCK else scope
        )
        for child in node.children:
            self._collect_element_lets(child, child_scope, entries)

    def _element_let_entry(
        self,
        node: Node,
        scope: tuple[int, int],
        entries: list[tuple[str, str, int, int, int]],
    ) -> None:
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        if pattern is None or pattern.type != cs.TS_IDENTIFIER:
            return
        name = safe_decode_text(pattern)
        if not name:
            return
        annotation = node.child_by_field_name(cs.FIELD_TYPE)
        elem = _rust_element_type_name(annotation) if annotation is not None else None
        if elem is None and (
            annotation is None or self._inferred_container(annotation)
        ):
            # `let workers: Vec<_> = ...map(|s| Worker { .. }).collect();`
            # leaves the element inferred: the collect chain's mapped
            # struct literal supplies it. A NON-sequence annotation
            # (`Result<Vec<T>, E>`) stands: the bound value is not a
            # sequence of the mapped type.
            elem = self._collected_element_type(
                node.child_by_field_name(cs.FIELD_VALUE)
            )
        if elem:
            entries.append((name, elem, *scope, node.end_byte))

    def _inferred_container(self, annotation: Node) -> bool:
        # `Vec<_>` / `VecDeque<_>`: a sequence annotation whose element is
        # left for inference.
        if annotation.type != cs.TS_GENERIC_TYPE:
            return False
        outer = annotation.child_by_field_name(cs.FIELD_TYPE)
        outer_name = safe_decode_text(outer) if outer else None
        return (
            outer_name in cs.RS_ELEMENT_CONTAINERS
            and _rust_element_type_name(annotation) is None
        )

    def _collected_element_type(self, value: Node | None) -> str | None:
        # Element type of a `<chain>.map(|..| Type { .. })....collect()`
        # value: descend receiver hops from `collect` (plain or turbofish;
        # a typed `collect::<Vec<Worker>>()` answers directly) through
        # element-preserving adaptors to the nearest `map` whose closure
        # returns a struct literal. Any other hop changes or hides the
        # element, so the walk stops undecided.
        if value is None:
            return None
        node = self._unwrap_try(value)
        seen_collect = False
        while node is not None and node.type in cs.RS_CALL_OR_GENERIC_FN:
            step = self._collect_chain_step(node, seen_collect)
            if step is None:
                return None
            elem, seen_collect, node = step
            if elem is not None:
                return elem
        return None

    def _collect_chain_step(
        self, node: Node, seen_collect: bool
    ) -> tuple[str | None, bool, Node | None] | None:
        # One hop of the collect chain: (found element or None, updated
        # collect flag, next receiver). None means the chain left the
        # supported shape entirely.
        func = node.child_by_field_name(cs.FIELD_FUNCTION)
        turbofish = None
        if func is not None and func.type == cs.TS_GENERIC_FUNCTION:
            turbofish = func.child_by_field_name(cs.TS_RS_TYPE_ARGUMENTS)
            func = func.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None or func.type != cs.TS_RS_FIELD_EXPRESSION:
            return None
        field = func.child_by_field_name(cs.FIELD_FIELD)
        field_name = safe_decode_text(field) if field else None
        receiver = func.child_by_field_name(cs.FIELD_VALUE)
        if field_name == cs.RS_ITER_COLLECT and not seen_collect:
            elem = (
                self._turbofish_element_type(turbofish)
                if turbofish is not None
                else None
            )
            return elem, True, receiver
        if seen_collect and field_name == cs.RS_ITER_MAP:
            args = node.child_by_field_name(cs.FIELD_ARGUMENTS)
            return self._closure_struct_literal_type(args), True, None
        if seen_collect and field_name in cs.RS_ITER_NEUTRAL_HOPS:
            return None, True, receiver
        return None

    def _turbofish_element_type(self, type_arguments: Node) -> str | None:
        target = next(
            (
                c
                for c in type_arguments.children
                if c.type in cs.RS_RETURN_TYPE_NODE_TYPES
            ),
            None,
        )
        return _rust_element_type_name(target) if target is not None else None

    def _closure_struct_literal_type(self, args: Node | None) -> str | None:
        closure = (
            next(
                (c for c in args.children if c.type == cs.TS_RS_CLOSURE_EXPRESSION),
                None,
            )
            if args is not None
            else None
        )
        body = (
            closure.child_by_field_name(cs.FIELD_BODY) if closure is not None else None
        )
        if body is not None and body.type == cs.TS_RS_BLOCK:
            # The block's VALUE is its last expression; trailing comments
            # are named nodes and must not hide it.
            named = [
                c for c in body.named_children if c.type not in cs.RS_COMMENT_TYPES
            ]
            body = named[-1] if named else None
        if body is not None and body.type == cs.TS_RS_STRUCT_EXPRESSION:
            struct_name = body.child_by_field_name(cs.FIELD_NAME)
            if struct_name is not None:
                return self._bare_type_name(struct_name)
        return None

    def collect_closure_param_bindings(
        self, caller_node: Node, element_entries: list[tuple[str, str, int, int, int]]
    ) -> list[tuple[int, int, str, str]]:
        # Span-scoped closure-parameter types: (closure_start_byte,
        # closure_end_byte, name, type), overlaid at call sites like match
        # arms. An explicit `|w: Worker|` annotation binds directly; a bare
        # parameter binds when the closure is the argument of an iterator
        # adaptor whose receiver chain reaches a collection of known
        # element type through element-preserving hops (issue #1045).
        # Calls inside closures attribute to the enclosing fn, so the
        # enclosing map is exactly where these bindings belong.
        bindings: list[tuple[int, int, str, str]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return bindings
        closures = self._descendants_of_type(body, cs.TS_RS_CLOSURE_EXPRESSION)
        shadows = self._closure_shadow_spans(closures)
        for closure in closures:
            params = closure.child_by_field_name(cs.FIELD_PARAMETERS)
            if params is None:
                continue
            annotated = [c for c in params.children if c.type == cs.TS_RS_PARAMETER]
            bare = [c for c in params.children if c.type == cs.TS_IDENTIFIER]
            self._annotated_param_bindings(closure, annotated, bindings)
            if len(bare) == 1 and not annotated:
                elem = self._adaptor_element_type(closure, element_entries, shadows)
                if elem and (name := safe_decode_text(bare[0])):
                    bindings.append((closure.start_byte, closure.end_byte, name, elem))
        return bindings

    def _annotated_param_bindings(
        self,
        closure: Node,
        annotated: list[Node],
        bindings: list[tuple[int, int, str, str]],
    ) -> None:
        for param in annotated:
            pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            if (
                pattern is not None
                and pattern.type == cs.TS_IDENTIFIER
                and type_node is not None
                and (name := safe_decode_text(pattern))
                and (type_name := self._bare_type_name(type_node))
            ):
                bindings.append((closure.start_byte, closure.end_byte, name, type_name))

    def _closure_shadow_spans(self, closures: list[Node]) -> list[tuple[str, int, int]]:
        # Every name a closure's parameter list binds shadows same-named
        # outer collections within the closure's span: `|items|` over one
        # collection hides a fn param `items`, so the inner adaptor's
        # receiver is the closure's binding, not the outer entry.
        shadows: list[tuple[str, int, int]] = []
        for closure in closures:
            params = closure.child_by_field_name(cs.FIELD_PARAMETERS)
            if params is None:
                continue
            for name in self._closure_bound_names(params):
                shadows.append((name, closure.start_byte, closure.end_byte))
        return shadows

    def _closure_bound_names(self, params: Node) -> set[str]:
        # All identifiers a closure parameter list binds: bare params,
        # annotated params' pattern side, and pattern contents (`|(i, w)|`).
        names: set[str] = set()
        for child in params.children:
            target: Node | None = child
            if child.type == cs.TS_RS_PARAMETER:
                target = child.child_by_field_name(cs.TS_FIELD_PATTERN)
            if target is None:
                continue
            if target.type == cs.TS_IDENTIFIER:
                if text := safe_decode_text(target):
                    names.add(text)
                continue
            for ident in self._descendants_of_type(target, cs.TS_IDENTIFIER):
                if text := safe_decode_text(ident):
                    names.add(text)
        return names

    def _adaptor_element_type(
        self,
        closure: Node,
        element_entries: list[tuple[str, str, int, int, int]],
        shadows: list[tuple[str, int, int]],
    ) -> str | None:
        receiver = self._adaptor_receiver(closure)
        if receiver is None:
            return None
        segments, pos = receiver
        # Longest known prefix names the collection (locals are one
        # segment, `self.field` two); everything after it must preserve
        # the element or the closure sees a different type.
        for k in range(len(segments), 0, -1):
            key = cs.SEPARATOR_DOT.join(segments[:k])
            elem = self._element_at(key, pos, element_entries, shadows)
            if elem is not None:
                hops = segments[k:]
                if all(hop in cs.RS_ITER_NEUTRAL_HOPS for hop in hops):
                    return elem
                return None
        return None

    def _adaptor_receiver(self, closure: Node) -> tuple[list[str], int] | None:
        # The receiver chain of the iterator adaptor this closure is an
        # argument of, with the adaptor call's position: None when the
        # closure sits anywhere else.
        parent = closure.parent
        if parent is None or parent.type != cs.TS_ARGUMENTS:
            return None
        call = parent.parent
        if call is None or call.type != cs.TS_RS_CALL_EXPRESSION:
            return None
        func = call.child_by_field_name(cs.FIELD_FUNCTION)
        if func is None or func.type != cs.TS_RS_FIELD_EXPRESSION:
            return None
        field = func.child_by_field_name(cs.FIELD_FIELD)
        if not field or safe_decode_text(field) not in cs.RS_ITER_ADAPTORS:
            return None
        value = func.child_by_field_name(cs.FIELD_VALUE)
        segments = self._callee_chain_segments(value) if value is not None else None
        if not segments:
            return None
        return segments, call.start_byte

    def _element_at(
        self,
        key: str,
        pos: int,
        element_entries: list[tuple[str, str, int, int, int]],
        shadows: list[tuple[str, int, int]],
    ) -> str | None:
        # The binding visible for `key` at byte position `pos`: entries
        # whose scope contains the position and whose declaration precedes
        # it; the innermost scope wins, later declarations shadow earlier
        # ones within it. An enclosing closure PARAMETER of the same name
        # hides every entry declared outside that closure.
        best: tuple[int, int, str] | None = None
        for name, elem, start, end, decl_end in element_entries:
            if name != key or not (start <= pos < end) or pos < decl_end:
                continue
            if any(
                sname == key and s0 <= pos < s1 and start < s0
                for sname, s0, s1 in shadows
            ):
                continue
            rank = (end - start, -decl_end)
            if best is None or rank < (best[0], best[1]):
                best = (end - start, -decl_end, elem)
        return best[2] if best is not None else None

    def _guard_inner_type(self, type_node: Node) -> str | None:
        # `Mutex<State>` / `Arc<Mutex<State>>` -> State; None for a non-guard type.
        # A guard wrapped in a deref pointer (`Arc<Mutex<T>>`) still unwraps to its
        # inner, so peel deref pointers first.
        if type_node.type != cs.TS_GENERIC_TYPE:
            return None
        outer = type_node.child_by_field_name(cs.FIELD_TYPE)
        outer_name = safe_decode_text(outer) if outer else None
        args = type_node.child_by_field_name(cs.TS_RS_TYPE_ARGUMENTS)
        inner = (
            next(
                (c for c in args.children if c.type in cs.RS_RETURN_TYPE_NODE_TYPES),
                None,
            )
            if args is not None
            else None
        )
        if inner is None:
            return None
        if outer_name in cs.RS_GUARD_WRAPPERS:
            return self._bare_type_name(inner)
        if outer_name in cs.RS_DEREF_WRAPPERS:
            return self._guard_inner_type(inner)
        return None

    def collect_call_var_bindings(
        self, caller_node: Node
    ) -> list[tuple[str, list[str], int]]:
        # `let x = Type::assoc(...)` / `let x = Type::assoc(...).unwrap()`: pair the
        # bound name with the callee chain segments (base type first, then method
        # hops: `['Command', 'from_frame']`). The unified engine walks the segments
        # through the return-type map to type `x`. Only type-rooted associated-call
        # chains are collected.
        bindings: list[tuple[str, list[str], int]] = []
        if body := caller_node.child_by_field_name(cs.FIELD_BODY):
            self._collect_call_bindings(body, bindings)
        return bindings

    def collect_generic_bounds(self, caller_node: Node) -> dict[str, list[str]]:
        # Trait bounds for every generic type parameter in scope at this
        # caller: the fn's own generics and where clause, then each
        # enclosing impl/trait block's (a method's field receiver takes its
        # bound from the impl header). Innermost declaration wins outright:
        # a fn-level `M` shadows an impl-level `M` entirely, so the outer
        # list is not merged in. Scoped bound spellings keep their path
        # (`crate::x::Matcher`); bare names stay bare.
        bounds: dict[str, list[str]] = {}
        node: Node | None = caller_node
        while node is not None:
            if node.type in cs.RS_GENERIC_SCOPE_ITEMS:
                for name, spellings in self._item_generic_bounds(node).items():
                    bounds.setdefault(name, spellings)
            node = node.parent
        return bounds

    def _item_generic_bounds(self, item: Node) -> dict[str, list[str]]:
        # One item's parameter-list bounds (`<M: Matcher + Clone>`) merged
        # with its where-clause bounds (`where M: Matcher`); a parameter
        # bounded in both places gets both lists.
        merged: dict[str, list[str]] = {}
        if params := item.child_by_field_name(cs.TS_RS_TYPE_PARAMETERS):
            for param in params.children:
                if param.type != cs.TS_RS_TYPE_PARAMETER:
                    continue
                self._merge_bound_entry(
                    merged,
                    param.child_by_field_name(cs.FIELD_NAME),
                    param.child_by_field_name(cs.FIELD_BOUNDS),
                )
        for child in item.children:
            if child.type != cs.TS_RS_WHERE_CLAUSE:
                continue
            for pred in child.children:
                if pred.type != cs.TS_RS_WHERE_PREDICATE:
                    continue
                self._merge_bound_entry(
                    merged,
                    pred.child_by_field_name(cs.FIELD_LEFT),
                    pred.child_by_field_name(cs.FIELD_BOUNDS),
                )
        return merged

    def _merge_bound_entry(
        self,
        merged: dict[str, list[str]],
        name_node: Node | None,
        bounds_node: Node | None,
    ) -> None:
        # Only a plain identifier binds a substitutable parameter name: a
        # where-clause left side like `Self::Item` or `Vec<M>` constrains a
        # projection, not a bare parameter.
        if (
            name_node is None
            or name_node.type != cs.TS_TYPE_IDENTIFIER
            or bounds_node is None
        ):
            return
        name = safe_decode_text(name_node)
        if not name:
            return
        spellings = [
            spelling
            for child in bounds_node.children
            if (spelling := self._bound_spelling(child))
        ]
        if spellings:
            merged.setdefault(name, []).extend(spellings)

    def _bound_spelling(self, node: Node) -> str | None:
        # A bound's usable spelling: a bare trait name as-is, a scoped path
        # whole (the consumer resolves it strictly by module path), a generic
        # bound (`From<NoError>`) by its base path. Lifetimes, `?Sized`
        # markers, and fn-trait bounds yield nothing.
        if node.type == cs.TS_TYPE_IDENTIFIER:
            return safe_decode_text(node)
        if node.type == cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            return safe_decode_text(node)
        if node.type == cs.TS_GENERIC_TYPE:
            inner = node.child_by_field_name(cs.FIELD_TYPE)
            return self._bound_spelling(inner) if inner is not None else None
        return None

    def _collect_parameters(self, caller_node: Node, var_types: dict[str, str]) -> None:
        params = caller_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is None:
            return
        for param in params.children:
            if param.type != cs.TS_RS_PARAMETER:
                continue
            pattern = param.child_by_field_name(cs.TS_FIELD_PATTERN)
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            if pattern is None or pattern.type != cs.TS_IDENTIFIER or type_node is None:
                continue
            if (name := safe_decode_text(pattern)) and (
                type_name := self._bare_type_name(type_node)
            ):
                var_types[name] = type_name

    def _collect_bindings(self, node: Node, var_types: dict[str, str]) -> None:
        # Only `let` bindings go in the flat map. Match-variant bindings are NOT
        # flattened here: a shared name across arms (or a nested match rebinding a
        # param) would clobber the entry with the wrong (last) type. They come
        # per-arm-scoped via collect_match_arm_bindings, overlaid by the resolver at
        # each call's position.
        if node.type == cs.TS_RS_LET_DECLARATION:
            self._collect_let_binding(node, var_types)
        for child in node.children:
            self._collect_bindings(child, var_types)

    def _collect_let_binding(self, node: Node, var_types: dict[str, str]) -> None:
        # `let x: T = ...` (explicit annotation) and `let x = T { .. }` (struct
        # literal) yield a directly-known type. `let x = T::assoc(..)` is left for
        # collect_call_var_bindings (needs the return-type map).
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        if pattern is None or pattern.type != cs.TS_IDENTIFIER:
            return
        name = safe_decode_text(pattern)
        if not name:
            return
        if annotation := node.child_by_field_name(cs.FIELD_TYPE):
            if type_name := self._bare_type_name(annotation):
                var_types[name] = type_name
            return
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if value is not None and value.type == cs.TS_RS_STRUCT_EXPRESSION:
            struct_name = value.child_by_field_name(cs.FIELD_NAME)
            if struct_name and (type_name := self._bare_type_name(struct_name)):
                var_types[name] = type_name

    def _tuple_struct_binding(self, pattern: Node) -> tuple[str, str] | None:
        # `Variant(x)`: bind x to the variant's payload type. Rust's newtype idiom
        # (`Command::Get(Get)`) names the variant after the wrapped type, so the
        # variant name IS the payload type. Only single-field patterns bind.
        variant = pattern.child_by_field_name(cs.FIELD_TYPE)
        if variant is None:
            return None
        variant_name = self._path_leaf_name(variant)
        bound = [
            c for c in pattern.children if c.type == cs.TS_IDENTIFIER and c != variant
        ]
        if variant_name and len(bound) == 1 and (name := safe_decode_text(bound[0])):
            return (name, variant_name)
        return None

    def collect_match_arm_bindings(
        self, caller_node: Node
    ) -> list[tuple[int, int, str, str]]:
        # Per-arm scoped match-variant bindings: (arm_start_byte, arm_end_byte,
        # binding_name, variant_type). Lets the resolver overlay the binding whose
        # arm range contains a call, so each `cmd.apply()` in a distinct arm resolves
        # to its OWN variant type, not the flat map's last-arm one.
        bindings: list[tuple[int, int, str, str]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return bindings
        for arm in self._descendants_of_type(body, cs.TS_RS_MATCH_ARM):
            # Extract only THIS arm's own pattern bindings, not descendants: a
            # nested `match` lives in the arm's value/body and is a separate
            # match_arm (collected in its own iteration with its own range).
            # Scanning descendants would scope a nested arm's binding to the outer
            # arm, mis-overlaying outer-scope calls.
            arm_pattern = arm.child_by_field_name(cs.TS_FIELD_PATTERN)
            if arm_pattern is None:
                continue
            for pattern in self._descendants_of_type(
                arm_pattern, cs.TS_RS_TUPLE_STRUCT_PATTERN
            ):
                if binding := self._tuple_struct_binding(pattern):
                    bindings.append((arm.start_byte, arm.end_byte, *binding))
        return bindings

    def _collect_call_bindings(
        self, node: Node, bindings: list[tuple[str, list[str], int]]
    ) -> None:
        if node.type == cs.TS_RS_LET_DECLARATION:
            self._collect_call_binding(node, bindings)
        for child in node.children:
            self._collect_call_bindings(child, bindings)

    def _collect_call_binding(
        self, node: Node, bindings: list[tuple[str, list[str], int]]
    ) -> None:
        pattern = node.child_by_field_name(cs.TS_FIELD_PATTERN)
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if pattern is None or pattern.type != cs.TS_IDENTIFIER or value is None:
            return
        name = safe_decode_text(pattern)
        if not name:
            return
        value_expr = self._unwrap_try(value)
        if segments := self._callee_chain_segments(value_expr):
            # A single bare identifier is a move or fn-pointer binding
            # (`let f = make;`), not a call: `f` holds the function, not a value of
            # its return type. Only an invoked base counts.
            if len(segments) == 1 and value_expr.type not in cs.RS_CALL_OR_GENERIC_FN:
                return
            # The offset of the CALL, not of the `let`: it is the call whose
            # name a block item shadows, and the two differ when the call sits
            # in a block nested inside the initializer. Every other block-scope
            # probe is handed the call node's own start (issue #1069).
            bindings.append((name, segments, value_expr.start_byte))

    def _callee_chain_segments(self, node: Node) -> list[str] | None:
        # Flatten a Rust value expression into ordered chain segments, base first:
        # `Type::assoc().m()` -> ['Type','assoc','m']; `self.shared.state.lock()
        # .unwrap()` -> ['self','shared','state','lock','unwrap']. Method calls and
        # field accesses are both segments (the resolver disambiguates each hop).
        # Base must be an identifier, `self`, or a scoped `Type::assoc` path;
        # anything else (index, literal) yields None.
        node = self._unwrap_try(node)
        if node.type in cs.RS_CALL_OR_GENERIC_FN:
            # generic_function is turbofish (`f::<T>()`); descend to its callee.
            func = node.child_by_field_name(cs.FIELD_FUNCTION)
            return self._callee_chain_segments(func) if func is not None else None
        if node.type == cs.TS_RS_FIELD_EXPRESSION:
            receiver = node.child_by_field_name(cs.FIELD_VALUE)
            field = node.child_by_field_name(cs.FIELD_FIELD)
            field_name = safe_decode_text(field) if field else None
            if receiver is None or not field_name:
                return None
            if base := self._callee_chain_segments(receiver):
                return [*base, field_name]
            return None
        if node.type == cs.TS_SCOPED_IDENTIFIER:
            path = node.child_by_field_name(cs.TS_RS_FIELD_PATH)
            name = node.child_by_field_name(cs.FIELD_NAME)
            # Keep the FULL base path (`crate::cmd::Command`) so a fully-qualified
            # inline call disambiguates by path in the return-type lookup.
            base = safe_decode_text(path) if path else None
            leaf = safe_decode_text(name) if name else None
            return [base, leaf] if base and leaf else None
        if node.type in cs.RS_IDENT_OR_SELF:
            return [text] if (text := safe_decode_text(node)) else None
        return None

    def _unwrap_try(self, node: Node) -> Node:
        # `expr?` is a try_expression wrapping the real value.
        while node.type == cs.TS_RS_TRY_EXPRESSION and node.child_count:
            node = node.children[0]
        return node

    def _bare_type_name(self, type_node: Node) -> str | None:
        return _rust_bare_type_name(type_node)

    def _path_leaf_name(self, node: Node) -> str | None:
        # Last identifier of a path: `Command` from a bare identifier,
        # `Unknown` from `Command::Unknown`.
        if node.type in cs.RS_IDENTIFIER_TYPES:
            return safe_decode_text(node)
        if node.type in cs.RS_SCOPED_TYPES:
            name = node.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name) if name else None
        return None

    def _descendants_of_type(self, node: Node, node_type: str) -> list[Node]:
        found: list[Node] = []

        def walk(n: Node) -> None:
            if n.type == node_type:
                found.append(n)
            for child in n.children:
                walk(child)

        walk(node)
        return found


def _rust_bare_generic_name(type_node: Node) -> str | None:
    outer = type_node.child_by_field_name(cs.FIELD_TYPE)
    outer_name = _rust_bare_type_name(outer) if outer else None
    # A receiver type strips only transparent deref pointers (Arc<Shared>
    # -> Shared); Option/Result/Vec are kept (a call dispatches to them).
    if outer_name not in cs.RS_DEREF_WRAPPERS:
        return outer_name
    args = type_node.child_by_field_name(cs.TS_RS_TYPE_ARGUMENTS)
    if args is None:
        return outer_name
    inner = next(
        (c for c in args.children if c.type in cs.RS_RETURN_TYPE_NODE_TYPES),
        None,
    )
    return _rust_bare_type_name(inner) if inner else outer_name


def _rust_element_type_name(type_node: Node) -> str | None:
    # Element type of a sequence-shaped type, or None: `Vec<Worker>` /
    # `&[Worker]` / `[Worker; 4]` -> Worker, recursing through references
    # and transparent deref pointers (`Arc<Vec<Worker>>`, `Box<[Worker]>`).
    match type_node.type:
        case cs.TS_RS_REFERENCE_TYPE:
            for child in type_node.children:
                if (
                    child.type in cs.RS_RETURN_TYPE_NODE_TYPES
                    or child.type == cs.TS_RS_ARRAY_TYPE
                ):
                    return _rust_element_type_name(child)
            return None
        case cs.TS_RS_ARRAY_TYPE:
            element = type_node.child_by_field_name(cs.RS_FIELD_ELEMENT)
            return _rust_bare_type_name(element) if element else None
        case cs.TS_GENERIC_TYPE:
            return _rust_generic_element_name(type_node)
        case _:
            return None


def _rust_generic_element_name(type_node: Node) -> str | None:
    outer = type_node.child_by_field_name(cs.FIELD_TYPE)
    outer_name = safe_decode_text(outer) if outer else None
    args = type_node.child_by_field_name(cs.TS_RS_TYPE_ARGUMENTS)
    inner = (
        next(
            (
                c
                for c in args.children
                if c.type in cs.RS_RETURN_TYPE_NODE_TYPES
                or c.type == cs.TS_RS_ARRAY_TYPE
            ),
            None,
        )
        if args is not None
        else None
    )
    if inner is None:
        return None
    if outer_name in cs.RS_ELEMENT_CONTAINERS:
        if inner.type == cs.TS_RS_ARRAY_TYPE:
            return _rust_element_type_name(inner)
        name = _rust_bare_type_name(inner)
        # `Vec<_>` leaves the element inferred: no type, so the caller
        # may derive it from the value expression instead.
        return None if name == cs.CHAR_UNDERSCORE else name
    if outer_name in cs.RS_DEREF_WRAPPERS:
        return _rust_element_type_name(inner)
    return None


def _rust_bare_type_name(type_node: Node) -> str | None:
    # Bare type name, stripping references/generics/wrappers down to the leaf
    # identifier: `&'a mut Shutdown` -> Shutdown, `Result<Command>` -> Command.
    match type_node.type:
        case cs.TS_TYPE_IDENTIFIER | cs.TS_RS_PRIMITIVE_TYPE:
            return safe_decode_text(type_node)
        case cs.TS_GENERIC_TYPE:
            return _rust_bare_generic_name(type_node)
        case cs.TS_RS_SCOPED_TYPE_IDENTIFIER:
            name = type_node.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name) if name else None
        case cs.TS_RS_TUPLE_TYPE:
            inner = tuple_group_inner(type_node)
            return _rust_bare_type_name(inner) if inner else None
        case _:
            # reference_type / dyn / impl / bounded / other wrappers: descend
            # to the first typed child (a bounded type's first bound is the
            # principal trait; the rest are auto-trait markers).
            for child in type_node.children:
                if child.type in cs.RS_RETURN_TYPE_NODE_TYPES:
                    return _rust_bare_type_name(child)
            return None
