# vigil

The night shift for code-graph-rag. Each night vigil turns the releases of
cgr's dependencies into experiments on cgr, and by morning reports verdicts,
not summaries: every item is measured, or recorded as not worth measuring
with the reason.

```
$ uv run --frozen python -m vigil watch      # releases not yet tried
$ uv run --frozen python -m vigil index      # cgr indexes itself, for impact
$ uv run --frozen python -m vigil upgrade    # one experiment per release
$ uv run --frozen python -m vigil report     # the morning standing
```

## One experiment

1. **Reach.** cgr's graph of itself names the modules that import the
   package (`vigil impact <package>`) and the test files one hop out. A
   tree-sitter grammar is loaded by name, so its reach is its language's
   test files.
2. **Candidate.** A worktree cut from `origin/main` takes the release with
   `uv lock --upgrade-package`. When cgr's published range excludes it, the
   one clause that does is widened. When the range admits it but the lock
   cannot (a dev tool caps a shared dependency), it is installed over the
   current lock, the way `pip install code-graph-rag` gets it.
3. **Baseline.** The same commit, synced with the same extras, on the same
   machine the same night. Nothing is compared across nights.
4. **Gates, in order:**
   - **Tests:** the selected tests on both sides, where only failures the
     baseline does not have count.
   - **Graph:** the canonical graph of three pinned corpora (requests,
     express, hexyl), which must be byte-identical.
   - **Timing:** nine interleaved A/B builds, each in a fresh interpreter. A
     change counts only past an exact sign test (p < 0.05) and a noise floor
     of max(50 ms, 2%).

## Verdicts

What decides urgency is whether fresh installs already resolve the release.

| | inside the published range | outside it |
|---|---|---|
| breaks tests, changes the graph, or builds slower | **ACT**: installs have it today | **WATCHING**: the cap still holds |
| green, graph identical | **READY**: a lock bump on `vigil/<package>-<version>` | **READY**: the widened requirement |
| green for pip users, but uv.lock cannot take it | **WATCHING**, with uv's reason | |

An adopt experiment (`vigil candidate`, then `vigil gate`) is a code change
made to use something a release added. It is **READY** only when it builds
faster beyond the floor with the graph byte-identical and peak RSS within
10%. Anything else is **DISCARDED**, with the reason.

## Memory

`.vigil/` is committed every night, because the session that runs a night
remembers nothing:

- `seen.json`: each release tried once
- `results.tsv`: every experiment, one row each
- `journal.md`: what each attempt learned
- `nights/<date>.jsonl` and `.md`: the night's outcomes and its report

The nightly procedure for the agent is `.claude/skills/vigil/SKILL.md`.
