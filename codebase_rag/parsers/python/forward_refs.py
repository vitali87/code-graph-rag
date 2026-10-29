"""Read a Python annotation's forward references as the names they quote.

`n: "Node"` annotates `n` exactly as `n: Node` does: a type checker evaluates
the string as the expression it spells (PEP 484 forward references). The
annotation text as written keeps the quotes, and `"Node"` names no class, so
a quoted parameter was left untyped (issue #2837).
"""

from __future__ import annotations

import ast

from ... import constants as cs


class _ForwardRefReader(ast.NodeTransformer):
    def __init__(self) -> None:
        self.changed = False

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if not isinstance(node.value, str):
            return node
        try:
            spelled = ast.parse(node.value.strip(), mode=cs.PY_AST_EVAL_MODE).body
        except (SyntaxError, ValueError):
            # `"a node"`: a string that spells no type stays a string.
            return node
        self.changed = True
        # A forward reference may itself quote one: `"Optional['Node']"`.
        return self.visit(spelled)

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        head = _head_name(node.value)
        if head == cs.PY_TYPING_LITERAL:
            # `Literal["a"]` holds values, not types.
            return node
        if head == cs.PY_TYPING_ANNOTATED and isinstance(node.slice, ast.Tuple):
            # `Annotated[T, metadata]`: only `T` is a type.
            elements = node.slice.elts
            if elements:
                elements[0] = self.visit(elements[0])
            return node
        return self.generic_visit(node)


def _head_name(node: ast.expr) -> str | None:
    # `Literal` and `typing.Literal` alike.
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=name):
            return name
    return None


def unquote_forward_refs(annotation: str) -> str:
    """The annotation with each forward reference read as the name it quotes.

    `"Node"` -> `Node`, `Dict[str, "Node"]` -> `Dict[str, Node]`. Text with
    no forward reference (no quotes, only `Literal` values, a string that is
    not a type expression, or text that does not parse) is returned exactly
    as written, so its spelling, spacing included, never changes.
    """
    if cs.PY_DOUBLE_QUOTE not in annotation and cs.PY_SINGLE_QUOTE not in annotation:
        return annotation
    try:
        tree = ast.parse(annotation.strip(), mode=cs.PY_AST_EVAL_MODE)
    except (SyntaxError, ValueError):
        return annotation
    reader = _ForwardRefReader()
    read = reader.visit(tree)
    return ast.unparse(read) if reader.changed else annotation
