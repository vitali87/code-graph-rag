---
description: "AI-powered codebase optimization with language-specific best practices and interactive approval."
---

# Code Optimization

Code-Graph-RAG provides AI-powered codebase optimization with best practices guidance and an interactive approval workflow.

## Basic Usage

```bash
cgr optimize python --repo-path /path/to/your/repo
```

## With Reference Documentation

Guide the optimization process using your own coding standards:

```bash
cgr optimize python \
  --repo-path /path/to/your/repo \
  --reference-document /path/to/best_practices.md
```

```bash
cgr optimize java \
  --reference-document ./ARCHITECTURE.md
```

```bash
cgr optimize rust \
  --reference-document ./docs/performance_guide.md
```

The agent incorporates guidance from your reference documents when suggesting optimizations, ensuring they align with your project's standards and architectural decisions.

## Using Specific Models

```bash
cgr optimize javascript \
  --repo-path /path/to/frontend \
  --orchestrator google:gemini-2.0-flash-thinking-exp-01-21
```

```bash
cgr optimize javascript --repo-path /path/to/frontend \
  --batch-size 5000
```

## Supported Languages

All supported languages: `python`, `javascript`, `typescript`, `rust`, `go`, `java`, `scala`, `cpp`

## How It Works

1. **Analysis Phase**: The agent analyzes your codebase structure using the knowledge graph
2. **Pattern Recognition**: Identifies common anti-patterns, performance issues, and improvement opportunities
3. **Best Practices Application**: Applies language-specific best practices and patterns
4. **Interactive Approval**: Presents each optimization suggestion for your approval before implementation
5. **Guided Implementation**: Implements approved changes with detailed explanations

## Example Session

```
Starting python optimization session...
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ The agent will analyze your python codebase and propose specific          ┃
┃ optimizations. You'll be asked to approve each suggestion before          ┃
┃ implementation. Type 'exit' or 'quit' to end the session.                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

Analyzing codebase structure...
Found 23 Python modules with potential optimizations

Optimization Suggestion #1:
   File: src/data_processor.py
   Issue: Using list comprehension in a loop can be optimized
   Suggestion: Replace with generator expression for memory efficiency

   [y/n] Do you approve this optimization?
```

## CLI Options

| Option | Description |
|--------|-------------|
| `--orchestrator` | Specify provider:model for main operations |
| `--cypher` | Specify provider:model for graph queries |
| `--repo-path` | Path to repository (defaults to current directory) |
| `--batch-size` | Override Memgraph flush batch size |
| `--reference-document` | Path to reference documentation |
