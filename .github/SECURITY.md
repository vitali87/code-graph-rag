# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.0.x   | :white_check_mark: |

As the project is in early development (pre 1.0), only the latest release receives security updates. Please ensure you are running the most recent version before reporting a vulnerability.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public issues, pull requests, or any other public channels.**

Instead, please use GitHub's private vulnerability reporting: go to the [Security tab](https://github.com/vitali87/code-graph-rag/security/advisories/new) and click **Report a vulnerability**. This keeps the details confidential between you and the maintainers until a fix is available.

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

- **Dependency scanning**: Dependabot alerts and security updates are enabled, with version updates configured weekly for GitHub Actions, Docker and pip
- **Secret scanning**: GitHub secret scanning is active on this repository
- **Push protection**: Secret scanning push protection blocks commits containing supported secrets before they reach the repository
- **Code scanning**: CodeQL default setup runs weekly across the Actions, C/C++, C#, JavaScript/TypeScript and Python code in this repository
- **Vulnerability scanning**: The [OSV-Scanner](https://google.github.io/osv-scanner/) workflow checks dependencies against the OSV database on pull requests targeting `main`, on pushes to `main` and weekly, and reports findings to the Security tab
- **Supply chain scorecard**: The [OpenSSF Scorecard](https://github.com/ossf/scorecard) workflow audits the repository's supply chain posture and reports findings to the Security tab
- **Private vulnerability reporting**: Enabled, so vulnerabilities can be reported privately through the Security tab as described above
- **Branch protection**: The `main` branch is covered by a ruleset that requires changes to arrive by pull request with the `All Checks Pass` status check green, and blocks branch deletion and force pushes

## Preferred Languages

We accept security reports in English.
