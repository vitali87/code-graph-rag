---
description: "Security model and assurance case: threat model, trust boundaries, and how the project's security requirements are met."
---

# Security Model

This page documents what users can and cannot expect from code-graph-rag in terms of security: the security requirements, the trust boundaries, the threat model, and the argument for why the requirements are met. Vulnerability reporting is covered by the [security policy](https://github.com/vitali87/code-graph-rag/blob/main/.github/SECURITY.md), and project decision-making by the [governance document](https://github.com/vitali87/code-graph-rag/blob/main/GOVERNANCE.md).

## What the software does

code-graph-rag parses codebases into a knowledge graph stored in a local Memgraph database, optionally embeds code for semantic search in a local Qdrant instance, and answers natural-language questions about the code, either through the interactive CLI agent or through the MCP server.

## Security requirements

1. Tree-sitter parsing of analysed repository content must never execute that content. Frontends that invoke a language toolchain on the analysed project must be documented, along with how to disable them.
2. Credentials (LLM API keys, MCP tokens) must never appear in source code or in the graph.
3. The MCP server must not expose the graph or the tools to other hosts unauthenticated.
4. The paths by which code can leave the machine must be documented and controlled by explicit configuration.
5. Agent tool use (shell commands, file edits) must be constrained to the project being analysed, with any escape hatch requiring an explicit user choice.

## Trust boundaries and threat model

**The analysed repository is untrusted input, with one important exception.** Most language frontends are pure Tree-sitter parsers: they read bytes and produce syntax trees, so a hostile repository can at worst produce a wrong graph, not code execution. Two frontends go further. The C++ frontend defaults to `CPP_FRONTEND=hybrid`, which additionally parses translation units with libclang; this is still parsing only. The C# frontend defaults to `CSHARP_FRONTEND=auto`, which resolves to the Roslyn-based hybrid frontend whenever a `dotnet` toolchain is on `PATH`; that path runs `dotnet restore` and evaluates the project's MSBuild files, and source generators from the analysed repository execute inside the compilation. In other words, with the .NET SDK installed, indexing a C# repository runs parts of that repository's build with your privileges, and this is the default. Set `CSHARP_FRONTEND=treesitter` before indexing C# repositories whose build you do not trust. See the [graph schema documentation](graph-schema.md) for the frontend matrix.

**The graph and vector stores are local processes, but their ports are currently network-reachable.** Memgraph and Qdrant run in local Docker containers managed by `cgr daemon`. The compose file presently publishes their ports (7687, 7444, 3000, 6333, 6334) without a host-address restriction, which Docker binds to all interfaces, so on an untrusted network other hosts can reach the unauthenticated Memgraph and Qdrant endpoints. Treat the graph with the same confidentiality as the code itself and run the stack only on trusted networks; issue [#1012](https://github.com/vitali87/code-graph-rag/issues/1012) tracks changing the default to a loopback bind.

**The LLM and embedding provider boundary.** Code can leave the machine on two paths, both chosen by configuration: interactive querying sends snippets and questions to the configured model provider, and semantic indexing sends code to the embedding provider when `CGR_EMBEDDING_PROVIDER=openai` is set. Both use TLS with standard certificate verification (httpx defaults). The defaults keep everything local: a local UniXcoder model computes embeddings, and with a local model provider (Ollama) queries never leave the machine either. API keys are supplied via environment variables or `.env`, are excluded from version control, and are never written to the graph.

**The MCP server boundary.** The HTTP MCP server binds `127.0.0.1` by default. It refuses to bind a non-loopback address unless `MCP_HTTP_AUTH_TOKEN` is set, in which case every request must carry the bearer token. This closes the drive-by exposure reported in issue [#808](https://github.com/vitali87/code-graph-rag/issues/808).

**The agent's tools.** The CLI agent can run shell commands and edit files at the user's request. In the default permission mode, shell commands are checked against an allowlist and screened for destructive patterns (for example `rm` against paths outside the project) before execution, and file operations are scoped to the target project root. The interactive session offers a YOLO mode that disables the allowlist check (destructive-path screening still applies); it exists for users who accept the risk, is off by default, and requires an explicit toggle.

**XML and other parsers.** XML from analysed projects is parsed with `defusedxml`, which disables entity-expansion attacks.

## How the requirements are assured

- **Secure development process.** Every change goes through a pull request with CI, strict ruff linting, and a red/green test discipline; statement coverage is above 90% and is tracked on [SonarCloud](https://sonarcloud.io/project/overview?id=vitali87_code-graph-rag), which also performs static security analysis.
- **Dependency hygiene.** Dependencies are pinned in `uv.lock`, monitored by Dependabot, and scanned by OSV-Scanner in CI.
- **Supply-chain integrity.** Release binaries are signed with Sigstore and, from v0.0.484 onwards, carry SLSA build provenance generated by GitHub Actions; see [verifying release artifacts](../getting-started/installation.md#verify-release-artifacts). The project's [OpenSSF Scorecard](https://scorecard.dev/viewer/?uri=github.com/vitali87/code-graph-rag) is published, and the project holds the [OpenSSF Best Practices badge](https://www.bestpractices.dev/projects/13757).
- **No bespoke cryptography.** The project implements no cryptographic algorithms of its own; TLS and signature verification are delegated to httpx, Sigstore, and the platform.

## What users should not expect

- Toolchain-invoking analysis of a hostile repository is not sandboxed; the toolchain runs with your privileges (see the C# frontend caveat above).
- The local Memgraph and Qdrant containers are unauthenticated services intended for a single user on a trusted machine and network (see issue [#1012](https://github.com/vitali87/code-graph-rag/issues/1012)).
- Pre-1.0, the project releases continuously and security-relevant defaults may be tightened in any release; release notes call such changes out.
