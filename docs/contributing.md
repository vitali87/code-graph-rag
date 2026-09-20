---
description: "Contribution guidelines for Code-Graph-RAG including setup, code standards, and PR process."
---

# Contributing

Thank you for your interest in contributing to Code-Graph-RAG!

!!! note "Canonical guide"
    This page is a quick-start summary. The full, authoritative contribution guide, including the complete coding standards, lives in [`CONTRIBUTING.md`](https://github.com/vitali87/code-graph-rag/blob/main/CONTRIBUTING.md) at the repository root. When the two differ, the root guide wins.

## Getting Started

1. **Browse Issues**: Check out the [issue tracker](https://github.com/vitali87/code-graph-rag/issues) to find tasks that need work. Look for `good first issue` and `help wanted` labels.
2. **Pick an Issue**: Choose an issue that interests you and matches your skill level
3. **Comment on the Issue**: Let us know you're working on it to avoid duplicate effort
4. **Fork the Repository**: Create your own fork to work on
5. **Create a Branch**: Use a descriptive branch name like `feat/add-feature` or `fix/bug-description`

## Development Setup

```bash
git clone https://github.com/YOUR-USERNAME/code-graph-rag.git
cd code-graph-rag
make dev
```

This installs all dependencies and sets up pre-commit hooks automatically.

## Pre-commit Hooks

All commits must pass pre-commit checks. Do not skip hooks with `--no-verify`.

```bash
pre-commit install
pre-commit autoupdate
```

## Running Checks Locally

```bash
make lint          # Lint check
make format        # Apply formatting (ruff format writes files; CI uses ruff format --check)
make typecheck     # Type check
make test-parallel # Unit tests in parallel
make test-integration  # Integration tests (requires Docker)
```

Or run everything at once:

```bash
make check      # Runs lint + typecheck + test
make pre-commit # Runs ALL pre-commit checks (mirrors CI)
```

## Pull Request Guidelines

- Keep PRs focused on a single issue or feature
- Write clear, descriptive commit messages using Conventional Commits format
- Include tests for new functionality
- Update documentation when necessary
- Be responsive to feedback during code review

### CI Pipeline

All pull requests are validated by CI, which runs in parallel:

1. **Lint & Format**: `ruff check` and `ruff format --check`
2. **Type Check**: `ty check` on production code
3. **Unit Tests**: Parallel execution with `pytest-xdist` and coverage reporting
4. **Integration Tests**: Full stack testing with Memgraph
5. **PR Title Validation**: Conventional Commits format check

### Release cadence and why tags outrun releases

`version-bump.yml` tags every merge, but only some of those tags publish. The
difference is the CREDENTIAL used to push the tag, which is easy to miss when
reading the workflows:

```yaml
git tag "v${{ steps.bump_version.outputs.new }}"
if [ "${{ steps.decide.outputs.release }}" = "true" ]; then
  git push origin "v${{ steps.bump_version.outputs.new }}"          # SSH deploy key
else
  git push "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
    "v${{ steps.bump_version.outputs.new }}"
fi
```

`publish.yml` triggers on `push: tags: ["v*"]`, so it looks like every tag should
publish. It does not, because GitHub deliberately does not run workflows for
events pushed with `GITHUB_TOKEN`. Interim tags therefore land on the remote and
publish nothing, while release tags go out over the SSH deploy key and do trigger
the publish.

A release happens every 50 versions, or immediately when the `decide` step
sees a security fix (a GHSA id in the commit message, an explicit `[security]`
marker, or an associated PR labelled `security`).

If you are ever debugging "the tag exists but PyPI did not update", check WHICH
kind of tag it is first. For an interim tag this is the intended behaviour and
there is nothing to fix. For a release tag it is a real failure: that one is
pushed over the SSH deploy key and must trigger `publish.yml`, so investigate
the workflow run rather than assuming the cadence explains it.

### Automated Code Review

This project uses automated code review bots (**Greptile** and **Gemini Code Assist**). Before requesting a human review, address all bot comments by either implementing suggestions or replying with a clear justification for why a suggestion doesn't apply.

A green check list does not by itself mean a PR was verified: a review bot's *skip* notice is a comment with a non-empty body, and a PR based on a branch other than the default is covered by no ruleset, so nothing is required of it. Run `uv run python scripts/check_pr_gated.py <pr-number>` before merging; it reports every reason a PR is not verifiably gated rather than only the first.

## Technical Requirements

- **PydanticAI for now**: The current framework choice, open to a well-argued change. Do not add a second one (LangChain, CrewAI, AutoGen) in a feature PR; open an issue first
- **External services**: Integrations use free tiers only, resolve their backend from configuration rather than hard-coding one vendor, and default to a keyless provider where one exists. Paid capability belongs in [Enterprise Services](https://code-graph-rag.com/enterprise). Contributors integrating a service they are compensated by disclose that in the PR description. See [`CONTRIBUTING.md`](https://github.com/vitali87/code-graph-rag/blob/main/CONTRIBUTING.md) for the full terms
- **Heavy Pydantic Usage**: Use Pydantic models for data validation, serialisation, and configuration
- **Package Management**: Use `uv` for all dependency management
- **Code Quality**: Use `ruff` for linting and formatting
- **Type Safety**: Use type hints everywhere and run `uv run ty check`

## Development Tools

| Tool | Purpose |
|------|---------|
| `uv` | Package manager and dependency resolver |
| `ruff` | Code linting and formatting |
| `ty` | Static type checking (from Astral) |
| `pytest` | Testing framework |
| `ripgrep` (`rg`) | Shell command text searching |

## Comment Policy

Write comments that explain why and how, not what. A comment that only restates the adjacent code adds no value.

## Questions?

- Open a discussion on GitHub
- Comment on the relevant issue
- Reach out to the maintainers
