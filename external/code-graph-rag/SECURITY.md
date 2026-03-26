# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.0.x   | :white_check_mark: |

As the project is in early development (pre 1.0), only the latest release receives security updates. Please ensure you are running the most recent version before reporting a vulnerability.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues, pull requests, or discussions.**

Instead, please use [GitHub Private Vulnerability Reporting](https://github.com/vitali87/code-graph-rag/security/advisories/new) to submit your report. This ensures the details remain confidential until a fix is available.

When reporting, please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof of concept
- The version(s) affected
- Any suggested fix, if available

## What to Expect

- **Acknowledgement** within 72 hours of your report
- **Status update** within 7 days with an initial assessment
- **Resolution target** of 30 days for confirmed vulnerabilities, though critical issues will be prioritized for faster turnaround

If the vulnerability is accepted, we will work on a fix, coordinate disclosure with you, and credit you in the release notes (unless you prefer to remain anonymous).

If the vulnerability is declined, we will provide a clear explanation of why.

## Scope

This policy applies to the `code-graph-rag` Python package and its official repository. Third party dependencies are outside the direct scope of this policy, though we use Dependabot to monitor and update them.

## Security Measures in This Project

- **Dependency scanning**: Dependabot is enabled for automated dependency updates
- **Secret scanning**: GitHub secret scanning is active on this repository
- **Branch protection**: The `main` branch requires pull request reviews before merging

## Preferred Languages

We accept security reports in English.
