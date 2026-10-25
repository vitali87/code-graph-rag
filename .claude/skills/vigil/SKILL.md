---
name: vigil
description: Run vigil, the night shift for code-graph-rag. Each night it turns new releases of cgr's dependencies into measured experiments on cgr and reports verdicts (ACT, READY, DISCARDED, WATCHING) by morning. Use when the nightly vigil message arrives, when asked to run vigil or a vigil night, and when the owner replies to a vigil morning report (asks why, asks to open a PR for a READY item, or asks to retry an idea).
---

# vigil: the night shift for code-graph-rag

vigil measures; it does not summarise. Every item becomes an experiment with
a verdict, or it is recorded as not worth one, with the reason. The scorer is
`python -m vigil`; you run it, read its output, and use judgement only where
the procedure below asks for it.

All commands run from the repository root with `uv run --frozen python -m vigil ...`
(written `vigil ...` below). `vigil --help` lists them.

## The night

1. **Prepare.** `git fetch origin main`, `uv sync --frozen`, then `vigil index`
   (cgr indexes itself into its embedded graph, about 4 minutes; `impact`
   reads it). Read `.vigil/journal.md` before deciding anything: it is what
   earlier nights learned, and you remember none of it.
2. **Watch.** `vigil watch` lists the dependency releases not yet tried. Note
   the count for the report.
3. **Upgrade experiments.** `vigil upgrade` runs every new item: it cuts a
   worktree from `origin/main`, takes the release, syncs, runs the tests
   cgr's graph selects, checks the canonical graph on three pinned corpora
   and times builds against a baseline measured tonight. Each verdict is
   recorded as it lands. Run it in the background and wait for it; it takes
   minutes per item.
4. **Read what the releases changed (judgement).** For each tonight's ACT
   and READY item and for any WATCHING item whose release is a new major,
   open its release notes (the `links` in `vigil watch --json`, else the
   project's GitHub releases) and look for exactly three things:
   - a deprecation or removal that hits a call cgr makes: find the call
     sites with `uv run cgr graph callers <qualified name>` and record it
     with `vigil note <item> --verdict WATCHING --headline "..." --evidence "<site>"`;
   - a faster or new API that could replace code in the package's cone
     (`vigil impact <package>`): that is an adopt idea, step 5;
   - nothing relevant: record nothing.
5. **Adopt experiments (at most 3 a night).** One idea per experiment.
   Skip an idea the journal already records as tried, unless the release
   that prompted it is newer than that attempt.
   - `vigil candidate <name>` makes a synced worktree from `origin/main` and
     prints its path.
   - Make the edit there, or delegate it to a subagent with the path, the
     idea, and the files of the cone it may touch. Edits only: the subagent
     must not run benchmarks.
   - `vigil gate <name> --item <item id> --idea "<one line>"` judges it:
     frozen paths, ruff and ty, selected tests, identical graph, then
     interleaved timing. READY means faster beyond the noise floor with the
     graph byte-identical; anything else is DISCARDED with the reason.
6. **Report.** `vigil report --considered <count from step 2>` prints the
   standing and saves it to `.vigil/nights/<night>.md`.
7. **Persist.** Commit `.vigil/` on this branch
   (`chore(vigil): night <date>`) and push it, so the next night's session
   starts from this one's memory.
8. **End the turn with the report**, exactly as `vigil report` printed it,
   followed by one line listing the READY branches by name.

## Rules

- **The scorer is the contract.** A candidate never edits `vigil/`,
  `.vigil/`, `evals/`, `benchmarks/`, `codebase_rag/tests/` or `.github/`;
  `vigil gate` discards one that does.
- **Correctness before speed.** A changed graph or a new test failure ends
  an adopt attempt, however fast it is.
- **Noise is not a win.** "within noise" is DISCARDED for an adopt
  experiment. Never re-run a timing hoping for a better draw.
- **Measure serially.** Never run two vigil commands that build or time at
  once: they share the machine and would corrupt each other's timings.
  Subagents edit; only you run `upgrade` and `gate`.
- **Never compare across nights.** Baselines are re-measured each night in
  the same container. A timing from another night is not evidence.
- **Push only this branch and `.vigil/`.** READY branches stay local until
  the owner asks for them. Do not open PRs, merge, or push to main.
- **Budget.** Stop starting new experiments after 5 hours of wall time and
  report what finished.

## In the morning (the owner replies)

- *"Why X?"*: answer from `.vigil/nights/<night>.jsonl` and the journal,
  quoting the evidence lines; do not re-derive from memory.
- *"Ship X"* or *"PR for X"*: push `vigil/<branch>` and open a PR against
  main whose body is the outcome's headline and evidence, following
  CONTRIBUTING.md (it is a dependency change: say "No user-visible change"
  unless the verdict says otherwise).
- *"Retry X with Y"*: run it as an adopt experiment (step 5) now, then
  report its verdict in the same format.
