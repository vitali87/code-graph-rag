---
description: "How an edit-algebra operation proves it did the right thing: a per-operation expectation checked against the structural delta, with pass or fail reasons and the tests to run."
---

# Postcondition Contract

The [structural delta](structural-delta.md) tells an agent what an edit did.
An edit-algebra operation must prove it did the right thing: same
computation, different contract (issue #1531). `codebase_rag.editing.verify`
takes the operation's expectation and the delta measured after its
transaction landed and answers pass or fail with reasons.

```python
from codebase_rag.editing import rename_expectation, verify

verdict = verify(rename_expectation([(old_qn, new_qn)], heuristic_allowed=False), delta)
verdict.ok               # False when any check failed
verdict.failures         # one reason per failed check
verdict.affected_tests   # the delta's tests_reaching, for the caller to run
```

## Expectations

| Operation          | Promise                                                                                                                 |
|--------------------|-------------------------------------------------------------------------------------------------------------------------|
| `rename`           | The delta reports exactly the requested `(old, new)` pairs as renamed; no other symbol appears or disappears; the call-site count into the touched definitions is unchanged; no caller is dangling; no site resolved by guesswork (`heuristic`, `overload`, `dynamic`) was rewritten unless `heuristic_allowed`. |
| `change_signature` | Every call site of a changed signature reads `ok`, was rewritten by the operation (a rewritten site is mapped by construction), or is listed in `unmapped` as `path:line`; an `unknown` verdict is not accepted; no `too_many` arity finding remains unlisted. |
| `move`             | The old name is reported renamed to its new home, importers are updated (no dangling callers), and no import cycle appeared. |
| all                | No new duplicate group; every file parses.                                                                              |

`Expectation` is a plain record, so an operation can waive a check it
legitimately breaks: an `inline` removes its definition and changes the
caller count, and says so with `removed=(qn,)` and
`caller_count_unchanged=False`.

## Enforcement

An operation measures its delta through the scoped re-ingest of the files it
wrote (`measure`), verifies, and on failure undoes its transaction with
`undo_last` and re-ingests the restored files so the graph follows. The
[rename operation](rename.md#postcondition-contract) does this whenever it is
given a `reingest` callable; the MCP `rename` tool and `cgr rename` pass the
live updater's. The verdict rides on the operation's report (`verdict`), so
an agent sees the reasons and the affected tests in one place.

The transaction's own verifier runs before the commit and answers a
narrower question (does every staged file still parse); the contract runs
after, because the graph can only measure what is on disk.
