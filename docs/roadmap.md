---
description: "What Code-Graph-RAG intends to do, and not do, over the next year."
---

# Roadmap

This page describes what the project intends to do, and deliberately not do, over roughly the next year (from mid-2026). It is a statement of direction rather than a schedule; items ship when they are ready. Day-to-day priorities and known analysis gaps live in the [issue tracker](https://github.com/vitali87/code-graph-rag/issues).

## What we intend to do

**Deepen analysis quality for the languages we already support.** The fully supported languages (see the [language support matrix](architecture/language-support.md) for the current list and per-language status) should converge on the same level of fidelity: call resolution, type inference, and data-flow edges. The known per-language type-inference gaps are tracked in the issue tracker and are worked down continuously.

**Keep improving dead-code detection precision.** False-positive reduction against real-world codebases is an ongoing campaign, driven by dogfooding the tool against large open-source projects and fixing every class of misreport at the root.

**Maintain and extend the evaluation harness.** Structure evals with native per-language AST oracles keep the extractors honest. New analysis features land with eval coverage.

**Grow the configuration-driven language tier.** The ast-grep YAML tier lets a new language be added as a configuration file rather than a full extractor. This is the intended path for broadening language coverage cheaply.

**Stability of the SDK and MCP server.** The Python SDK and the MCP server are the supported integration surfaces and their interfaces are kept stable within the pre-1.0 versioning policy below.

**Performance and incremental updates.** Large-repository ingestion time and the real-time updater are active areas of improvement.

**Security posture.** The project holds the OpenSSF Best Practices passing badge and is working towards the silver badge, alongside improving its OpenSSF Scorecard results.

## What we do not intend to do

**New first-class language frontends without demonstrated demand.** A new hand-written extractor is a large permanent maintenance cost. Requests for new languages start as issues; a language graduates to a first-class frontend only when multiple users ask for it. Until then, the ast-grep YAML tier is the supported route.

**A hosted service.** code-graph-rag stays a local-first CLI and SDK. Your code and your graph remain on your machine unless you configure a cloud LLM provider yourself.

**API stability guarantees before 1.0.** The project releases continuously (a patch release on every merge), so while it is pre-1.0, interfaces may change in any release. Breaking changes are called out in release notes.
