---
description: "Keep your Code-Graph-RAG knowledge graph synchronised with code changes using the real-time file watcher."
---

# Real-Time Graph Updates

For active development, keep your knowledge graph automatically synchronised with code changes using the real-time updater.

## What It Does

- Watches your repository for file changes (create, modify, delete)
- Automatically updates the knowledge graph in real-time
- Re-parses only the changed file and the files that depend on it, and re-resolves calls in that set only (the same `GraphUpdater.reingest` path the MCP `reingest` tool uses)
- Filters out irrelevant files (`.git`, `node_modules`, etc.)

## Usage

Run the real-time updater in a separate terminal:

```bash
python realtime_updater.py /path/to/your/repo
```

![The real-time updater watching pallets/itsdangerous in one tmux pane while the other appends a function and cgr graph resolve finds it after the re-ingest](../assets/demos/realtime-updates.gif)

*Terminal 2 edits `encoding.py`; the watcher re-ingests it (and its dependents) and the new function is in the graph.*

Or using the Makefile:

```bash
make watch REPO_PATH=/path/to/your/repo
```

### With Custom Memgraph Settings

```bash
python realtime_updater.py /path/to/your/repo \
  --host localhost --port 7687 --batch-size 1000
```

```bash
make watch REPO_PATH=/path/to/your/repo HOST=localhost PORT=7687 BATCH_SIZE=1000
```

## Multi-Terminal Workflow

The watcher names the project exactly as `cgr start --repo-path` does for the
same directory (the directory name plus a hash of its resolved path), so its
live updates land in the project `cgr start` and the other `cgr` commands read.
If you name the project with `--project-name`, pass the same name to both.

```bash
# Terminal 1: Start the real-time updater
python realtime_updater.py ~/my-project

# Terminal 2: Run the AI assistant
cgr start --repo-path ~/my-project
```

### One sync of a checkout at a time

Every writer of a checkout (`cgr start --update-graph`, `cgr index`, the MCP
server's index and update tools, the watcher, `cgr check` and the MCP
`reingest`) takes the checkout's sync lock, `.cgr-sync-lock`, in the
checkout's directory under `CGR_HOME` beside its other sync state (see
[cgr's own state](../advanced/ignore-patterns.md#cgrs-own-state)); nothing is
written into the repository. Two syncs of one checkout at once would
interleave their deletes and re-creates and leave the graph incomplete, so:

- A full sync started while another holds the lock stops before it changes
  anything and names the running one:
  `Another sync of /path/to/repo is running (pid 4242, project 'repo__a4231746') ...`.
  Run it again when that sync has finished.
- The watcher's first scan and every scoped reingest (a watched change, an MCP
  edit's reingest, `cgr check`) wait for the running sync instead, then apply
  their change to the graph it finished.

The lock is an OS file lock, so a sync that crashes or is killed releases it
with its process; there is never a stale lock to delete. Like the rest of the
state under `CGR_HOME`, it is per user: syncs run by users with separate
`CGR_HOME` directories do not exclude each other, so give users who sync one
checkout into one graph a shared `CGR_HOME`, or sync it from one account.

A lock file the current user can read but not write, on a read-only
`CGR_HOME` or one another user of a shared `CGR_HOME` created, is locked
through a read-only handle, so it still keeps a second sync out. The holder's
name is not recorded then, so a refused sync may name an earlier one. When
there is no lock file and `CGR_HOME` cannot be written to create one, the
sync still runs, as it does without its incremental state, but without the
lock: it logs a warning that concurrent syncs of the checkout are not
excluded. Point `CGR_HOME` at a writable directory to keep the lock.

The lock file is never opened through a symbolic link: a link at its path is
refused with a message naming it, since writing the holder's name through the
link would overwrite whatever file it points at. A lock file the current user
cannot open at all, not even to read it, is refused too, with the reason:
without the lock a sync cannot tell whether another one is running. Make the
file readable and writable for every user who syncs the checkout. A
`.cgr-sync-lock` in the repository itself is not the lock and is left alone.

A write Memgraph rejects as a conflicting transaction is retried a few times
with a short, growing wait before it is reported.

## CLI Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `repo_path` | Yes | | Path to repository to watch |
| `--host` | No | `localhost` | Memgraph host |
| `--port` | No | `7687` | Memgraph port |
| `--batch-size` | No | | Number of buffered nodes/relationships before flushing to Memgraph |
| `--project-name` | No | Same as `cgr start --repo-path` | Project name to store in the graph; give the one you gave `cgr start --project-name` |

## Performance Note

The updater batches rapid saves with a debounce window. Each processed change
runs `GraphUpdater.reingest`, which deletes what the file previously
contributed, re-parses it together with the files that import or call into it
(one level deep, found through the graph's own edges), resolves calls within
that set only, and restores every other inbound edge verbatim. A change in one
file therefore costs the parse of its affected dependents rather than a
re-resolution of the project (a hub file imported by dozens still pays for each
of them); see [Scoped re-ingest latency](../reports/REINGEST_BENCHMARK.md)
for measured numbers.

An edit that leaves the file's interface as it was skips its dependents
altogether. A comment, a reformatted line, or a change inside a function body
that no other file's call resolution reads all count. The file's Module records
an `interface_fingerprint`: its definitions, signatures, annotations, bases and
module-level statements, plus the parts of function bodies that cross-file
type inference reads and the functions a body hands to other files as call
arguments (a constructor argument filling a field another file invokes, a
callback forwarded to the function that calls it). When the new source hashes
to the same value, the dependents keep their edges and only the edited file is
re-parsed. A dependent whose edges with the file could not be restored by name
is still re-parsed. The same rule applies to `cgr start --update-graph`.
Python, Java, JavaScript and TypeScript files carry the fingerprint; the
dependents of any other file are re-parsed as before, as they are when a
capture group beyond the default ones is enabled or a semantic frontend (Jedi,
javac) is selected.

Agents that edit files directly can call the MCP `reingest` tool with the paths
they touched instead of waiting for the watcher or running `update_repository`.
