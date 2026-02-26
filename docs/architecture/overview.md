---
description: "Architecture overview of Code-Graph-RAG's two-component system for codebase analysis."
---

# Architecture Overview

Code-Graph-RAG consists of two main components that work together to analyze and query codebases.

## Components

### 1. Multi-Language Parser

A Tree-sitter based parsing system that analyzes codebases and ingests data into Memgraph.

- Uses Tree-sitter for robust, language-agnostic AST parsing
- Extracts functions, classes, methods, modules, and their relationships
- Supports 11 programming languages with a unified graph schema
- Handles complex patterns like nested functions, class hierarchies, and cross-module calls

### 2. RAG System (`codebase_rag/`)

An interactive CLI for querying the stored knowledge graph.

- Translates natural language questions into Cypher queries
- Retrieves source code snippets for found elements
- Supports AI-powered code editing with AST-based targeting
- Provides code optimization with interactive approval workflow

## Data Flow

```
Source Code → Tree-sitter Parser → AST Analysis → Memgraph Knowledge Graph
                                                          ↓
User Query → AI Model (Cypher Gen) → Cypher Query → Graph Results → Response
```

## Key Dependencies

| Dependency | Purpose |
|-----------|---------|
| `tree-sitter` | Language-agnostic AST parsing |
| `pymgclient` | Memgraph database adapter |
| `pydantic-ai` | Agent framework for LLM integration |
| `pydantic-settings` | Settings management |
| `mcp` | Model Context Protocol SDK |
| `typer` | CLI framework |
| `rich` | Terminal rendering |
| `prompt-toolkit` | Interactive command line |
| `diff-match-patch` | Code patching |
| `watchdog` | Filesystem events monitoring |
| `huggingface-hub` | UniXcoder model download |
