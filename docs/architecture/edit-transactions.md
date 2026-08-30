---
description: "How cgr applies multi-file edits atomically: stage in an overlay, verify, then commit or roll back, with a recorded history for show and undo."
---

# Edit Transactions

An edit-algebra operation (a rename, a signature change, a move) touches
many files at once. Those files must land completely or not at all, and a
bad batch must be reversible. `codebase_rag.editing.EditTransaction` is the
primitive every such operation goes through (issue #1528); agents do not
drive it directly.

## Lifecycle

```python
from codebase_rag.editing import EditTransaction, VerificationResult

tx = EditTransaction(repo_root)
tx.stage("pkg/models.py", new_models_source)   # full content, str or bytes
tx.stage("pkg/old_helper.py", None)            # delete
tx.stage("pkg/new_helper.py", helper_source)   # create

def verify(tree):
    # `tree.read(rel)` answers from the overlay first, the disk second;
    # `tree.root` is a materialised copy for tools that need real files.
    return VerificationResult(ok=parses(tree.read("pkg/models.py")), message="")

outcome = tx.commit(verify)
outcome.applied      # True only if every file was written
outcome.diff         # the combined unified diff (a/ b/ style, /dev/null for create and delete)
outcome.files        # repo-relative paths, sorted
```

- **Stage** collects the new content per file in an in-memory overlay keyed
  by repo-relative path; the working tree is untouched. Staging the current
  content is a no-op, a later stage of the same path wins, and the baseline
  (`before`) is captured from disk at first stage. Paths that resolve outside
  the repo are refused.
- **Verify** runs the caller's callback against a `StagedTree`. Most checks
  only need `read`/`exists`; a check that runs a real tool asks for `root`,
  which copies the tree once (ignored directories and cgr state files
  excluded) and writes the overlay on top. A verifier that returns `False`,
  returns a failing `VerificationResult`, or raises rejects the transaction.
- **Commit** first refuses (`TransactionConflict`) if any staged file no
  longer holds the bytes it was staged on, then writes each file through a
  temp sibling and `os.replace`, holding the originals so a failure part-way
  restores what already landed. Commits to one repo serialise on a lock.
  On a rejected verification the working tree is byte-identical to before.
- **Rollback** discards the overlay. `with transaction(root) as tx:` rolls
  back on an exception.

## History, show and undo

Every applied transaction is appended to `.cgr-edit-history.json` at the
repo root (one of the `CGR_STATE_FILENAMES`, so it is never indexed): the
id, timestamp, each file's before/after bytes and the verification outcome.
The file keeps the last 50 transactions.

```bash
cgr edits show            # newest first; --diff prints each patch set
cgr edits undo            # reverse the latest transaction
cgr edits undo -n 3       # reverse the latest three, newest first
```

An undo is itself a transaction staging `after -> before`. It refuses, and
stops the run, when a file no longer holds what the transaction wrote, so an
undo never clobbers a later hand edit; the entry stays in the history until
it is undone.
