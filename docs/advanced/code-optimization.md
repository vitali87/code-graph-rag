# AI-Powered Code Optimization

Get intelligent code optimization suggestions with language-specific best practices and interactive approval workflow.

## Quick Start

```bash
graph-code optimize <language>
```

Supported languages: `python`, `javascript`, `typescript`, `rust`, `java`, `cpp`, `go`, `php`

---

## How It Works

1. Analyzes your codebase using the knowledge graph
2. Identifies optimization opportunities using AI
3. Presents suggestions with explanations
4. Applies approved changes automatically
5. Shows diffs for review

---

## Example Session

```bash
graph-code optimize python
```

Output:
```
=== Optimization Suggestions ===

File: src/utils/data_processor.py
Function: process_large_dataset

Current code:
   data = [item for item in large_list if item.is_valid()]

Suggestion: Replace with generator expression for memory efficiency

[y/n] Do you approve this optimization?
```

---

## Reference Document Support

Guide optimizations using your own coding standards and architectural guidelines:

```bash
# Use company coding standards
graph-code optimize python \
  --reference-document ./docs/coding_standards.md

# Use architectural guidelines
graph-code optimize java \
  --reference-document ./ARCHITECTURE.md

# Use performance best practices
graph-code optimize rust \
  --reference-document ./docs/performance_guide.md
```

The agent incorporates guidance from your reference documents when suggesting optimizations, ensuring they align with your project's standards.

---

## Optimization Categories

### Performance
- Algorithm complexity improvements
- Memory usage optimization
- Caching strategies
- Database query optimization

### Code Quality
- Naming improvements
- Code duplication removal
- Design pattern application
- Error handling enhancement

### Best Practices
- Language-specific idioms
- Security improvements
- Type safety enhancements
- Documentation additions

---

## CLI Options

| Option | Description | Example |
|--------|-------------|---------|
| `--reference-document` | Path to reference docs | `--reference-document ./docs/standards.md` |
| `--orchestrator` | Override LLM for optimization | `--orchestrator openai/gpt-4o` |
| `--repo-path` | Target repository path | `--repo-path /path/to/repo` |

---

## Best Practices

1. **Run on Feature Branches**: Test optimizations before merging
2. **Review All Changes**: Understand each suggestion before approving
3. **Use Reference Docs**: Guide AI with your project standards
4. **Test After Optimization**: Run test suites to verify changes
5. **Commit Incrementally**: Commit logical groups of optimizations

---

## Related Documentation

- **[Basic Usage](../usage/basic-usage.md)** - Getting started
- **[LLM Configuration](../llm/configuration.md)** - Configure optimization models
