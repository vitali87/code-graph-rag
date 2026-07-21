# Type Inference Gaps

Type-extraction engines now exist for Python, TypeScript/JavaScript, Java, C++, Rust, and Go
(`codebase_rag/parsers/*/type_inference.py`). Coverage varies by language: all handle explicit
local declarations; `var`/`auto`/short-declaration inference, generics element types, and
return-type inference are each implemented where that language's engine supports them (for
example C++ resolves `auto` from constructor-shaped initializers but does not generally infer
a variable's type from an arbitrary call's return type). The items below are the known gaps.

## Python

### Parse Return Type Annotations

```python
def get_all_users() -> list[User]:  # Annotation itself not parsed
    ...
```

Return types are currently inferred from `return` statements rather than parsed from the
annotation. Tree-sitter: `function_definition` → `return_type` field.

---

## TypeScript

### Parse Type Annotations

```typescript
const users: User[] = getUsers();  // Not parsed
```

Variable types are inferred from the assigned value (`new` expressions, call return types),
not from explicit annotations. Tree-sitter: `variable_declarator` → `type` field.

### Extract Element Type from Array/Generic Types

```typescript
User[]      → User
Array<User> → User
```

### Handle For-Of Loops

```typescript
for (const user of users) {  // Not handled
    user.save();
}
```

Tree-sitter: `for_in_statement` with `of` variant. No loop-variable element-type inference yet.

---

## JavaScript

### Handle For-Of Loops

```javascript
for (const user of users) {  // Not handled
    user.save();
}
```

Same as TypeScript - no loop variable inference.

---

## Lua

No type annotations exist. Current coverage is sufficient for dynamic language.
