---
description: "Public incident response plan for security vulnerabilities and compromised project releases."
---

# Incident Response Plan

This plan explains how code-graph-rag handles security incidents. The
[security policy](https://github.com/vitali87/code-graph-rag/blob/main/.github/SECURITY.md)
remains the source of truth for reporting instructions, supported versions and
response targets. Roles and continuity arrangements are described in
[governance](https://github.com/vitali87/code-graph-rag/blob/main/GOVERNANCE.md).

## Scope and responsibility

The lead maintainer coordinates investigation, containment, recovery and
communication. This is a sole-maintainer project, not a continuously staffed
incident response service; availability may affect response times.

This plan covers vulnerabilities affecting the project, compromised repository
or publishing access, and suspect official packages, container images and
binaries. Dependency vulnerabilities are assessed for their impact on the
project, with upstream fixes coordinated where appropriate.

Deployment operators are responsible for their own hosts, databases, credentials
and incident investigations. The maintainer provides relevant fixes, mitigations
and guidance, but cannot monitor or recover users' deployments.

## When to activate this plan

Activate the relevant checklist for a credible vulnerability report, suspected
compromise of project access or releases, active exploitation, or unauthorized
data access linked to the software. Prioritize ongoing harm and publishing
compromise. A scanner alert alone is not proof of an incident: first assess
whether it affects the project.

Keep an incident record containing discovery time, known facts and unknowns,
affected versions and artifact hashes, evidence locations, actions and decisions
with timestamps, communications, and the next update. Keep sensitive evidence
and undisclosed vulnerability details out of public issues and commits; publish
only information suitable for disclosure.

## Vulnerability response

1. Acknowledge the report through the channel in the security policy. Request
   only the information needed to investigate, not credentials or entire private
   repositories.
2. Reproduce safely, identify affected versions and configurations, and assess
   impact and evidence of exploitation. Separate confirmed facts from assumptions.
3. Identify a temporary mitigation where possible. For affected dependencies,
   assess an update or workaround and coordinate with upstream as needed.
4. Prepare a fix and regression coverage. Verify both the fix and the mitigation.
5. Coordinate disclosure with the reporter. Publish a verified release and a
   GitHub Security Advisory where appropriate, stating affected and fixed
   versions, mitigations and any additional operator actions.
6. Record the outcome and follow-up work. Credit the reporter according to the
   security policy.

## Compromised access or release response

1. Suspend affected publishing paths and automated releases to limit further
   distribution.
2. Preserve relevant logs, workflow runs, commit identifiers, artifact hashes
   and timestamps before cleanup where practical. Do not delay urgent containment
   solely to collect evidence.
3. Recover affected accounts from a trusted device. Review account sessions,
   collaborators, applications, deploy keys, tokens and trusted-publisher
   permissions; revoke unauthorized or exposed access.
4. Determine which source changes and artifacts are affected across PyPI, GHCR
   and GitHub binary releases. A valid signature establishes provenance, not that
   an artifact produced by a compromised workflow is safe.
5. Coordinate restriction or withdrawal of suspect artifacts with the relevant
   platform, retaining their identifiers and evidence. Warn users promptly when
   there is actionable risk, even if the investigation is incomplete.
6. Review the source and publishing configuration, build replacements in a
   trusted environment, and verify published artifacts before resuming releases.
7. Explain whether users should stop using an artifact, investigate their hosts
   or rotate potentially exposed credentials. Installing a replacement does not
   by itself undo an earlier compromise.

## Communication and operator guidance

Use the private reporting channel for undisclosed vulnerability details.
Communicate publicly through repository security advisories and release notes
as appropriate. Each incident update should state:

- Status and publication time, including time zone.
- Affected versions, artifacts or configurations, and what remains unknown.
- Confirmed impact and specific actions users should take.
- Available mitigation or fixed version.
- The next update time, or that the incident is closed.

Avoid publishing secrets, private source code, personal information or details
that would unnecessarily expose users before mitigations are available.

For unintended database exposure, refer operators to the existing
[security model and deployment remediation instructions](architecture/security.md#trust-boundaries-and-threat-model).
Previously generated Compose files retain their bindings: upgrading the package
alone does not repair an exposed deployment. Operators should also assess
possible unauthorized access, not just correct the configuration.

## Recovery, closure and review

Close an incident once the relevant fix or access recovery is verified, affected
publishing paths are trusted again, necessary user guidance is published, and
remaining uncertainties and follow-up actions are recorded. Distinguish project
recovery from recovery of individual deployments, which operators must verify.

Record lessons learned and improve the relevant safeguards or documentation.
Review this plan after an incident, a significant publishing change, and at least
every six months.

### Practice exercise

Rehearse a suspected malicious release as a discussion-only exercise: identify
the artifact and publishing run, explain how publishing would be suspended,
walk through access recovery, draft a user warning, and describe how to verify
a clean replacement. Include what happens if the maintainer is unavailable.

Do not revoke access, remove real releases or send real incident notifications
as part of the exercise. Record the exercise date, gaps and follow-up actions
without publishing sensitive recovery details. Having this plan does not itself
demonstrate that an exercise has been completed.
