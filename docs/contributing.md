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
- Show a user-visible change in the PR description (see below)

### Show the change

A pull request that changes something a user can see must show it working, in the PR description, before and after. This applies to humans and AI agents alike.

- **What counts as user-visible:** CLI commands, output and prompts; MCP tool results; the nodes, relationships and properties the graph holds for a real snippet; the realtime watcher; the docs site and the README.
- **How to show it:** a terminal change gets a GIF recorded with [VHS](https://github.com/charmbracelet/vhs), with the `.tape` file kept out of the repository. A graph, browser or docs change gets a screenshot. Record from the PR's own branch, running its own code.
- **How to attach it:** embed it inline as a GitHub attachment, never committed. Reference the file in the body and let `gh` upload it: `gh pr create --attach ./after.png` or `gh pr edit <n> --attach ./after.png` (GitHub CLI 2.99 or later). A body line such as `![after](./after.png)` is rewritten to the uploaded `github.com/user-attachments` URL; without one the image is appended. By hand, drag or paste the file into the description.
- **When nothing is visible:** an internal refactor, a test-only or CI-only change says so in one line instead, for example `No user-visible change: refactors the call resolver's cache key.`

### CI Pipeline

All pull requests are validated by CI, which runs in parallel:

1. **Lint & Format**: `ruff check` and `ruff format --check`
2. **Type Check**: `ty check` on production code
3. **Unit Tests**: Parallel execution with `pytest-xdist` and coverage reporting
4. **Integration Tests**: Full stack testing with Memgraph
5. **PR Title Validation**: Conventional Commits format check

### Which number a merge bumps

Every bump raises the patch number unless a pull request merged since the
version last changed carries the `breaking change` label. Then the minor number
goes up while the version is below 1.0, and the major number from 1.0 on. A
breaking change counts once: a later bump does not count it again, even when
the release that carried it was deferred, and a version set by hand already
accounts for every pull request merged up to it. Nothing rolls over: the
patch number keeps counting past 999, because a carry into the minor number
would announce a breaking change that never happened. To pick a type by hand,
run the workflow with its `bump_type` input or change the version in the pull
request itself. A version set in the pull request is not bumped again: its
merge gets the bump commit and tag a bumped version gets, and a release when
one is due, so a breaking change or security fix in it ships with it.

### Release cadence and why tags outrun releases

`version-bump.yml` tags every version bump, but only some of those tags publish.
A bump is not one per merge: runs are serialised, and only the run for the
newest commit on `main` bumps, so merges that land close together share one
bump and one tag. Each older run would still need a runner just to stand down,
one after another, so `version-bump-supersede.yml` cancels every bump run that
has not started yet for a commit the newest merge contains, and the newest
run goes next. Which tags publish comes down to the CREDENTIAL used to push
the tag, which is easy to miss when reading the workflows:

```yaml
git tag "v${NEW_VERSION}"
if [ "$RELEASE" = "true" ] && [ "$CI_GATE" = "success" ]; then
  git push origin "v${NEW_VERSION}"          # SSH deploy key
else
  git push "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
    "v${NEW_VERSION}"
fi
```

`publish.yml` triggers on `push: tags: ["v*"]`, so it looks like every tag should
publish. It does not, because GitHub deliberately does not run workflows for
events pushed with `GITHUB_TOKEN`. Interim tags therefore land on the remote and
publish nothing, while release tags go out over the SSH deploy key and do trigger
the publish.

A release happens every 50 versions, or immediately when the `decide` step
sees a security fix (a GHSA id in the commit message, an explicit `[security]`
marker, or an associated PR labelled `security`) or a PR labelled
`breaking change`, so the version that announces a breaking change is one
users can install. To ship any other merge at once, label its PR `release now`
before merging: it releases without the security notice or a breaking bump.
It counts only when whoever applied it last is a repository admin or
maintainer; applied by anyone else, a bot or a workflow token included, it is
ignored and the run's summary says so. Do not borrow the `security` label for this, since every release it triggers
opens with a "contains a security fix" notice. The label is read when the
merge's bump runs; added after that, it comes too late, since the merge is
already behind the bump's tag. A release also needs green CI
on the merge commit it is cut from, within 45 minutes of the bump being pushed.
When CI fails or does not finish in time, the bump is still tagged, as an
interim tag annotated `release-deferred:`, and the release moves to the next
bump (the run's summary reads "Release: deferred"). The next run never starts
its scan at such a tag, so it reads the deferred commits again and a security
fix, breaking change or `release now` PR among them still releases without
waiting for the 50-version cadence. The breaking change still bumps the number
only once.

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
