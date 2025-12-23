# Type Inference Gaps

## Python

### 1. Parse Return Type Annotations

```python
def get_all_users() -> list[User]:  # Not parsed
    ...
```

Tree-sitter: `function_definition` → `return_type` field.

### 2. Extract Element Type from Generics

```python
users = get_all_users()  # type: list[User]
for user in users:       # user should be: User
    user.save()          # resolves to: User.save()
```

Regex: `list[X]` → `X`, `dict[K, V]` → `V`, `Optional[X]` → `X`

---

## TypeScript

### 1. Parse Type Annotations

```typescript
const users: User[] = getUsers();  // Not parsed
function getUsers(): User[] { ... }  // Not parsed
```

Tree-sitter: `variable_declarator` → `type` field, `function_declaration` → `return_type` field.

### 2. Handle For-Of Loops

```typescript
for (const user of users) {  // Not handled
    user.save();
}
```

Tree-sitter: `for_in_statement` with `of` variant.

### 3. Extract Element Type from Array/Generic Types

```typescript
User[]      → User
Array<User> → User
```

---

## JavaScript

### 1. Handle For-Of Loops

```javascript
for (const user of users) {  // Not handled
    user.save();
}
```

Same as TypeScript - no loop variable inference.

---

## Java

### 1. Extract Element Type from Generics

```java
List<User> users = getUsers();
users.get(0).save();  // Doesn't know get(0) returns User
```

Parse `List<User>` → extract `User` for collection method return types.

### 2. Handle `var` Type Inference

```java
var users = getUsers();  // type is List<User>, needs inference
var user = users.get(0); // type is User, needs inference
```

When `var` is used, infer from right-hand side expression.

---

## C++

### No Type Extraction Engine

Currently returns empty `local_var_types`. Need to extract explicit types from declarations.

```cpp
User user = getUser();  // Extract "User" from declaration
user.save();            // Resolve to User::save()

auto user = getUser();  // Needs return type inference (like Java var)
```

Tree-sitter: `declaration` → `type` field, `init_declarator` → `declarator` field.

---

## Rust

### No Type Extraction Engine

Currently returns empty `local_var_types`. Need to extract explicit types from declarations.

```rust
let user: User = get_user();  // Extract "User" from type annotation
user.save();                   // Resolve to User::save()

let user = get_user();  // Needs return type inference
```

Tree-sitter: `let_declaration` → `type` field.

---

## Go

### No Type Extraction Engine

Currently returns empty `local_var_types`. Need to extract explicit types from declarations.

```go
var user User = getUser()  // Extract "User" from declaration
user.Save()                // Resolve to User.Save()

user := getUser()  // Short declaration - needs return type inference
```

Tree-sitter: `var_declaration` → type identifier, `short_var_declaration` needs inference.

---

## Lua

No type annotations exist. Current coverage is sufficient for dynamic language.
