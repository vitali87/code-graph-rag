## Summary

<!-- What does this PR do? Keep it brief: 1-3 bullet points. -->

-

## Demo

<!-- A user-visible change shows itself working, before and after: a VHS GIF for a terminal change, a screenshot for a graph, browser or docs change. Drag or paste the file here so it embeds as a GitHub attachment; do not commit it. With nothing user-visible, replace this with one line: "No user-visible change: <why>". See CONTRIBUTING.md, "Show the change". -->

## Type of Change

<!-- Check all that apply. -->

- [ ] Bug fix
- [ ] New feature
- [ ] Performance improvement
- [ ] Refactoring (no functional changes)
- [ ] Documentation
- [ ] CI/CD or tooling
- [ ] Dependencies

## Related Issues

<!-- Link related issues: "Fixes #123", "Closes #456", or "Related to #789" -->

## Test Plan

<!-- How was this tested? Check all that apply. -->

- [ ] Unit tests pass (`make test-parallel` or `uv run pytest -n auto -m "not integration"`)
- [ ] New tests added
- [ ] Integration tests pass (`make test-integration`, requires Docker)
- [ ] Manual testing (describe below)

## Checklist

- [ ] PR title follows [Conventional Commits](https://www.conventionalcommits.org/) format
- [ ] All pre-commit checks pass (`make pre-commit`)
- [ ] No hardcoded strings in non-config/non-constants files
- [ ] No `# type: ignore`, `cast()`, `Any`, or `object` type hints
- [ ] No new comments or docstrings (code should be self-documenting)
