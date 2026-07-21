---
description: "Index several repositories into one knowledge graph and query across them, including microservice architectures."
---

# Multi-Project Graphs

One Memgraph instance can hold the graphs of several repositories at once.
Each indexed repository becomes a `Project` node, every qualified name is
prefixed with that project's name, and retrieval tools read source files
through each node's recorded absolute path, so answers stay correct no
matter which directory you launch from.

## Indexing several repositories

Index each repository separately; every run adds (or refreshes) one project
in the shared graph:

```bash
cgr start --repo-path ~/services/user-service --update-graph
cgr start --repo-path ~/services/order-service --update-graph
```

Project names are derived from the directory name plus a short hash of the
full path (for example `user-service__a1b2c3d4`), so two checkouts with the
same folder name never overwrite each other. Pass `--project-name` to choose
a name yourself.

Each `Project` node records the repository root it was indexed from
(`root_path`), and every code node stores the absolute path of its source
file, which is what allows cross-project retrieval from any working
directory.

## Querying across projects

`cgr start` scopes queries to the selected repository by default. To query
several projects in one session:

```bash
# Explicit list of project names
cgr start --repo-path ~/services/user-service --projects "user-service__a1b2c3d4,order-service__e5f6a7b8"

# Or a saved workspace
cgr workspace create backend
cgr workspace add-repo backend ~/services/user-service
cgr workspace add-repo backend ~/services/order-service
cgr start --workspace backend
```

`--projects` overrides `--project-name`; `--workspace` expands to every
repository saved in the workspace. See `cgr help workspace` for the full
workspace command set.

## Semantic search within one project

When several projects share the graph, semantic search can be confined to a
single project: the agent's `semantic_search` tool accepts an optional
project name and then only returns matches whose qualified names belong to
that project. Without it, results are ranked across every indexed project.

## Housekeeping

```bash
# Remove one project without touching the others
cgr delete-project --name user-service__a1b2c3d4
```

Deleting a project also removes its embeddings from the vector store.
