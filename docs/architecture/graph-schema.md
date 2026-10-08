---
description: "Knowledge graph schema with node types, relationships, and language-specific AST mappings."
---

# Graph Schema

The knowledge graph uses a unified schema across all supported languages.

## Node Types

A label marked opt-in belongs to a [capture group](#capture-groups) that a default index leaves out, so it appears only once that group is enabled.

<!-- SECTION:node_schemas -->
| Label | Properties |
|-----|----------|
| Project | `{name: string, root_path: string?}` |
| Package | `{qualified_name: string, name: string, path: string, absolute_path: string}` |
| Folder | `{path: string, name: string, absolute_path: string}` |
| File | `{path: string, name: string, extension: string?, absolute_path: string}` |
| Module | `{qualified_name: string, name: string, path: string, absolute_path: string, docstring: string?, flow_covered: boolean?, generated: boolean?, generator: string?, start_line: int?, end_line: int?, decorators: list[string]?, rust_cfg_test_mods: list[string]?, rust_ungated_mods: list[string]?, front_matter: list[string]?, unresolved_specifiers: list[string]?, unresolved_references: list[string]?}` |
| Class | `{qualified_name: string, name: string, modifiers: list[string], decorators: list[string], path: string, absolute_path: string, start_col: int?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, anchor_hash: string?, namespace: string?}` |
| Function | `{qualified_name: string, name: string, modifiers: list[string], decorators: list[string], path: string, absolute_path: string, start_col: int?, name_start_line: int?, name_start_col: int?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, is_macro: boolean?, is_object_member: boolean?, is_body_scoped_name: boolean?, positional_params: list[string]?, return_type: string?, param_types: list[string]?, ast_fingerprint: string?, ast_fingerprint_nodes: int?, ast_branch_fingerprints: list[string]?, anchor_hash: string?}` |
| Method | `{qualified_name: string, name: string, modifiers: list[string], decorators: list[string], path: string, absolute_path: string, start_col: int?, name_start_line: int?, name_start_col: int?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, is_property: boolean?, overrides_external: boolean?, positional_params: list[string]?, return_type: string?, param_types: list[string]?, signature: string?, declared_in_class: boolean?, ast_fingerprint: string?, ast_fingerprint_nodes: int?, ast_branch_fingerprints: list[string]?, anchor_hash: string?}` |
| Interface | `{qualified_name: string, name: string, path: string, absolute_path: string, modifiers: list[string]?, decorators: list[string]?, start_col: int?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, anchor_hash: string?, namespace: string?}` |
| Enum | `{qualified_name: string, name: string, path: string, absolute_path: string, modifiers: list[string]?, decorators: list[string]?, start_col: int?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, anchor_hash: string?, namespace: string?}` |
| Type | `{qualified_name: string, name: string, path: string?, absolute_path: string?, modifiers: list[string]?, decorators: list[string]?, start_col: int?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, anchor_hash: string?}` |
| Union | `{qualified_name: string, name: string, path: string?, absolute_path: string?, modifiers: list[string]?, decorators: list[string]?, start_col: int?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?, anchor_hash: string?}` |
| ModuleInterface | `{qualified_name: string, name: string, path: string, absolute_path: string, module_type: string}` |
| ModuleImplementation | `{qualified_name: string, name: string, path: string, absolute_path: string, implements_module: string, module_type: string}` |
| ExternalPackage | `{name: string}` |
| ExternalModule | `{qualified_name: string, name: string, path: string}` |
| Resource (opt-in: [`io`](#capture-groups)) | `{qualified_name: string, name: string, kind: string}` |
| Section | `{qualified_name: string, name: string, heading_level: int, start_line: int, end_line: int, path: string, absolute_path: string}` |
| Pattern (opt-in: [`findings`](#capture-groups)) | `{qualified_name: string, name: string, message: string, start_line: int, end_line: int, path: string, snippet: string?}` |
| CodeSmell (opt-in: [`findings`](#capture-groups)) | `{qualified_name: string, name: string, message: string, start_line: int, end_line: int, path: string, snippet: string?}` |
| SecurityIssue (opt-in: [`findings`](#capture-groups)) | `{qualified_name: string, name: string, message: string, start_line: int, end_line: int, path: string, snippet: string?}` |
| Gloss (opt-in: [`glosses`](#capture-groups)) | `{qualified_name: string, kind: string, status: string, body: string, created_by: string, created_at: string, commit_sha: string?, target_qn: string, target_hash: string?, anchor_quote: string?, anchor_prefix: string?, anchor_suffix: string?, anchor_state: string, moved_from: string?, candidate_qns: list[string]?, project: string?, write_id: string?, mention_qns: list[string]?}` |
| Parameter (opt-in: [`parameters`](#capture-groups)) | `{qualified_name: string, name: string, index: int, path: string, absolute_path: string, start_line: int?, start_col: int?, type_name: string?, is_variadic: boolean?, has_default: boolean?}` |
| Field (opt-in: [`fields`](#capture-groups)) | `{qualified_name: string, name: string, path: string, absolute_path: string, start_line: int?, start_col: int?, type_name: string?, modifiers: list[string]?, is_static: boolean?, docstring: string?}` |
| EnumVariant (opt-in: [`enum_variants`](#capture-groups)) | `{qualified_name: string, name: string, path: string, absolute_path: string, start_line: int?, start_col: int?, index: int, value: string?, docstring: string?}` |
| Constant (opt-in: [`constants`](#capture-groups)) | `{qualified_name: string, name: string, path: string, absolute_path: string, start_line: int?, start_col: int?, type_name: string?, value: string?}` |
<!-- /SECTION:node_schemas -->

`ExternalModule` stands for an imported module that lives outside the repository (a third-party or stdlib target of `IMPORTS`, or a positively-external base class target of `INHERITS`/`IMPLEMENTS`).

`Resource` is a synthetic node standing for an external I/O target (a file, environment variable, network endpoint, database, standard stream, socket). Its `qualified_name` has the form `resource::<KIND>::<identity>`, where `identity` is a static string literal when one is available and `<dynamic>` otherwise, and `kind` is one of `FILE`, `NETWORK`, `DATABASE`, `STDIN`, `STDOUT`, `STDERR`, `ENV`, `SOCKET`. Resource nodes are captured only when the `io` capture group is enabled (see below).

`Section` is a heading in a document (Markdown), holding the heading's text, its level (1-6) and its line span. Sections nest through `CONTAINS_SECTION` by heading level, so a subheading hangs off the heading above it rather than off the file; a top-level heading hangs off the document's `Module`. The span covers the heading and the prose beneath it, ending at the line before the next heading at the same or a shallower level (or at end of file), so a parent section's span contains its subsections.

`Pattern`, `CodeSmell`, and `SecurityIssue` are ast-grep finding nodes, captured only when the `findings` capture group is enabled.

## Relationships

Every relationship type belongs to exactly one [capture group](#capture-groups). A relationship marked opt-in belongs to a group that a default index leaves out, so it appears only once that group is enabled.

<!-- SECTION:relationship_schemas -->
| Source | Relationship | Target |
|------|------------|------|
| Project, Package, Folder | CONTAINS_PACKAGE | Package |
| Project, Package, Folder | CONTAINS_FOLDER | Folder |
| Project, Package, Folder | CONTAINS_FILE | File |
| Project, Package, Folder | CONTAINS_MODULE | Module |
| Module, Section | CONTAINS_SECTION | Section |
| Module, Function, Method, Class | DEFINES | Class, Function, Method, Enum, Interface, Type, Union, Module |
| Class, Interface, Enum, Type, Union | DEFINES_METHOD | Method |
| Module | IMPORTS | Module, ExternalModule |
| Module | EXPORTS | Class, Function |
| Module | EXPORTS_MODULE | ModuleInterface |
| Module | IMPLEMENTS_MODULE | ModuleImplementation |
| Class, Interface, Function | INHERITS | Class, Interface, Function, ExternalModule |
| Class, Enum | IMPLEMENTS | Interface, Class, Enum, ExternalModule |
| Method, Function | OVERRIDES | Method |
| Function, Method | RETURNS | Class, Interface, Enum, Type, Union |
| Function, Method | ACCEPTS | Class, Interface, Enum, Type, Union |
| ModuleImplementation | IMPLEMENTS | ModuleInterface |
| Project | DEPENDS_ON_EXTERNAL | ExternalPackage |
| Module | LINKS_TO | File |
| Module, Function, Method | CALLS | Function, Method, Enum, Type |
| Module, Function, Method | REFERENCES | Function, Method, Class |
| Module, Function, Method | INSTANTIATES | Class |
| Module, Function, Method | READS_FROM (opt-in: [`io`](#capture-groups)) | Resource |
| Module, Function, Method | WRITES_TO (opt-in: [`io`](#capture-groups)) | Resource |
| Module, Function, Method, Resource | FLOWS_TO (opt-in: [`io`](#capture-groups)) | Module, Function, Method, Resource |
| Function, Method, File | EXPOSES (opt-in: [`io`](#capture-groups)) | Resource |
| Resource | RESOLVES_TO (opt-in: [`io`](#capture-groups)) | Resource |
| Module | IMPLEMENTS_PATTERN (opt-in: [`findings`](#capture-groups)) | Pattern |
| Module | HAS_SMELL (opt-in: [`findings`](#capture-groups)) | CodeSmell |
| Module | HAS_VULNERABILITY (opt-in: [`findings`](#capture-groups)) | SecurityIssue |
| Gloss | ANNOTATES (opt-in: [`glosses`](#capture-groups)) | Module, Class, Function, Method, Interface, Enum, Type, Union |
| Gloss | MENTIONS (opt-in: [`glosses`](#capture-groups)) | Module, Class, Function, Method, Interface, Enum, Type, Union |
| Function, Method | HAS_PARAMETER (opt-in: [`parameters`](#capture-groups)) | Parameter |
| Class, Interface, Enum, Type, Union | HAS_FIELD (opt-in: [`fields`](#capture-groups)) | Field |
| Enum | HAS_VARIANT (opt-in: [`enum_variants`](#capture-groups)) | EnumVariant |
| Module | DEFINES_CONSTANT (opt-in: [`constants`](#capture-groups)) | Constant |
| Parameter, Field, Constant | OF_TYPE (opt-in: [`parameters`](#capture-groups)) | Class, Interface, Enum, Type, Union |
<!-- /SECTION:relationship_schemas -->

`REFERENCES` records a non-call mention of a callable or class (a function passed as a value, a callback stored in a dict, a Java method reference such as `Acc::add`). `INSTANTIATES` records a class being constructed; a Java constructor reference (`Acc::new`) instantiates its class and references each declared constructor. Both belong to the default `calls` capture group. The findings relationships (`IMPLEMENTS_PATTERN`, `HAS_SMELL`, `HAS_VULNERABILITY`) are opt-in with the `findings` capture group.

### Edge-site properties

`CALLS`, `REFERENCES` and `INSTANTIATES` edges record *where* they were produced, not only that they exist, and `IMPORTS` edges record the statement that produced them (issue #1522):

| Edge types | Property | Meaning |
|---|---|---|
| CALLS, REFERENCES, INSTANTIATES, IMPORTS | `line: int`, `col: int`, `end_line: int`, `end_col: int` | Span of the producing expression (the call, the referencing name, the constructor invocation) or of the import statement. Lines are 1-based, columns 0-based and the end is exclusive, matching node `start_line` / `start_col`. |
| CALLS, REFERENCES, INSTANTIATES | `arg_count: int?`, `kwarg_names: list[string]?` | Present when the site has an argument list: the number of arguments passed (positional plus keyword) and the keyword names in source order. A Python `*rest` or `**opts` unpacking is not counted: it passes an unknown number of arguments, and `star_args` / `star_kwargs` record it instead. A reference site (a bare function value) carries neither. |
| CALLS, REFERENCES, INSTANTIATES | `star_args: boolean?`, `star_kwargs: boolean?` | `true` when the Python argument list unpacks a sequence (`f(*rest)`) or a mapping (`f(**opts)`); absent otherwise (issue #2635). The arguments written beside a `*rest` are a lower bound on the positionals passed, which is how `cgr check` reads them. |
| IMPORTS | `alias: string?` | The name the statement binds in the importing scope: the `as` name when renamed, otherwise the imported symbol or module name. Wildcard imports and Go dot-imports bind no name and carry no alias. |
| IMPORTS | `imported_name: string?` | For symbol-level imports (`from x import y`, `import { y as z }`, `use a::b::y`, Java `import a.b.C`, `const { y } = require(...)`) the symbol's own name; `*` for wildcards; absent for whole-module imports. |
| CALLS, REFERENCES, INSTANTIATES | `resolution: string?` | How the edge was bound (issue #1526): `exact` (scope, import, type or signature), `overload` (one edge per same-named candidate), `heuristic` (name-only: trie suffix, wildcard import, package member), `trace_confirmed` (a static edge a runtime trace observed), `dynamic` (a call only a trace saw). Absent on edges emitted before the label existed; they rank as `exact`. |
| INHERITS, IMPLEMENTS (C#) | `resolution: string?` | `heuristic` when neither the class's namespace, an enclosing namespace, nor a `using` alias or namespace declares the base, and it was bound by a project-wide name match instead (issue #2534). Absent when scope resolved it. |
| CALLS (`dynamic` only) | `dispatch_literal: boolean?`, `unlocatable: boolean?` | `dispatch_literal: true` with `line`/`col` pointing at the `getattr(obj, "name")` argument or the registry-key literal the call went through; `unlocatable: true` when the caller's own body (nested definitions excluded) holds no such literal, or more than one, since two candidates cannot be told apart statically. |
| CALLS, REFERENCES, INSTANTIATES | `spread_args: boolean?` | `true` when a TypeScript, JavaScript, PHP or Go call passes a number of values its written arguments do not show: `f(...xs)`, `f(...$xs)`, `f(xs...)`, a Go call whose lone argument is itself a call (`f(pair())` passes every result of `pair`), or a tagged template, which passes its strings array and one value per substitution. `arg_count` keeps what is written; absent otherwise (issue #2517). `cgr check` gives such a site no definite arity verdict. |
| CALLS, REFERENCES, INSTANTIATES | `call_qualifier: string?` | What a Rust or C# call is written through: the last name of a Rust path (`S` in `S::m(s, 1)`, `Self`, `<S as Trait>`) or of a C# member call's left side when that name binds no local, parameter, field or property at the call (`Util` in `Util.Ext(s, 1)`), and `""` when the left side is a value (every Rust `s.m(1)`; C# `s.Ext(1)` for a parameter `s`, `"x".Ext(1)`, `this.Ext(1)`, `s?.Ext(1)`). Absent for a bare call and in other languages (issue #2517). `cgr check` reads it to count a Rust `self` or C# extension receiver only where the call passes it. |

Sites are stored as **one edge per site**: a function that calls `g` twice has two `CALLS` edges to `g`, one per call expression, and `from x import a, b` yields two `IMPORTS` edges to `x` (same statement span, different `alias`). The site properties join the write-time `MERGE` key (`line`, `col`; plus `alias` for `IMPORTS`), the same mechanism that keeps parallel `FLOWS_TO` edges apart, so re-indexing is idempotent. A query that wants callers rather than call sites should `DISTINCT` on the endpoint; a query that wants the sites reads `r.line`.

Edges emitted without a syntactic site (and trace-written edges without a dispatch literal, those marked `unlocatable`) carry none of these properties and keep collapsing on their endpoints: libclang macro uses and `#include` edges, Roslyn-only facts, inferred C# namespace imports, interprocedural callable-parameter flow edges, and edges written back by dynamic tracing. For a Go grouped `import ( ... )` block the site is the individual spec line, which is the unit an import rewrite edits. `cgr diff` treats these properties as location, not structure: a line shift never reports as a changed relationship.

`CALLS` edges are otherwise created by static analysis with no further properties. [Dynamic call tracing](../guide/dynamic-tracing.md) decorates them with runtime provenance (`dynamic`, `dynamic_call_count`, `dynamic_workloads`, `dynamic_workload_count`, `dynamic_receiver_types`) and creates runtime-only edges flagged `static_missed: true` when no matching static edge existed in the graph at ingest time. Dynamic dispatch, reflection, and registries are the common causes. Ingest also upgrades every observed static edge's `resolution` to `trace_confirmed` in place (on each of its sites) and tags the runtime-only edges `dynamic`; `cgr dead-code --min-resolution` and the `callers`/`callees` tools read the label. An incremental sync that re-parses an endpoint keeps these edges, re-applied by qualified name, and sets `dynamic_stale: true` on the ones whose caller or callee definition changed since the trace was ingested.

### Type facts on definitions

`return_type` and `param_types` (issue #1527) hold a definition's annotations
as written in the source. `return_type` is absent when the definition has no
return annotation, so "unknown" and "annotated as `None`" stay
distinguishable. `param_types` is parallel to the declared parameters in
source order, one entry per parameter and `""` for an unannotated one; it is
absent, not empty, for languages the extractor does not read (Python,
TypeScript/JavaScript, Go, Java, Rust, C# and, return type only, C/C++ are
read). Receivers (`self`, `&self`) count as a parameter with `""`.

`positional_params` lists a Python definition's positional parameter names
as CPython counts them, receiver included (issue #227). TypeScript,
JavaScript, Go, Rust, PHP, Java and C# definitions list every parameter a
call fills, marked with the optionality the signature declares (issue
#2517): `pad?` may be left out, `...rest` takes any number of trailing
arguments, and `self` (Rust) or `this s` (a C# extension method) is a
receiver one call form passes and another does not. The property is absent,
never empty, for every other language and for a bodiless TypeScript
signature, which reads as "kinds unknown". `cgr check` compares the lists to
report [signature changes](structural-delta.md#signatures-outside-python).

The names an annotation mentions are resolved after every file is parsed:
through the module's imports first, then the module and its enclosing
modules, then a unique project type of that name (two equally near
candidates stay unresolved rather than guessed). Each resolved name yields
one `RETURNS` (return annotation) or `ACCEPTS` (any parameter annotation)
edge to the Class / Interface / Enum / Type / Union node. Builtins and
third-party types produce no edge.

## Capture Groups

Which parts of the schema above an index writes is chosen per capture group. Every relationship type belongs to exactly one group; a node label a group owns is written only while at least one of that group's relationships is enabled, and a label no group owns is always written. A default index enables the groups marked ✓. The others are opt-in: until the repository is indexed with one of them, a query for its labels or relationships, such as `MATCH (f)-[:HAS_PARAMETER]->(p)`, returns nothing.

<!-- SECTION:capture_groups -->
| Group | Default | Node labels | Relationships | Description |
|-----|-------|-----------|-------------|-----------|
| `structure` | ✓ | - | CONTAINS_PACKAGE, CONTAINS_FOLDER, CONTAINS_FILE, CONTAINS_MODULE, CONTAINS_SECTION, DEFINES, DEFINES_METHOD | The containment tree from the project down to modules and document sections, and what each module, class or function defines. |
| `calls` | ✓ | - | CALLS, REFERENCES, INSTANTIATES | Call sites, functions and classes used as values, and class instantiations. |
| `types` | ✓ | - | IMPLEMENTS_MODULE, INHERITS, IMPLEMENTS, OVERRIDES, RETURNS, ACCEPTS | Inheritance, interface and module implementation, method overrides, and the project types a signature returns or accepts. |
| `imports` | ✓ | - | IMPORTS, EXPORTS, EXPORTS_MODULE, DEPENDS_ON_EXTERNAL, LINKS_TO | Imports and exports between modules, the project's external package dependencies, and document links to files. |
| `io` | - | Resource | READS_FROM, WRITES_TO, FLOWS_TO, EXPOSES, RESOLVES_TO | External resources code reads, writes or exposes (files, environment variables, network, databases, endpoints), value flow between them, and client calls resolved to the endpoints they reach. |
| `findings` | - | Pattern, CodeSmell, SecurityIssue | IMPLEMENTS_PATTERN, HAS_SMELL, HAS_VULNERABILITY | ast-grep findings on each module: design patterns, code smells and security issues. |
| `glosses` | - | Gloss | ANNOTATES, MENTIONS | Notes agents write about definitions with the annotate MCP tool, rather than anything parsed from source. |
| `parameters` | - | Parameter | HAS_PARAMETER, OF_TYPE | One node per declared parameter of a function or method, and the OF_TYPE edge from a parameter or field to the project type its annotation names. |
| `fields` | - | Field | HAS_FIELD | One node per field of a class, interface, enum, type or union. A field's OF_TYPE edge belongs to parameters, so field types need both. |
| `enum_variants` | - | EnumVariant | HAS_VARIANT | One node per enum member, with its position and value. |
| `constants` | - | Constant | DEFINES_CONSTANT | One node per module-level constant, with its declared type and value. A constant's OF_TYPE edge belongs to parameters, so constant types need both. |
<!-- /SECTION:capture_groups -->

### Choosing Groups

Every indexing run reads the selection from the `CGR_CAPTURE` environment variable, whose tokens are separated by commas, semicolons or spaces. `cgr start --update-graph` and `cgr index` also take `--capture`, repeatable and comma-separated (`--capture none,structure`), applied after `CGR_CAPTURE`. Tokens apply left to right, starting from the default groups:

| Token | Effect |
|-------|--------|
| `GROUP`, `+GROUP` | Add the group. |
| `-GROUP` | Drop the group. |
| `TYPE`, `+TYPE` | Add one relationship type, such as `+HAS_PARAMETER`. |
| `-TYPE` | Drop one relationship type, such as `-OVERRIDES`. |
| `all` | Enable every group. |
| `none` | Disable every group, so the tokens after it build the selection from nothing. |

Names are case-insensitive. A name that is both a group and a relationship type is read as the group, so `-calls` and `-CALLS` both drop `CALLS`, `REFERENCES` and `INSTANTIATES`. Node labels are not tokens: `+Parameter` is not recognised, while the group `parameters` is. A bare group is added to what is already enabled, so naming a default group changes nothing and logs a warning saying so; put `none` first to capture only that group. A `--capture` token that names neither a group nor a relationship type is a usage error, and the command stops before indexing anything. In `CGR_CAPTURE`, which long-running servers also read, such a token is skipped with the warning `Ignoring unknown capture token`, and the rest of the selection still applies.

```bash
# The defaults plus Parameter and Field nodes
cgr start --repo-path . --update-graph --capture parameters --capture fields
# Every group
cgr index --repo-path . -o ./index-out --capture all
# Only the containment tree and definitions
CGR_CAPTURE=none,structure cgr start --repo-path . --update-graph
# The defaults without OVERRIDES edges
cgr start --repo-path . --update-graph --capture -OVERRIDES
```

![cgr start --update-graph --capture parameters --capture fields on pallets/itsdangerous, then cgr stats listing the new Parameter and Field nodes and HAS_PARAMETER, HAS_FIELD and OF_TYPE relationships](../assets/demos/graph-schema-capture.gif)

![CGR_CAPTURE=none,structure cgr start --update-graph on pallets/itsdangerous, then cgr stats showing only the containment tree and DEFINES relationships](../assets/demos/graph-schema-capture-structure.gif)

*Recorded on pallets/itsdangerous.*

The selection is part of the parser fingerprint, so enabling a group on an indexed project needs no `--clean`: the next `--update-graph` re-parses the project once and writes the group's nodes and relationships (see [Document Support](language-support.md#document-support-document-tier)).

## Resource Kinds

A `Resource` node stands for something outside the code that code reads,
writes, exposes or calls. Its `kind` is one of:

| Kind | Stands for |
|------|------------|
| FILE | A file path an I/O call names |
| NETWORK | A URL or host a client call reaches |
| DATABASE | A database, table or query target |
| STDIN | Standard input |
| STDOUT | Standard output |
| STDERR | Standard error |
| ENV | An environment variable |
| SOCKET | A socket address |
| PROCESS | A command run as a subprocess |
| ENDPOINT | A route a handler exposes (`GET /users/{id}`), reached from a NETWORK resource through `RESOLVES_TO` |
| CONTRACT | A codegen contract operation shared by client stubs and server implementations |
| RPC | An RPC method a handler exposes; callers join it directly |
| DISPATCH | A string-keyed dispatch target (a queue name, a command key); callers join it directly, or through `RESOLVES_TO` from a `key/deployment` variant of the key |

`EXPOSES` joins a handler to the ENDPOINT, RPC or DISPATCH resource it
serves. `RESOLVES_TO` joins a client's NETWORK resource to the ENDPOINT its
literal URL matches, a client stub's RPC operation and a server's ENDPOINT
to the CONTRACT they implement, and a `key/deployment` DISPATCH variant to
its head key.

## I/O and Data-Flow Edges

The `io` capture group (opt-in; excluded from the default capture set) adds the relationships that model how code touches external resources and how values move between them: the three below, and `EXPOSES` and `RESOLVES_TO` from [Resource Kinds](#resource-kinds).

`READS_FROM` and `WRITES_TO` connect a callable to a `Resource` it reads from or writes to (for example `os.getenv("K")` reads the `ENV` resource, `print(x)` writes the `STDOUT` resource).

`FLOWS_TO` records value flow, turning provenance questions into graph reachability. It is emitted in three shapes, distinguished by a `kind` edge property:

- **resource → resource** (`kind = resource`): a value read from one resource reaches a write to another within a function body, e.g. `x = os.getenv("K"); print(x)` yields `Resource(ENV::K) -FLOWS_TO-> Resource(STDOUT)`.
- **caller → callee** (`kind = arg`): a tainted local value is passed as an argument to a first-party callee. A `via` edge property names the conduit as `arg:<index>` or `kw:<name>`.
- **callee → caller** (`kind = return`, `via = return`): a callee whose return value is tainted flows that value back to its caller. The edge terminates at the calling function, not at the assignment that received the value.

Taint is propagated through plain `x = y` assignments. `FLOWS_TO` is intentionally conservative in this phase: flow inside a body is tracked by an intra-procedural walk, return taint composes transitively across functions and files, and argument hand-off is one level.

See [I/O and Data-Flow Edges](data-flow-edges.md) for the detailed reference: the taint model, propagation and kill rules, the `kind`/`via` edge properties, scope attribution, and example queries.

## Module Documentation

Every `Module` node carries the documentation for the file as a whole in its
optional `docstring` property, in whatever form the language uses.

The grammars do not distinguish a documentation comment from an ordinary one
-- tree-sitter reports Rust's `//!` and a throwaway `// note` both as
`line_comment` -- so the marker prefix decides, not the node type.

| Language | Marker | Notes |
|----------|--------|-------|
| Python | `"""docstring"""` | A string literal as the first statement |
| Rust | `//!`, `/*!` | Inner docs only; `///` documents the next item, not the module |
| Go | `//` above `package` | No marker: a blank line before `package` makes it a licence header instead |
| Java, Scala | `/**`, `/*!`, `///` | Javadoc/Scaladoc |
| JavaScript, TypeScript, TSX | `/**`, `/*!` | JSDoc. `///` is TypeScript's `<reference/>` directive, not a doc |
| C, C++ | `/**`, `/*!`, `///` | Doxygen |
| C# | `///`, `/**`, `/*!` | XML documentation comments |
| Dart | `///`, `/**`, `/*!` | Library docs |
| PHP | `/**`, `/*!`, `///` | Follows the `<?php` tag |
| Lua | `---` | LuaDoc/LDoc; a plain `--` is an ordinary comment |
| SQL | none | No module-documentation convention, so nothing is extracted |

Consecutive line comments join into one block, and a blank line ends it. A
shebang before the comment is skipped, so a CLI entry point keeps its
documentation.

A comment that does not carry its language's marker is left alone: recording a
licence header or a `// TODO` as the file's documentation is a wrong answer
that reads like a right one. Three further kinds are excluded for the same
reason, even when they do carry the marker:

- **Directives** -- `//go:generate`, `//nolint:`, `// Code generated ... DO NOT
  EDIT.` -- are instructions to tooling. They are skipped rather than treated
  as the end of the comment, so a real doc beneath one is still found.
- **Separator rules** -- `--------`, `////////` -- are decoration, not prose.
- **TypeScript's `/// <reference />`** is machine input, so `///` is not a doc
  marker in JavaScript, TypeScript or TSX; `/**` is.

## Definition Documentation

`Class`, `Function`, `Method`, `Interface`, `Enum`, `Type` and `Union` carry
the documentation of that one definition in the same optional `docstring`
property. Python's is the string literal that opens the body; every other
language's is the doc comment immediately above the declaration -- or above
the statement that wraps it: an `export`, a Go `type`, a `const f = () =>`
assignment, a `module.exports.f = function` assignment. A comment that trails
the previous line (`int a; ///< the a field`) is that line's remark, never the
next declaration's documentation.

The markers are the ones in the module table with one exception: Rust
documents a definition with the **outer** forms, `///` and `/**`, while `//!`
and `/*!` describe the enclosing module and are never attached to an item. Go
has no marker at either level, so `//` directly above a declaration is its doc.

Whether a comment belongs to the file or to the declaration beneath it is one
decision, made once, from the blank line: a doc comment touching a declaration
is that declaration's, a detached one is the file's. So `/** Class docs */`
directly above `class C {}` lands on the `Class` node and not on the `Module`,
and the same comment separated by a blank line does the reverse. Rust is the one
language where a detached `///` belongs to neither -- it documents nothing, and
`rustc` warns on it.

An attribute between the comment and its declaration does not detach it
(`/// doc` / `#[derive(Debug)]` / `struct S`). In the other languages an
annotation is part of the declaration node itself, so the comment is already
adjacent and no skipping is needed.

The exclusions are the module table's -- separator rules, directives, ordinary
comments without the marker -- for the same reason: an `// ordinary note`
recorded as a function's documentation is a wrong answer that reads like a
right one.

## Nested Definitions

A function or class defined inside another function or method (a closure or a function-local class) is attached by `DEFINES` to its **enclosing scope**, not flattened onto the Module. So `DEFINES` can originate from a `Function` or `Method` as well as a `Module`. A top-level function or class is still defined by its `Module`.

A JavaScript or TypeScript named function expression whose value is not stored under that same name (a callback argument such as `app.use(function createError (req, res, next) {...})`, a return value, `var g = function f () {}`) carries `is_body_scoped_name: true`. Its name is in scope inside its own body only, so a bare call by that name resolves to it from there (recursion). Anywhere else the name reaches it only as one `@line` variant of a same-named definition that does bind the name (see Qualified Name Uniqueness below).

Methods of classes defined inside function bodies are captured only when `CGR_CAPTURE_LOCAL_DEFINITIONS` is enabled, which is the default (see [Configuration](../getting-started/configuration.md)); function-local *classes* are always captured, and setting the flag to `false` skips their methods.

## Qualified Name Uniqueness

`qualified_name` uniquely identifies each `Function`, `Method`, and `Class` node. When the same qualified name is defined more than once in a module, every definition is kept as a distinct node. This happens with the `if has_x(): ... else: ...` import-fallback idiom, `typing.overload`, and `try/except ImportError` fallbacks.

The first definition keeps the plain dotted qualified name; each later definition is suffixed with `@<start_line>` (for example `pkg.module.store_embedding@161`) so both survive instead of one overwriting the other. The `name` property stays the plain name on every variant. Source order decides across labels too: a Python `class Tool` followed by a same-named `def Tool` in an `if` block (a docs or `TYPE_CHECKING` shim) keeps `m.Tool` for the class, and the `def` becomes `m.Tool@<line>`.

A `CALLS` edge to a name that has more than one definition links to every variant, since each is a runtime-possible target. When a Python name has both a class and a function variant, a call such as `Tool()` records `INSTANTIATES` to each class variant and `CALLS` to each function variant, and a method call on the result (`Tool().run()`) resolves through the class. A bare decorator `@Tool` runs `Tool(func)` and binds the same way: the module `INSTANTIATES` a class decorator (and `CALLS` its `__init__`), whether or not a same-named function shares its name.

C++ member-function overloads follow the same rule, with one difference: a member is written twice, declared in its class and often defined out of it in another file (`int Sep::g(int a) { ... }`), and both must land on one node. So a C++ `Method` carries a `signature` (its parameter types as written and its `const`/`&`/`&&` qualifiers: `(int)`, `(const std::string&) const`) and the overload is chosen by it. The first overload of a name keeps the plain qualified name, so a member that is not overloaded is named exactly as before; each other overload gets `@<line>` of the place it is first seen, which is its in-class declaration when the class body is parsed (`Sep.g` for `g(int)` declared on line 3 of `sep.h`, `Sep.g@4` for `g(double)` declared on line 4, both defined in `sep.cpp`). The node's `start_line` and `path` are the definition's, so for an out-of-class definition the suffix and `start_line` differ. Parameter names, default arguments, spacing, `struct`/`class`/`typename` keywords, a leading `::` and a by-value parameter's own `const` (`f(const int n)` is `f(int)`) are not part of the signature; namespace qualifiers are, so `f(a::T)` and `f(b::T)` are two overloads. An out-of-class definition spelled differently from its declaration still pairs with it when the spelling settles which: the class declares only one overload of that name, or only one whose types agree up to qualification (`std::string` against `string` under a using-directive, `a::T` against `T` inside namespace `a`), or only one of that arity. Otherwise it is kept as its own node rather than guessed onto one. An `OVERRIDES` edge follows the same rules to the base overload, and a member that settles on none of the base's overloads gets no edge. A lone candidate counts only when its parameter count matches (a declaration's default arguments count as parameters, as its definition counts them), so `f(int, int)` is neither a definition nor an override of a lone `f(int)`. An override needs one more thing: a base overload picked by anything short of a verbatim match counts only if each differing parameter type could be the other through an alias, and the `const`/`&`/`&&` qualifiers agree. Pointers, references, array bounds and the `const` on what a pointer or reference refers to must agree, and only the base type may differ through an alias (`Alias*` may be `int*`, never `int`), unless one side is a bare alias that can stand for the other's whole type (`PtrT` for `const char*`); a by-value parameter's own `const` is ignored. Two different names may be one type only if one of them is not a built-in type word, `nullptr_t` or a class that the member's own class can name from its scope, looked up by namespace path whichever file declares it (`size_t` and the `<cstdint>` names are implementation-defined aliases, so they may be; `std::string` may be only a `basic_string` of `char`, `std::wstring` one of `wchar_t`, and so on; a class of the same name in an unrelated namespace does not count), and one template on both sides is one type only if its arguments may be, by the same rules. `f(double)` beside a lone base `f(int)` hides it and gets no edge, as does `f(std::vector<double>)` beside `f(std::vector<int>)`; `f(Alias)`, `f(unsigned long)` against `f(std::size_t)` and `f(std::vector<Alias>)` against `f(std::vector<int>)` still override. A member whose declaration was read from its class body also carries `declared_in_class: true`; an incremental run reads only those back as declarations, so a member known only from its definition (an in-class declaration returning a pointer or reference is not read from the class body) is re-read with that definition rather than paired with it. The pure `CPP_FRONTEND=libclang` frontend names member overloads the same way, keyed on each overload's canonical declaration, and binds each call to the overload libclang resolved.

A JavaScript or TypeScript function written as an object literal's property value (`{retry: {delay: () => 0}}`, `{delay: function () {}}`, `{delay () {}}`) is named by its key under the enclosing scope, without the object's path, and carries `is_object_member: true`. Only its object reaches it (`options.retry.delay()`), so a bare call such as `delay(5)` never links to it by name, and a bare call to a real `delay` does not fan out onto such a variant. A binding imported from a module that exports the object (`const { delay } = require('./opts')`) still resolves to it.

`Module` nodes are also identified by `qualified_name` (`File` and `Folder` nodes are keyed by `absolute_path` instead, so they stay per-checkout), but without the `@<start_line>` suffix mechanism: bodied modules that share one qualified name (for example mutually-exclusive `#[cfg]` twin `mod` blocks in one Rust file) merge into a single `Module` node whose location properties come from the last definition ingested. This is an accepted representational merge: call resolution is unaffected, because each twin's functions bind through their own module body's imports rather than a merged import map.

## Macros

Macro definitions map onto the existing `Function` label rather than a dedicated node type, since macros are a cross-language concept (C and C++ `#define`, Rust `macro_rules!`). Macro Function nodes carry `is_macro: true`, macro invocations resolve to their definitions and emit `CALLS` edges, and dead-code analysis treats macros like any function.

Language notes:

- **Rust**: macros and functions live in separate namespaces, so a macro invocation (`write!`) never binds a same-named `fn` and a function call never binds a same-named macro. `#[macro_export]` sets `is_exported` (macros take no `pub`).
- **C/C++** (macro semantics, shared by the libclang-backed modes): compiler builtins, system-header macros, and empty-bodied object-like macros (include guards, feature flags) are not nodes. A macro use inside a function body emits `CALLS` from that function; a use outside any function attributes to the `Module`. A macro whose definition body references another macro emits a macro-to-macro `CALLS` edge, since nested expansions are never reported as individual uses.
- **C/C++ hybrid mode** (the default: `CPP_FRONTEND=hybrid`; `libclang` forces the pure libclang frontend and `treesitter` disables libclang entirely; the libclang bindings ship in the `cpp` extra, `pip install "code-graph-rag[cpp]"`): tree-sitter remains the backbone (every file gets its tree-sitter definitions and calls; nothing is skipped) and libclang layers on only macro `Function` nodes and `#include` `IMPORTS` edges, whose qualified names are identical between the two schemes. Macro uses are attributed to the tightest enclosing tree-sitter definition span after the definition pass, so macro `CALLS` edges join the qualified-name scheme the rest of the graph uses.
- **C#**: a namespace that mirrors the file's directory is not repeated in the qualified name: `src/Serilog/Capturing/PropertyBinder.cs` under `namespace Serilog.Capturing` is `proj.src.Serilog.Capturing.PropertyBinder.PropertyBinder`, not `…PropertyBinder.Serilog.Capturing.PropertyBinder`. A namespace the directory does not spell stays in the qualified name, so two same-named types in one file remain distinct. The declared namespace is always on the type node as `namespace`, and `resolve` finds a type by `<namespace>.<name>` through it.
- **C# hybrid mode** (opt-in: the default is `CSHARP_FRONTEND=treesitter`; selecting `auto` uses hybrid mode when `dotnet` is on PATH, while `hybrid`/`roslyn` explicitly request Roslyn-backed analysis; unavailable toolchains fall back to tree-sitter): tree-sitter remains the backbone and a bundled Roslyn tool (requires `dotnet`) layers on location-keyed semantic facts. Base lists get exact `INHERITS`-vs-`IMPLEMENTS` classification; each invocation site gets the compiler's own overload resolution (argument types, not arity) and extension-method binding, overriding the syntactic heuristics per call; `partial` types merge by symbol identity instead of the directory heuristic; and LINQ query-syntax operators that resolve to first-party methods emit `CALLS` edges tree-sitter cannot see (query syntax has no invocation nodes). Source generators run inside the workspace compilation, so resolution through generated members works, but generated code has no repo file and gets no nodes. Any missing fact degrades to the tree-sitter heuristic for that site. See the [security model](security.md#repository-parsing-and-toolchains) before enabling toolchain-backed analysis on untrusted repositories.
- **Java** (default `JAVA_FRONTEND=heuristic`): a call to same-arity overloads is bound by the argument types the parser can see: literals, declared and cast types, and widening up the project's own classes and interfaces or common JDK collection, map and reflection types. Overloads a class inherits compete with the ones it declares. When the argument types cannot tell candidates apart, each tied overload gets a `CALLS` edge labelled `overload` instead of the first declaration getting one labelled `exact`. The same happens when a candidate the parser cannot rule out could beat the pick: a type-variable parameter, or a JDK supertype outside that table (an `IOException` argument beside `f(Throwable)` and `f(Object)`).

## Language-Specific AST Mappings

The function- and class-defining AST node types captured per language (auto-generated from the language specs):

<!-- SECTION:language_mappings -->
- **C**: `enum_specifier`, `function_definition`, `struct_specifier`, `union_specifier`
- **C#**: `class_declaration`, `constructor_declaration`, `conversion_operator_declaration`, `destructor_declaration`, `enum_declaration`, `interface_declaration`, `local_function_statement`, `method_declaration`, `operator_declaration`, `property_declaration`, `record_declaration`, `struct_declaration`
- **C++**: `class_specifier`, `declaration`, `enum_specifier`, `field_declaration`, `function_definition`, `lambda_expression`, `struct_specifier`, `template_declaration`, `union_specifier`
- **Dart**: `class_definition`, `constant_constructor_signature`, `constructor_signature`, `enum_declaration`, `extension_declaration`, `extension_type_declaration`, `factory_constructor_signature`, `function_signature`, `getter_signature`, `mixin_declaration`, `setter_signature`
- **Go**: `function_declaration`, `method_declaration`, `type_alias`, `type_spec`
- **Java**: `annotation_type_declaration`, `class_declaration`, `constructor_declaration`, `enum_declaration`, `interface_declaration`, `method_declaration`, `record_declaration`
- **JavaScript**: `arrow_function`, `class`, `class_declaration`, `function_declaration`, `function_expression`, `generator_function`, `generator_function_declaration`, `method_definition`
- **Lua**: `function_declaration`, `function_definition`
- **PHP**: `anonymous_class`, `anonymous_function`, `arrow_function`, `class_declaration`, `enum_declaration`, `function_definition`, `interface_declaration`, `method_declaration`, `trait_declaration`
- **Python**: `class_definition`, `function_definition`
- **Rust**: `closure_expression`, `enum_item`, `function_item`, `function_signature_item`, `impl_item`, `macro_definition`, `struct_item`, `trait_item`, `type_item`, `union_item`
- **TypeScript (TSX)**: `abstract_class_declaration`, `arrow_function`, `class`, `class_declaration`, `enum_declaration`, `function_declaration`, `function_expression`, `function_signature`, `generator_function`, `generator_function_declaration`, `interface_declaration`, `internal_module`, `method_definition`, `type_alias_declaration`
- **TypeScript**: `abstract_class_declaration`, `arrow_function`, `class`, `class_declaration`, `enum_declaration`, `function_declaration`, `function_expression`, `function_signature`, `generator_function`, `generator_function_declaration`, `interface_declaration`, `internal_module`, `method_definition`, `type_alias_declaration`
- **Scala**: `class_definition`, `function_declaration`, `function_definition`, `object_definition`, `trait_definition`
- **SQL (PostgreSQL)**: `create_function`
<!-- /SECTION:language_mappings -->
