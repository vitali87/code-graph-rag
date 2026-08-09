# Governance

This document describes how the code-graph-rag project is governed: how decisions are made, who holds which roles, and how the project continues if a key person becomes unavailable.

## Governance Model

code-graph-rag uses a maintainer-led governance model. A single lead maintainer holds final decision-making authority over the project's direction, code, and releases. All decisions are made in the open through GitHub issues and pull requests, and anyone is welcome to participate in those discussions.

## Roles and Responsibilities

### Lead Maintainer

The lead maintainer is [Vitali Avagyan (@vitali87)](https://github.com/vitali87). The lead maintainer is responsible for:

- Setting the project direction and maintaining the [roadmap](https://docs.code-graph-rag.com/roadmap/)
- Triaging issues and reviewing pull requests
- Merging changes and publishing releases to PyPI and GitHub
- Responding to security reports as described in the [security policy](.github/SECURITY.md)
- Enforcing the [code of conduct](.github/CODE_OF_CONDUCT.md)

### Contributors

Anyone may contribute through pull requests, following the [contributing guide](CONTRIBUTING.md). Contributors are credited in release notes and retain copyright in their contributions, which are accepted under the project's MIT licence.

### Becoming a Maintainer

Contributors who make sustained, high-quality contributions and demonstrate good judgement in reviews and issue discussions may be invited by the lead maintainer to become co-maintainers with commit access.

## Decision Making

Routine decisions (bug fixes, small features, dependency updates) are made through the normal pull request process. Significant decisions (new language support, architectural changes, breaking changes) are proposed and discussed in GitHub issues before implementation, so the reasoning is public and searchable. The lead maintainer makes the final call when consensus is not reached.

## Continuity

The project is designed so that no local machine or personal key is required to keep it running:

- The repository, the PyPI package, and release automation are all driven from GitHub; releases are built and signed by GitHub Actions using keyless Sigstore signing, and PyPI publishing uses trusted publishing rather than a maintainer-held token.
- The automated version-bump pipeline uses repository-scoped secrets (a deploy key and a release-notes endpoint token); a repository admin can regenerate these, and releases can also be dispatched manually without them.

An emergency contact with standby access to the repository and the PyPI project is being designated so that issues, merges, and releases can continue within a week if the lead maintainer becomes unavailable.

If the project becomes unmaintained, the MIT licence permits anyone to fork and continue it.
