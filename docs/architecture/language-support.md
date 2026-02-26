---
description: "Supported programming languages and their feature coverage in Code-Graph-RAG."
---

# Language Support

Code-Graph-RAG uses Tree-sitter for language-agnostic AST parsing with a unified graph schema across all languages.

## Support Matrix

| Language | Status | Extensions | Functions | Classes/Structs | Modules | Package Detection | Additional Features |
|----------|--------|------------|-----------|-----------------|---------|-------------------|---------------------|
| C++ | Fully Supported | .cpp, .h, .hpp, .cc, .cxx, .hxx, .hh, .ixx, .cppm, .ccm | Yes | Yes | Yes | Yes | Constructors, destructors, operator overloading, templates, lambdas, C++20 modules, namespaces |
| Java | Fully Supported | .java | Yes | Yes | Yes | No | Generics, annotations, modern features (records/sealed classes), concurrency, reflection |
| JavaScript | Fully Supported | .js, .jsx | Yes | Yes | Yes | No | ES6 modules, CommonJS, prototype methods, object methods, arrow functions |
| Lua | Fully Supported | .lua | Yes | No | Yes | No | Local/global functions, metatables, closures, coroutines |
| Python | Fully Supported | .py | Yes | Yes | Yes | Yes | Type inference, decorators, nested functions |
| Rust | Fully Supported | .rs | Yes | Yes | Yes | Yes | impl blocks, associated functions |
| TypeScript | Fully Supported | .ts, .tsx | Yes | Yes | Yes | No | Interfaces, type aliases, enums, namespaces, ES6/CommonJS modules |
| C# | In Development | .cs | Yes | Yes | Yes | No | Classes, interfaces, generics (planned) |
| Go | In Development | .go | Yes | Yes | Yes | No | Methods, type declarations |
| PHP | In Development | .php | Yes | Yes | Yes | No | Classes, functions, namespaces |
| Scala | In Development | .scala, .sc | Yes | Yes | Yes | No | Case classes, objects |

## Language-Agnostic Design

All languages share a unified graph schema, meaning queries work the same way regardless of language. You can query across languages in the same knowledge graph when analyzing polyglot repositories.

## Adding New Languages

Code-Graph-RAG makes it easy to add support for any language that has a Tree-sitter grammar. See the [Adding Languages](../advanced/adding-languages.md) guide.

!!! tip
    While you can add languages yourself, we recommend waiting for official full support for optimal parsing quality and comprehensive feature coverage. [Submit a language request](https://github.com/vitali87/code-graph-rag/issues) if you need a specific language supported.
