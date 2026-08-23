---
description: "Architecture overview of Code-Graph-RAG's two-component system for codebase analysis."
---

# Architecture Overview

Code-Graph-RAG consists of two main components that work together to analyse and query codebases.

## Components

### 1. Multi-Language Parser

A Tree-sitter based parsing system that analyses codebases and ingests data into the shared knowledge graph, stored in Memgraph (the default) or ArcadeDB — see [Graph Backend Abstraction](graph-backends.md).

- Uses Tree-sitter for robust, language-agnostic AST parsing
- Extracts functions, classes, methods, modules, and their relationships
- Supports 13 programming languages with a unified graph schema (plus Scala in development)
- Handles complex patterns like nested functions, class hierarchies, and cross-module calls

### 2. RAG System (`codebase_rag/`)

An interactive CLI for querying the stored knowledge graph.

- Translates natural language questions into Cypher queries
- Retrieves source code snippets for found elements
- Supports AI-powered code editing with AST-based targeting
- Provides code optimisation with interactive approval workflow

## Data Flow

```
Source Code → Tree-sitter Parser → AST Analysis → Knowledge Graph (Memgraph or ArcadeDB)
                                                          ↓
User Query → AI Model (Cypher Gen) → Cypher Query → Graph Results → Response
```

## Key Dependencies

| Dependency | Purpose |
|-----------|---------|
| `tree-sitter` | Language-agnostic AST parsing |
| `pymgclient` | Memgraph database adapter (default backend) |
| `neo4j` | ArcadeDB database adapter (optional `arcadedb` extra — see [Graph Backend Abstraction](graph-backends.md)) |
| `pydantic-ai` | Agent framework for LLM integration |
| `pydantic-settings` | Settings management |
| `mcp` | Model Context Protocol SDK |
| `typer` | CLI framework |
| `rich` | Terminal rendering |
| `prompt-toolkit` | Interactive command line |
| `diff-match-patch` | Code patching |
| `watchdog` | Filesystem events monitoring |
| `huggingface-hub` | UniXcoder model download |
