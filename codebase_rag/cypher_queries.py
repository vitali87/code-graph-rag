"""Cypher query literals.

Many of these are f-strings, so that a label set derived once from the
`constants.graph` enums (`_SNIPPET_LABELS`, `_GLOSS`, ... -- defined below,
from those enums) is interpolated rather than respelled per query. **In an
f-string query every literal brace must be doubled**: `{{qualified_name:
$qn}}`, not `{qualified_name: $qn}`.

A missed doubling in Cypher's map syntax is loud, because the colon is parsed
as a format spec: `{name: $x}` raises `NameError` at import, and `{id: $x}`
(a builtin key) raises `TypeError`. The quiet case is a COLON-FREE brace whose
contents resolve as a name -- usually a builtin, which is always in scope
whatever this module defines. `f"RETURN {id}"` yields `RETURN <built-in
function id>`, and Cypher's own quantifier is the shape that would bite here:
`f"-[:CALLS*{2}]->"` yields `-[:CALLS*2]->`.

Non-f-string literals split two ways. In a plain string a brace is inert; in a
`.format()` template it is not, and a stray brace raises `KeyError` at CALL
time rather than at import -- quieter still. The templates in THIS file are
`CYPHER_AUDIT_MISSING_REQUIRED` and `CYPHER_AUDIT_IS_NULL`, both formatted in
`graph_audit.py`. (`constants.graph` holds another,
`CYPHER_MEMORY_LIMIT_SUFFIX`, formatted in `graph_dialects.py`; the same rule
applies there.)
"""

import re
from collections.abc import Iterable

from .constants import (
    ANCHOR_HASH_VERSION,
    CYPHER_DEFAULT_LIMIT,
    DEFINITION_NODE_LABELS,
    KEY_FROM_MISSING,
    KEY_FROM_VAL,
    KEY_ROW_INDEX,
    KEY_TO_MISSING,
    KEY_TO_VAL,
    NODE_UNIQUE_CONSTRAINTS,
    SNIPPET_NODE_LABELS,
    GlossAnchorState,
    NodeLabel,
    RelationshipType,
    UniqueKeyType,
)

CYPHER_DELETE_ALL = "MATCH (n) DETACH DELETE n;"

# Graph structural integrity audit (issue #646). A zero-degree Project is a
# valid empty-repo graph, so the orphan scan exempts it. OPTIONAL MATCH +
# count instead of `WHERE NOT (n)--()`: Memgraph 3.x rejects pattern
# expressions inside WHERE, and this form is accepted by both 2.x and 3.x
# (issue #1257).
# `:IncompleteRun` is the CLI's own sync marker, unattached to any project
# tree on purpose (see CYPHER_MARK_PROJECT_INCOMPLETE); doctor reports it as
# an interrupted sync, not as an orphan (issue #2394).
CYPHER_AUDIT_ORPHANS = (
    "MATCH (n) WHERE NOT n:Project AND NOT n:IncompleteRun "
    "OPTIONAL MATCH (n)--(x) "
    "WITH n, count(x) AS degree "
    "WHERE degree = 0 "
    "RETURN labels(n)[0] AS label, count(n) AS orphans"
)
CYPHER_AUDIT_LABELS = "MATCH (n) UNWIND labels(n) AS label RETURN DISTINCT label"
CYPHER_AUDIT_REL_TRIPLES = (
    "MATCH (a)-[r]->(b) "
    "RETURN DISTINCT labels(a)[0] AS src, type(r) AS rel, labels(b)[0] AS dst"
)
CYPHER_AUDIT_LABEL_PROPS = (
    "MATCH (n) UNWIND labels(n) AS label UNWIND keys(n) AS key "
    "RETURN DISTINCT label AS label, key AS key"
)
CYPHER_AUDIT_MISSING_REQUIRED = (
    "MATCH (n:{label}) WHERE {conditions} RETURN count(n) AS missing"
)
CYPHER_AUDIT_IS_NULL = "n.{prop} IS NULL"
CYPHER_AUDIT_OR = " OR "

CYPHER_LIST_PROJECTS = (
    "MATCH (p:Project) RETURN p.name AS name, p.root_path AS root_path ORDER BY p.name"
)
# The repository a graph project was indexed from (issue #1523): source is
# read from disk only when that root IS the local repository, never on the
# strength of a matching project name alone.
CYPHER_PROJECT_ROOT_PATH = (
    "MATCH (p:Project {name: $project_name}) RETURN p.root_path AS root_path"
)
# What the project's last sync captured (issue #2521), with the root the
# re-index hint names.
CYPHER_PROJECT_CAPTURE = (
    "MATCH (p:Project {name: $project_name}) "
    "RETURN p.captured_relationships AS captured_relationships, "
    "p.root_path AS root_path"
)
# A sync's claim on a project name (issue #2411): one statement, so two syncs
# of the same unowned name cannot both read "no owner" before either writes.
# The first claim records its root; every later one, this repository's own
# included, gets back the root already stored and compares it with its own.
# `created` says this claim made the Project, so only it may release it.
CYPHER_CLAIM_PROJECT_ROOT = (
    "OPTIONAL MATCH (held:Project {name: $project_name}) "
    "WITH count(held) AS holders "
    "MERGE (p:Project {name: $project_name}) "
    "ON CREATE SET p.root_path = $root_path "
    "RETURN p.root_path AS root_path, holders = 0 AS created"
)
# The marker write of a sync that holds a claim (review of PR 2499). Another
# sync of this repository may have created the claim, failed and released it
# since this sync's check, and a different repository claimed the name. So
# the marker goes down only while the Project's root is still one the check
# accepted, read in this same statement; a Project released meanwhile is
# claimed again here. Setting the name to itself writes the Project, so a
# release at the same moment conflicts with this write instead of deleting
# the node out from under the marker. It records the sync's owner as
# `CYPHER_MARK_CLI_SYNC_INCOMPLETE` does (#2444), in the same statement.
_CLI_SYNC_MARKER_OWNER_SET = (
    "m.owner_host = $owner_host, m.owner_pid = $owner_pid, m.owner_token = $owner_token"
)
CYPHER_MARK_CLAIMED_PROJECT_INCOMPLETE = (
    "MERGE (p:Project {name: $project_name}) "
    "ON CREATE SET p.root_path = $root_path "
    "WITH p WHERE p.root_path IN $root_paths "
    "SET p.name = p.name "
    "MERGE (m:IncompleteRun {project: $project_name, run_id: $run_id}) "
    "SET m.run_incomplete = true, "
    "m.writing = coalesce(m.writing, false) OR $writing, "
    + _CLI_SYNC_MARKER_OWNER_SET
    + " RETURN p.root_path AS root_path"
)
# Gives back a claim whose first sync failed before writing anything (review
# of PR 2499): a bare Project would refuse every other repository the name.
# Kept once the sync has put down its incomplete-run marker or any node hangs
# off the Project, since a partial graph is then this repository's to finish.
CYPHER_RELEASE_PROJECT_CLAIM = (
    "MATCH (p:Project {name: $project_name, root_path: $root_path}) "
    "OPTIONAL MATCH (p)--(linked) "
    "WITH p, count(linked) AS links "
    "OPTIONAL MATCH (m:IncompleteRun {project: $project_name}) "
    "WITH p, links, count(m) AS markers "
    "WHERE links = 0 AND markers = 0 "
    "DELETE p"
)

# The incomplete-run marker (issue #1679). `_graph_incomplete` on the tools
# registry only ever covered the process that ran the failed update: a crash or
# an MCP restart left a fresh registry seeing a project that looks whole, so it
# would hydrate a scoped updater from the partial graph and treat its missing
# definitions as authoritative. Persisting the flag means the refusal survives
# the process that earned it.
#
# It lives on its OWN node, not as a property of the Project, for two reasons
# found in review:
#
# 1. `MERGE` on a dedicated label works before any Project exists. A first
#    index creates the Project inside `GraphUpdater.run()`, so a `MATCH
#    (p:Project ...)` set beforehand updated ZERO rows and a failed first run
#    left no marker at all -- exactly the case the guard exists for.
# 2. `CYPHER_DELETE_PROJECT` detaches and deletes the Project and everything
#    it CONTAINS, so a marker stored on the Project would be deleted by the
#    very rebuild whose failure it is meant to record. An unconnected
#    `:IncompleteRun` node is not reachable from that traversal and survives.
#
# Cleared by DELETING the node rather than setting false, so a project indexed
# before this existed reads exactly like one that completed: absent means
# complete, and no migration is needed.
#
# `writing` is the marker's PHASE. Every mutating path marks before anything
# destructive, but some of them then do read-only or graph-external work first
# (a reingest's prologue, the embedding purge before an index or delete) and
# can stop there -- an abort, a crash, a cleanup failure -- with the graph
# exactly as it was. A marker from such a run says `writing = false`, and a
# fresh process may clear it and carry on; one that reached its first graph
# write says `writing = true` and refuses scoped work until a full update
# recovers the graph (#1705 review). An absent property reads as `true`: a
# marker written before the phase existed is treated the fail-closed way.
# The phase only ever advances: a mark with `writing=false` must not demote a
# marker another process left at `writing=true` over a graph it had started to
# change, or that process's abort would clear a guard it does not own (#1705
# review). Ownership of the DELETE is the remaining half, tracked in #1709.
# One marker node PER RUN, not per project (issue #1709). The ingestor lock is
# per registry instance, so two registries can index the same project at once;
# with a single node per project, the second run's clear deleted the first
# run's marker and a partial graph looked complete to a fresh process.
CYPHER_MARK_PROJECT_INCOMPLETE = (
    "MERGE (m:IncompleteRun {project: $project_name, run_id: $run_id}) "
    "SET m.run_incomplete = true, "
    "m.writing = coalesce(m.writing, false) OR $writing"
)
# Deletes only THIS run's marker, so a concurrent run's marker outlives it.
CYPHER_CLEAR_PROJECT_INCOMPLETE = (
    "MATCH (m:IncompleteRun {project: $project_name, run_id: $run_id}) DELETE m"
)
# The CLI sync's mark also records WHO holds it (PR #2532 review): every CLI
# sync of a project shares one run id, so the run id cannot say whether the
# sync that left the marker still runs. `delete-project` reads the owner and
# clears the marker only once that sync provably stopped. Set in the same
# statement as the mark, so a marker never carries a stopped sync's owner
# after a running sync has re-marked it.
CYPHER_MARK_CLI_SYNC_INCOMPLETE = (
    CYPHER_MARK_PROJECT_INCOMPLETE + ", " + _CLI_SYNC_MARKER_OWNER_SET
)
# The MCP server's runs and `cgr check` name their process the same way
# (review of PR #3496), so a full rebuild can tell a run whose process exited
# from one still writing. No token: each of these runs has an id of its own.
CYPHER_MARK_RUN_INCOMPLETE = (
    CYPHER_MARK_PROJECT_INCOMPLETE
    + ", m.owner_host = $owner_host, m.owner_pid = $owner_pid"
)
CYPHER_CLI_SYNC_MARKER_OWNER = (
    "MATCH (m:IncompleteRun {project: $project_name, run_id: $run_id}) "
    "RETURN m.owner_host AS owner_host, m.owner_pid AS owner_pid, "
    "m.owner_token AS owner_token"
)
# Compare-and-delete on the owner token read above: a sync that started
# after that read re-marked the node under its own token and keeps it. `cgr
# watch` takes its own marker off the same way, so a marker another writer
# has put down since its mark is left to that writer (review of PR 3365).
CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER = (
    "MATCH (m:IncompleteRun {project: $project_name, run_id: $run_id}) "
    "WHERE m.owner_token = $owner_token DELETE m"
)
# RECOVERY: clears every outstanding marker for the project, whichever run
# wrote it. Deliberately not run-scoped -- its whole purpose is to clear a
# marker some OTHER run stranded (a run that stopped before its first graph
# write and could not, or did not live to, clear its own). A run-scoped
# delete matches nothing there, and the project stays blocked until someone
# runs a full update. Guarded by the caller on `writing=false` for EVERY
# outstanding marker, so no run that has begun writing can be cleared this
# way (issue #1709).
# Deletes only markers that are STILL read-only at delete time. The caller
# reads the phase and then acts on it, and between those a concurrent run can
# promote its own marker to `writing=true` -- an unconditional delete would
# then remove a marker protecting a graph that IS being written (raised in
# review of #1850). Re-checking the phase inside the delete closes that
# window: the predicate is evaluated against the row as it is now, not as the
# caller last saw it. A legacy marker with no `writing` property reads as
# writing, exactly as `CYPHER_PROJECT_IS_INCOMPLETE` coalesces it, so it stays
# fail-closed here too.
CYPHER_RECOVER_PROJECT_INCOMPLETE = (
    "MATCH (m:IncompleteRun {project: $project_name}) "
    "WHERE coalesce(m.writing, true) = false DELETE m"
)
# A marker written before run ids existed has no `run_id` property, so the
# run-scoped clear can never match it while the read -- which matches on
# project alone -- still sees it. Without this the project would stay blocked
# forever after an upgrade (raised in review of #1850). Cleared alongside this
# run's own marker on a successful completion, since a run that completes for
# the project has established the graph is whole.
#
# Phase-guarded for the ROLLING DEPLOYMENT case: during an upgrade an
# old-version process can still be writing under a legacy marker, and a
# new-version run completing for the same project would otherwise delete the
# protection for a graph that IS being written (#1850 review). A legacy
# marker with no `writing` property coalesces to true and is left alone, so
# the pre-phase markers this was written for stay put until a run can prove
# they are stale -- fail-closed, which is the safe direction here.
CYPHER_CLEAR_LEGACY_PROJECT_INCOMPLETE = (
    "MATCH (m:IncompleteRun {project: $project_name}) "
    "WHERE m.run_id IS NULL AND coalesce(m.writing, true) = false DELETE m"
)
# "Is ANY run outstanding": the read was already a boolean question, so it
# generalises without changing its callers' meaning. `writing` is true if ANY
# outstanding run has begun writing, which is the conservative reading -- a
# recoverable `writing=false` state requires every outstanding run to be
# read-only still.
CYPHER_PROJECT_IS_INCOMPLETE = (
    "MATCH (m:IncompleteRun {project: $project_name}) "
    "RETURN coalesce(m.run_incomplete, false) AS run_incomplete, "
    "coalesce(m.writing, true) AS writing"
)
# The runs a full rebuild of the project supersedes (review of PR #3493): no
# other path removes a marker its run left after its first write, so one
# whose process died stayed down for good. Read before the rebuild marks,
# with the process each marker names: `sync_guard.stranded_runs` keeps only
# the runs whose process provably exited, since the checkout's sync lock
# keeps out no writer of another checkout or CGR_HOME under the same project
# name (review of PR #3496). A marker from before run ids existed is left
# alone, as `CYPHER_CLEAR_LEGACY_PROJECT_INCOMPLETE` leaves it.
CYPHER_STRANDED_RUN_IDS = (
    "MATCH (m:IncompleteRun {project: $project_name}) "
    "WHERE m.run_id IS NOT NULL AND m.run_id <> $run_id "
    "AND coalesce(m.writing, true) "
    "RETURN m.run_id AS run_id, m.owner_host AS owner_host, "
    "m.owner_pid AS owner_pid"
)
# Deletes one run `CYPHER_STRANDED_RUN_IDS` returned, once the forced rebuild
# that supersedes it has committed and cleared its own marker, and only while
# the marker still names the exited process: the CLI sync's run id is shared,
# so a CLI writer that started since has marked it under its own.
CYPHER_RETIRE_STRANDED_RUN = (
    "MATCH (m:IncompleteRun {project: $project_name, run_id: $run_id}) "
    "WHERE m.owner_host = $owner_host AND m.owner_pid = $owner_pid DELETE m"
)

# Every project with an outstanding marker, whichever path wrote it: `cgr
# status` reports them so a sync that was interrupted is visible there, not
# only to the next MCP process that trips over it (issue #2219).
CYPHER_PROJECTS_WITH_INCOMPLETE_RUNS = (
    "MATCH (m:IncompleteRun) RETURN DISTINCT m.project AS project"
)

_CONTAINMENT_RELS = (
    "CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE|CONTAINS_SECTION"
)
_CONTAINMENT_REL_LIST = (
    "['CONTAINS_PACKAGE', 'CONTAINS_FOLDER', 'CONTAINS_FILE', "
    "'CONTAINS_MODULE', 'CONTAINS_SECTION']"
)
# The definition walk takes what `CYPHER_DELETE_MODULE` takes from each
# module (a test pins that), the finding edges included: a Pattern,
# CodeSmell or SecurityIssue is keyed under its module's qualified name, so
# no other project holds it, and left out it outlived the project as an
# orphan (issue #2536).
_DEFINITION_RELS = (
    "DEFINES|DEFINES_METHOD|HAS_PARAMETER|HAS_FIELD|HAS_VARIANT|DEFINES_CONSTANT|"
    "IMPLEMENTS_PATTERN|HAS_SMELL|HAS_VULNERABILITY"
)


def _belongs_to_p(var: str) -> str:
    # A qualified name is `p`'s when it is `p` or sits under `p.`, but not
    # under a longer registered name such as `p.v2` (issue #1985): the rule
    # CYPHER_DELETE_MODULE applies. `nested` is bound to those longer names.
    return (
        f"({var}.qualified_name = p.name "
        f"OR {var}.qualified_name STARTS WITH (p.name + '.')) "
        f"AND NOT any(q IN nested WHERE {var}.qualified_name = q "
        f"OR {var}.qualified_name STARTS WITH (q + '.'))"
    )


def _belongs_to_named(var: str) -> str:
    # _belongs_to_p for a project given by `$project_name`/`$project_prefix`
    # rather than a bound Project node.
    return (
        f"({var}.qualified_name = $project_name "
        f"OR {var}.qualified_name STARTS WITH $project_prefix) "
        f"AND NOT any(q IN nested WHERE {var}.qualified_name = q "
        f"OR {var}.qualified_name STARTS WITH (q + '.'))"
    )


# Folder and File carry no qualified name: they are keyed by absolute_path,
# so every project indexed from one directory MERGEs onto the same nodes
# (issue #2665). Only these two edge types lead to them, and from them a walk
# reaches no other kind of node.
_SHARED_ENTRY_RELS = "CONTAINS_FOLDER|CONTAINS_FILE"


# The nodes project `p` claims: those a containment path from `p` reaches
# without passing through another project's Package, Module or Section, plus
# what they define. Walking every containment path instead let two projects
# over one directory reach each other's packages through their shared `src`
# Folder, so each deleted and counted the other (issue #2665). Such a path
# runs through p's own qualified nodes and shared Folders/Files, so it is
# taken in two walks rather than one path tested node by node, which the
# embedded engine cannot express: p's own Packages, Modules and Sections that
# p reaches, then the Folders and Files reached from p or one of those
# through Folders and Files alone. Binds `p`, `nested` and the per-project
# list `owned`.
_CYPHER_PROJECT_CLAIMS = f"""
OPTIONAL MATCH (longer:Project) WHERE longer.name STARTS WITH (p.name + '.')
WITH p, collect(longer.name) AS nested
OPTIONAL MATCH (p)-[:{_CONTAINMENT_RELS}*]->(unit) WHERE {_belongs_to_p("unit")}
WITH p, nested, collect(DISTINCT unit) AS units
UNWIND [p] + units AS holder
OPTIONAL MATCH (holder)-[:{_SHARED_ENTRY_RELS}*]->(entry)
WITH p, nested, collect(DISTINCT holder) + collect(DISTINCT entry) AS containers
UNWIND containers AS container
OPTIONAL MATCH (container)-[:{_DEFINITION_RELS}*]->(defined)
WITH p, nested, collect(DISTINCT container) + collect(DISTINCT defined) AS owned
"""

# When a sync of the project last completed (issue #2444). On the Project node
# so the time lives exactly as long as the project does in THIS graph:
# `delete-project` and `--clean` take it with the node, and a project synced
# into another Memgraph is simply not here. The client-side log `cgr status`
# read before could reconcile none of that.
CYPHER_RECORD_PROJECT_SYNC = (
    "MATCH (p:Project {name: $project_name}) SET p.last_synced_at = $last_synced_at"
)
# A project synced before the time was recorded reads back null.
CYPHER_PROJECT_SYNC_TIMES = (
    "MATCH (p:Project) "
    "RETURN p.name AS name, p.last_synced_at AS last_synced_at ORDER BY p.name"
)

# Retires a project whose checkout was just re-indexed under another name
# (issue #2412). Both projects index the same files, so they share every
# Folder and File node (keyed on absolute path), and a walk over every
# containment path would cross those into the new project's modules. Only
# containers carrying the old project's qualified name go, with what they
# define; the shared Folder and File nodes stay with the project that still
# contains them. A repository-root `__init__.py` makes the root Package and
# Module's qn the bare project name, so the prefix test alone would miss them
# (review of PR 2497); the trailing dot of the prefix still keeps a project
# whose name only starts with this one (`acme.webapp` beside `acme.web`).
CYPHER_RETIRE_PROJECT = """
MATCH (p:Project {name: $project_name})
OPTIONAL MATCH (p)-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE|CONTAINS_SECTION*]->(container)
WHERE container.qualified_name = $project_name
   OR container.qualified_name STARTS WITH $project_prefix
OPTIONAL MATCH (container)-[:DEFINES|DEFINES_METHOD|HAS_PARAMETER|HAS_FIELD|HAS_VARIANT*]->(defined)
DETACH DELETE p, container, defined
"""

# Deletes what `p` claims, except a Folder or File another project claims
# too: that one only loses `p`'s edges. The named nodes go first, so the
# check that follows walks up from each Folder/File over what is left; no
# large list is ever a grouping key, which cost seconds per thousand nodes.
# Another project `q` claims a Folder/File it reaches through Folders and
# Files from itself or from one of its own Packages, the same two walks as
# _CYPHER_PROJECT_CLAIMS taken upward.
CYPHER_DELETE_PROJECT = f"""
MATCH (p:Project {{name: $project_name}})
{_CYPHER_PROJECT_CLAIMS}
WITH [c IN owned WHERE c:Folder OR c:File] AS shareable,
[c IN owned WHERE NOT (c:Folder OR c:File)] AS named
FOREACH (n IN named | DETACH DELETE n)
WITH shareable
UNWIND shareable AS c
OPTIONAL MATCH (c)<-[:{_SHARED_ENTRY_RELS}*]-(holder)
WHERE NOT (holder:Folder OR holder:File)
OPTIONAL MATCH (holder)<-[:{_CONTAINMENT_RELS}*0..]-(q:Project)
WHERE q.name <> $project_name
AND (holder:Project OR holder.qualified_name = q.name
OR holder.qualified_name STARTS WITH (q.name + '.'))
WITH c, count(q) AS other_claims
WHERE other_claims = 0
DETACH DELETE c
"""

# Run after CYPHER_DELETE_PROJECT: the project's Packages and Modules that no
# containment path from it reached, such as a tree the #897 repair cut off
# from its Project, go too rather than outliving it as orphans (issue
# #2665). Modules always carry the project prefix, the fact
# CYPHER_DELETE_MODULE relies on. Glosses are keyed by qualified name as
# well, but are neither label and hang off nothing this walks, so they
# survive the rebuild they are meant to survive (issue #1828).
CYPHER_DELETE_PROJECT_CUT_OFF_UNITS = f"""
OPTIONAL MATCH (longer:Project) WHERE longer.name STARTS WITH $project_prefix
WITH collect(longer.name) AS nested
OPTIONAL MATCH (pkg:Package) WHERE {_belongs_to_named("pkg")}
WITH nested, collect(pkg) AS packages
OPTIONAL MATCH (mod:Module) WHERE {_belongs_to_named("mod")}
WITH packages, collect(mod) AS modules
WITH packages + modules AS units
UNWIND units AS u
OPTIONAL MATCH (u)-[:{_DEFINITION_RELS}|CONTAINS_SECTION*]->(defined)
WITH collect(DISTINCT u) + collect(DISTINCT defined) AS gone
FOREACH (n IN gone | DETACH DELETE n)
"""


# Damage detectors for the issue #897 migration. The superseded key merged
# projects in DIFFERENT directories that share a relative layout, so a merged
# node lies outside one owner's tree. The current key (absolute_path) shares a
# node only between projects over the SAME files -- one checkout under two
# names, or a project nested in another -- and the node lies inside every
# owner's tree, so it is not damage (issues #2665, #3025). An owner's tree is its
# Project's root_path; a Project that records none predates it and counts as
# legacy. root_path is the resolved path as the OS spells it, backslashes on
# Windows, while absolute_path is POSIX, so both sides are compared with
# forward slashes (review of PR 3216). A filesystem root (`/`, `C:\`) already
# ends in the separator, so the boundary one is added only to a root that
# does not.
def _outside_tree(root: str) -> str:
    node = "replace(n.absolute_path, '\\\\', '/')"
    tree = f"replace({root}, '\\\\', '/')"
    prefix = f"CASE WHEN {tree} ENDS WITH '/' THEN {tree} ELSE {tree} + '/' END"
    return f"({root} IS NULL OR NOT ({node} = {tree} OR {node} STARTS WITH ({prefix})))"


# The Projects that reach a node, and the node kept when two or more do and
# one of them does not hold it in its tree. The owners themselves are
# collected, not their roots: collect() drops nulls, so an owner that records
# no root would vanish from the list instead of counting as legacy (review of
# PR 3216).
_FROM_OWNERS = (
    "MATCH (p:Project)"
    "-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE*]->(n) "
)
_ACROSS_ROOTS = (
    "WITH n, collect(DISTINCT p) AS owners "
    "WHERE size(owners) > 1 "
    f"AND any(owner IN owners WHERE {_outside_tree('owner.root_path')}) "
)

# Sharing always leaves a single-hop signature: the topmost merged node has
# containment parents in two projects (Project roots are never merged, so the
# parents are distinct nodes). That cheap test runs on every sync and picks
# the candidates; only they are walked up to their Projects and judged as the
# purge judges them. A parent's own path cannot stand in for its Project's
# root: a Package records its directory, so a File two projects reach through
# their Packages read as inside both, hiding a rootless owner the purge counts
# as legacy (review of PR 3216). Keyless rows match the second purge's
# predicate directly.
CYPHER_ANY_SHARED_STRUCTURE = (
    "MATCH (parent)-[:CONTAINS_FOLDER|CONTAINS_FILE]->(n) "
    "WHERE (n:Folder OR n:File) AND n.absolute_path IS NOT NULL "
    "WITH n, count(parent) AS parents "
    "WHERE parents > 1 " + _FROM_OWNERS + _ACROSS_ROOTS + "RETURN 1 AS damaged LIMIT 1"
)

CYPHER_ANY_KEYLESS_STRUCTURE = (
    "MATCH (n) WHERE (n:Folder OR n:File) AND n.absolute_path IS NULL "
    "RETURN 1 AS damaged LIMIT 1"
)

# While a legacy constraint is still in force, every node was written by the
# old key: anything the containment walk reaches from more than one Project is
# a merge, which cannot be split, so it is purged and the next re-index
# rebuilds it with per-project identity.
CYPHER_PURGE_CROSS_PROJECT_STRUCTURE = (
    "MATCH (p:Project)"
    "-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE*]->(n) "
    "WHERE (n:Folder OR n:File) "
    "WITH n, count(DISTINCT p) AS owners "
    "WHERE owners > 1 "
    "DETACH DELETE n RETURN count(n) AS purged"
)

# Once the constraints are gone, current data shares nodes legitimately, so
# only a node outside some owner's tree (or under an owner that records no
# root) is the old key's merge.
CYPHER_PURGE_CROSS_ROOT_STRUCTURE = (
    _FROM_OWNERS
    + "WHERE (n:Folder OR n:File) AND n.absolute_path IS NOT NULL "
    + _ACROSS_ROOTS
    + "DETACH DELETE n RETURN count(n) AS purged"
)

# Rows written before absolute_path existed can never match the current
# delete queries; they are unmanageable and must go with the migration.
CYPHER_PURGE_KEYLESS_STRUCTURE = (
    "MATCH (n) WHERE (n:Folder OR n:File) AND n.absolute_path IS NULL "
    "DETACH DELETE n RETURN count(n) AS purged"
)

CYPHER_EXAMPLE_DECORATED_FUNCTIONS = f"""MATCH (n:Function|Method)
WHERE ANY(d IN n.decorators WHERE toLower(d) IN ['flow', 'task'])
RETURN n.name AS name, n.qualified_name AS qualified_name, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_CONTENT_BY_PATH = f"""MATCH (n)
WHERE n.path IS NOT NULL AND n.path STARTS WITH 'workflows'
RETURN n.name AS name, n.path AS path, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_KEYWORD_SEARCH = f"""MATCH (n)
WHERE toLower(n.name) CONTAINS 'database' OR (n.qualified_name IS NOT NULL AND toLower(n.qualified_name) CONTAINS 'database')
RETURN n.name AS name, n.qualified_name AS qualified_name, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_FIND_FILE = """MATCH (f:File) WHERE toLower(f.name) = 'readme.md' AND f.path = 'README.md'
RETURN f.path as path, f.name as name, labels(f) as type"""

CYPHER_EXAMPLE_README = f"""MATCH (f:File)
WHERE toLower(f.name) CONTAINS 'readme'
RETURN f.path AS path, f.name AS name, labels(f) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_PYTHON_FILES = f"""MATCH (f:File)
WHERE f.extension = '.py'
RETURN f.path AS path, f.name AS name, labels(f) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_TASKS = f"""MATCH (n:Function|Method)
WHERE 'task' IN n.decorators
RETURN n.qualified_name AS qualified_name, n.name AS name, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_FILES_IN_FOLDER = f"""MATCH (f:File)
WHERE f.path STARTS WITH 'services'
RETURN f.path AS path, f.name AS name, labels(f) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_LIMIT_ONE = """MATCH (f:File) RETURN f.path as path, f.name as name, labels(f) as type LIMIT 1"""

CYPHER_EXAMPLE_PROJECT_SCOPED = f"""MATCH (c:Class)
WHERE c.qualified_name STARTS WITH 'myproject.'
RETURN c.name AS name, c.qualified_name AS qualified_name, labels(c) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_CLASS_METHODS = f"""MATCH (c:Class)-[:DEFINES_METHOD]->(m:Method)
WHERE c.name = 'UserService'
RETURN c.name AS className, m.name AS methodName, m.qualified_name AS qualified_name, labels(m) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_FUNCTION_CALLERS = f"""MATCH (caller)-[r:CALLS]->(callee:Function|Method)
WHERE callee.qualified_name ENDS WITH '.process_payment'
RETURN caller.qualified_name AS caller_qualified_name, caller.path AS path,
       callee.qualified_name AS callee_qualified_name,
       type(r) AS relationship, labels(caller) AS caller_type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

# ast-grep findings (issue #413): Pattern/CodeSmell/SecurityIssue nodes hang
# off a Module via IMPLEMENTS_PATTERN/HAS_SMELL/HAS_VULNERABILITY. The finding
# node's name is the rule id; start_line locates the site.
CYPHER_EXAMPLE_FIND_PATTERN = f"""MATCH (m:Module)-[:IMPLEMENTS_PATTERN]->(p:Pattern)
WHERE p.name = 'singleton'
RETURN m.path AS path, p.name AS pattern, p.start_line AS line, p.message AS message
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_SECURITY_ISSUES = f"""MATCH (m:Module)-[:HAS_VULNERABILITY]->(s:SecurityIssue)
RETURN m.path AS path, s.name AS rule, s.start_line AS line, s.message AS message
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_CODE_SMELLS = f"""MATCH (m:Module)-[:HAS_SMELL]->(c:CodeSmell)
RETURN m.path AS path, c.name AS smell, c.start_line AS line, c.message AS message
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXPORT_NODES = """
MATCH (n)
RETURN id(n) as node_id, labels(n) as labels, properties(n) as properties
"""

CYPHER_EXPORT_RELATIONSHIPS = """
MATCH (a)-[r]->(b)
RETURN id(a) as from_id, id(b) as to_id, type(r) as type, properties(r) as properties
"""

# What a project owns is what deleting it removes: its containment tree and
# everything the containers define (the walk of CYPHER_DELETE_PROJECT). Walking
# from the Project node rather than matching a qualified-name prefix keeps
# `foo.bar`'s nodes out of `foo`, and takes File and Folder nodes, which have
# no qualified name. `cgr stats -n` (#2391) counts the same set.
_CYPHER_PROJECT_OWNED_NODES = """
MATCH (p:Project) WHERE p.name IN $project_names
OPTIONAL MATCH (p)-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE|CONTAINS_SECTION*]->(container)
OPTIONAL MATCH (container)-[:DEFINES|DEFINES_METHOD|HAS_PARAMETER|HAS_FIELD|HAS_VARIANT|DEFINES_CONSTANT|IMPLEMENTS_PATTERN|HAS_SMELL|HAS_VULNERABILITY*]->(defined)
WITH collect(DISTINCT p) + collect(DISTINCT container) + collect(DISTINCT defined) AS owned
UNWIND owned AS n
WITH DISTINCT n
"""
# A project's export is what it owns, the relationships that start there, and
# the nodes those relationships reach (a shared ExternalModule, another
# project's callee), so every relationship in the file has both of its ends
# in it (issue #2410). A Resource is never owned, so the links between
# resources (a client URL's RESOLVES_TO, an env var's FLOWS_TO into a URL)
# start outside the owned set; each one whose two ends are both exported is
# added, or a file holding a client and its server showed two services that
# never call each other (issue #3189). Another project's node reached here
# stays a leaf: nothing starting at it is added.
CYPHER_EXPORT_PROJECT_NODES = (
    _CYPHER_PROJECT_OWNED_NODES
    + """OPTIONAL MATCH (n)-->(reached)
WITH collect(DISTINCT n) + collect(DISTINCT reached) AS exported
UNWIND exported AS node
WITH DISTINCT node
RETURN id(node) as node_id, labels(node) as labels, properties(node) as properties
"""
)
CYPHER_EXPORT_PROJECT_RELATIONSHIPS = (
    _CYPHER_PROJECT_OWNED_NODES
    + """WITH collect(n) AS owned
UNWIND owned AS n
OPTIONAL MATCH (n)-->(reached)
WITH owned, collect(DISTINCT reached) AS reached
WITH owned, owned + reached AS exported, [x IN reached WHERE x:Resource] AS resources
UNWIND owned + resources AS a
MATCH (a)-[r]->(b)
WHERE NOT a:Resource OR b IN exported
RETURN id(a) as from_id, id(b) as to_id, type(r) as type, properties(r) as properties
"""
)

# Rows written, not edges matched: a MERGE without the per-site keys matches
# every parallel edge between its endpoints, so `count(r)` let one row count
# several times and a flush report more writes than rows (issue #2879). A row
# without its ordinal (one built outside the flush) counts as itself.
CYPHER_RETURN_COUNT = (
    f"RETURN count(DISTINCT coalesce(row.{KEY_ROW_INDEX}, row)) as created"
)
CYPHER_SET_PROPS_RETURN_COUNT = f"SET r += row.props\n{CYPHER_RETURN_COUNT}"

CYPHER_GET_FUNCTION_SOURCE_LOCATION = """
MATCH (m:Module)-[:DEFINES]->(n)
WHERE id(n) = $node_id
RETURN n.qualified_name AS qualified_name, n.start_line AS start_line,
       n.end_line AS end_line, m.path AS path, n.absolute_path AS absolute_path
"""

# One source for every definition lookup (issue #1925): see
# DEFINITION_NODE_LABELS. Sorted so the generated Cypher is stable across runs
# rather than varying with frozenset iteration order.
_GRAPH_DEFINITION_LABELS = "|".join(
    sorted(label.value for label in DEFINITION_NODE_LABELS)
)
_SNIPPET_LABELS = "|".join(sorted(label.value for label in SNIPPET_NODE_LABELS))
# The quote tier's candidates. A Module is a definition label but never
# carries an `anchor_hash`, so the quote move could never bind one: a sole
# module match would hold its note unattached on every pass instead of
# grading it LOST (Copilot, PR #1966).
_QUOTE_SPAN_LABELS = "|".join(
    sorted(
        label.value for label in DEFINITION_NODE_LABELS if label is not NodeLabel.MODULE
    )
)


def _any_label(var: str, labels: str) -> str:
    """`(var:A OR var:B ...)` for a `|`-joined label list.

    The label-alternation pattern `(n:A|B)` is shorter, but openCypher
    engines without it reject the whole query, so the queries cgr runs
    itself test labels in WHERE instead. On Memgraph 3.3 over a 38k-node
    graph the two spellings plan the same index seeks for a key lookup and
    return the same rows, with scans within a few percent either way in
    interleaved runs; the embedded graph reads these labels back for its own
    seeks (issue #386). Only for a list of two or more labels: a single
    label stays in the pattern, where it picks the label scan. The prompt's
    example queries keep the pattern form the model is taught to prefer.
    """
    return "(" + " OR ".join(f"{var}:{label}" for label in labels.split("|")) + ")"


# A lookup keyed on qualified_name alone can match more than one node: identity
# constraints are label-scoped, so a Method and a Field may share `<owner>.<name>`.
# LIMIT 1 without an ORDER BY then picks by storage order. Ordering by label and
# path makes the pick total and stable across a re-index, since qualified_name
# ties by definition in exactly the ambiguous case (issue #1925).
_DEFINITION_TIEBREAK = "ORDER BY labels(n)[0], n.path, n.start_line"

# Fields and Parameters share the `<owner>.<name>` key space with Methods (a
# Python class may have a field and a method both called `total`), and a Field
# row carries no `end`, so one winning here made an indexed definition read as
# not found. Matched against the definition allowlist rather than excluding
# those two labels by name, which fails open as labels are added (issue #1925).
#
# The Module fallback serves a node with no `path` of its own, and walks only
# the edges that CONTAIN a definition, towards it. An untyped `-[*]-` expanded
# through CALLS, IMPORTS and the rest of the graph before LIMIT applied, so a
# lookup cost grew with the whole project: a node with 814 callers exhausted
# Memgraph's 4 GiB query memory (issue #2196). It also reached the CALLER's
# Module and reported that file as the callee's. The bound covers the deepest
# nesting the parsers emit (Module > Class > Method > nested Function, or six
# levels of Markdown sections) with room to spare.
SNIPPET_CONTAINMENT_RELATIONSHIPS: tuple[RelationshipType, ...] = (
    RelationshipType.DEFINES,
    RelationshipType.DEFINES_METHOD,
    RelationshipType.CONTAINS_SECTION,
    RelationshipType.IMPLEMENTS_PATTERN,
    RelationshipType.HAS_SMELL,
    RelationshipType.HAS_VULNERABILITY,
)
SNIPPET_MODULE_FALLBACK_MAX_DEPTH = 16
_SNIPPET_CONTAINMENT = "|".join(rel.value for rel in SNIPPET_CONTAINMENT_RELATIONSHIPS)

CYPHER_FIND_BY_QUALIFIED_NAME = f"""
MATCH (n) WHERE {_any_label("n", _SNIPPET_LABELS)} AND n.qualified_name = $qn
OPTIONAL MATCH (m:Module)-[:{_SNIPPET_CONTAINMENT}*1..{SNIPPET_MODULE_FALLBACK_MAX_DEPTH}]->(n)
RETURN n.name AS name, n.start_line AS start, n.end_line AS end, coalesce(n.path, m.path) AS path,
       n.absolute_path AS absolute_path, n.docstring AS docstring
ORDER BY labels(n)[0], coalesce(n.path, m.path), n.start_line
LIMIT 1
"""


# Trace-ingestion fetches: every callable (plus Module, for module-level
# callers) of one project, and the statically discovered CALLS pairs so
# runtime-only edges can be flagged as static_missed. Edges a previous trace
# ingestion created (static_missed = true) are excluded, otherwise
# re-ingesting a trace would reclassify its own runtime-only edges as
# statically confirmed.
CYPHER_TRACE_CALLABLES = """
MATCH (n)
WHERE (n:Function OR n:Method OR n:Module)
  AND n.qualified_name STARTS WITH $prefix
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line
"""

CYPHER_TRACE_EXISTING_CALLS = """
MATCH (a)-[r:CALLS]->(b)
WHERE a.qualified_name STARTS WITH $prefix
  AND b.qualified_name STARTS WITH $prefix
  AND coalesce(r.static_missed, false) = false
RETURN a.qualified_name AS from_qn, b.qualified_name AS to_qn
"""

# Trace-derived CALLS edges touching the files a sync is about to delete and
# re-parse (issue #2429). An observation lives only in the graph, so it is
# read here, before the subtrees go, and re-applied by qualified name once
# the re-parse has rebuilt the static edges. Both directions: an edge out of
# a re-parsed file dies with its caller's subtree, and one into it records
# an observation of the callee as it was when traced. The endpoints'
# `anchor_hash` rides along so the carry can tell which definitions changed.
# Anchored on the nodes at the paths rather than on every CALLS edge: this
# runs on every incremental sync, and most syncs touch a handful of files.
CYPHER_TRACE_EDGES_AT_PATHS = """
MATCH (n)-[r:CALLS]-()
WHERE n.path IN $paths
  AND n.qualified_name STARTS WITH $prefix
  AND r.dynamic = true
WITH DISTINCT r
WITH r, startNode(r) AS a, endNode(r) AS b
WHERE a.qualified_name STARTS WITH $prefix
  AND b.qualified_name STARTS WITH $prefix
RETURN labels(a)[0] AS from_label, a.qualified_name AS from_qn,
       a.path AS from_path, a.anchor_hash AS from_hash,
       labels(b)[0] AS to_label, b.qualified_name AS to_qn,
       b.path AS to_path, b.anchor_hash AS to_hash,
       properties(r) AS props
"""

# The re-parsed endpoints of the captured trace edges, in the shape
# CYPHER_TRACE_CALLABLES returns plus the new `anchor_hash`. A name that
# comes back missing is a definition the edit removed or renamed.
CYPHER_TRACE_CARRY_ENDPOINTS = """
MATCH (n)
WHERE (n:Function OR n:Method OR n:Module)
  AND n.qualified_name IN $qns
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line,
       n.anchor_hash AS anchor_hash
"""

# CYPHER_TRACE_EXISTING_CALLS narrowed to the carried edges' endpoints, so a
# sync reads the static edges it may confirm rather than the whole project's.
CYPHER_TRACE_CARRY_STATIC_PAIRS = """
MATCH (a)-[r:CALLS]->(b)
WHERE a.qualified_name IN $from_qns
  AND b.qualified_name IN $to_qns
  AND coalesce(r.static_missed, false) = false
RETURN DISTINCT a.qualified_name AS from_qn, b.qualified_name AS to_qn
"""


CYPHER_STATS_NODE_COUNTS = """
MATCH (n)
RETURN labels(n) AS labels, count(*) AS count
ORDER BY count DESC
"""

CYPHER_STATS_RELATIONSHIP_COUNTS = """
MATCH ()-[r]->()
RETURN type(r) AS type, count(*) AS count
ORDER BY count DESC
"""

# What a project owns is what it claims (_CYPHER_PROJECT_CLAIMS): its
# containment tree and everything the containers define. That covers File
# and Folder nodes, which have no qualified name to match a prefix against,
# and leaves out shared nodes (ExternalPackage, Resource) another project may
# hold too. A Folder/File shared with a project indexed from the same
# directory counts for each of them, so a project's counts do not change
# when a second name is indexed over its checkout (issue #2665).
# Relationships count when they START at an owned node (issue #2391), except
# a containment edge from a shared Folder/File into the other project's tree.
_CYPHER_STATS_OWNED_NODES = (
    """
MATCH (p:Project) WHERE p.name IN $project_names
"""
    + _CYPHER_PROJECT_CLAIMS
    + """UNWIND owned AS n
"""
)
_CYPHER_STATS_EDGE_IS_PS = (
    f"NOT (type(r) IN {_CONTAINMENT_REL_LIST} "
    f"AND m.qualified_name IS NOT NULL AND NOT ({_belongs_to_p('m')}))"
)
CYPHER_STATS_PROJECT_NODE_COUNTS = (
    _CYPHER_STATS_OWNED_NODES
    + """WITH DISTINCT n
RETURN labels(n) AS labels, count(*) AS count
ORDER BY count DESC
"""
)
CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS = (
    _CYPHER_STATS_OWNED_NODES
    + """WITH DISTINCT p, nested, n
MATCH (n)-[r]->(m)
WHERE """
    + _CYPHER_STATS_EDGE_IS_PS
    + """
WITH DISTINCT r
RETURN type(r) AS type, count(r) AS count
ORDER BY count DESC
"""
)
# One row per project, so unscoped totals over a shared graph can be
# attributed; the same ownership walk as above, grouped by project.
CYPHER_STATS_PER_PROJECT = (
    """
MATCH (p:Project)
"""
    + _CYPHER_PROJECT_CLAIMS
    + """UNWIND owned AS n
WITH DISTINCT p, nested, n
OPTIONAL MATCH (n)-[r]->(m)
WHERE """
    + _CYPHER_STATS_EDGE_IS_PS
    + """
WITH p, n, count(r) AS outgoing
RETURN p.name AS project, count(n) AS nodes, sum(outgoing) AS relationships
ORDER BY project
"""
)


# Dead-code fetch queries. Reachability itself runs client-side in
# codebase_rag/dead_code.py: the previous single-query formulation expanded
# *BFS from every root, which is O(roots x graph) and hit memgraph's 600s
# query timeout on big projects (django: 31k roots, 101k CALLS edges). These
# two linear scans fetch the project's nodes and edges instead; the target
# of a relationship is deliberately unfiltered so INHERITS to an external
# base (typing.Protocol), OVERRIDES of external methods, and IMPLEMENTS of an
# external interface (NestJS `...OptionsFactory`) stay visible.
_DEAD_CODE_NODE_LABELS = "|".join(
    (
        NodeLabel.FUNCTION.value,
        NodeLabel.METHOD.value,
        NodeLabel.CLASS.value,
        NodeLabel.MODULE.value,
    )
)
_DEAD_CODE_REL_TYPES = "|".join(
    (
        RelationshipType.CALLS.value,
        RelationshipType.REFERENCES.value,
        RelationshipType.INSTANTIATES.value,
        RelationshipType.INHERITS.value,
        RelationshipType.DEFINES.value,
        RelationshipType.DEFINES_METHOD.value,
        RelationshipType.OVERRIDES.value,
        RelationshipType.IMPLEMENTS.value,
    )
)

CYPHER_DEAD_CODE_NODES = f"""MATCH (n)
WHERE {_any_label("n", _DEAD_CODE_NODE_LABELS)} AND n.qualified_name STARTS WITH $project_prefix
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.name AS name, n.path AS path, n.namespace AS namespace,
       n.start_line AS start_line, n.end_line AS end_line,
       n.decorated_start_line AS decorated_start_line,
       n.decorators AS decorators, n.is_exported AS is_exported,
       n.overrides_external AS overrides_external,
       n.is_program_main AS is_program_main,
       n.rust_cfg_test_mods AS rust_cfg_test_mods,
       n.rust_ungated_mods AS rust_ungated_mods,
       n.modifiers AS modifiers, n.return_type AS return_type,
       n.param_types AS param_types,
       n.type_checking_only AS type_checking_only"""

CYPHER_DEAD_CODE_RELS = f"""MATCH (a)-[r:{_DEAD_CODE_REL_TYPES}]->(b)
WHERE {_any_label("a", _DEAD_CODE_NODE_LABELS)}
  AND a.qualified_name STARTS WITH $project_prefix
RETURN labels(a)[0] AS from_label, a.qualified_name AS from_qn,
       type(r) AS rel_type, labels(b)[0] AS to_label,
       b.qualified_name AS to_qn, r.resolution AS resolution"""


# Duplicate-detection fetch. Grouping and overlap scoring run client-side in
# codebase_rag/duplicates.py (same reasoning as dead code: keep memgraph
# queries linear). ast-grep-tier symbols carry no fingerprint and are
# excluded here; their count is reported separately as "not analyzed".
_DUPLICATES_NODE_LABELS = "|".join((NodeLabel.FUNCTION.value, NodeLabel.METHOD.value))

CYPHER_DUPLICATE_FINGERPRINTS = f"""MATCH (n)
WHERE {_any_label("n", _DUPLICATES_NODE_LABELS)} AND n.qualified_name STARTS WITH $project_prefix
  AND n.ast_fingerprint IS NOT NULL
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.name AS name, n.path AS path,
       n.start_line AS start_line, n.start_col AS start_col,
       n.end_line AS end_line,
       n.ast_fingerprint AS ast_fingerprint,
       n.ast_fingerprint_nodes AS ast_fingerprint_nodes,
       n.ast_branch_fingerprints AS ast_branch_fingerprints"""

CYPHER_DUPLICATE_SKIPPED_COUNT = f"""MATCH (n)
WHERE {_any_label("n", _DUPLICATES_NODE_LABELS)} AND n.qualified_name STARTS WITH $project_prefix
  AND n.ast_fingerprint IS NULL
RETURN count(n) AS skipped"""


def wrap_with_unwind(query: str) -> str:
    return f"UNWIND $batch AS row\n{query}"


def build_nodes_by_ids_query(node_ids: list[int]) -> str:
    placeholders = ", ".join(f"${i}" for i in range(len(node_ids)))
    return f"""
MATCH (n)
WHERE id(n) IN [{placeholders}]
RETURN id(n) AS node_id, n.qualified_name AS qualified_name,
       labels(n) AS type, n.name AS name
ORDER BY n.qualified_name
"""


def build_constraint_query(label: str, prop: str) -> str:
    return f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"


def build_merge_node_query(label: str, id_key: str) -> str:
    return f"MERGE (n:{label} {{{id_key}: row.id}})\nSET n += row.props"


def build_node_props_query(label: str, id_key: str) -> str:
    """One node's full property map, addressed by its unique key."""
    return f"MATCH (n:{label} {{{id_key}: $id}}) RETURN properties(n) AS props"


# ASCII-only: `\w` alone would also accept non-ASCII letters and digits.
_PROPERTY_KEY = re.compile(r"[A-Za-z_]\w*", re.ASCII)


def build_remove_node_keys_query(label: str, id_key: str, keys: Iterable[str]) -> str:
    """Remove the named properties from one node.

    The batch merge adds and overwrites keys but never removes one, so it
    cannot undo a property a later write added; the isolated check's restore
    removes those by name (#1718). Cypher takes no parameter for a property
    NAME, so each one is checked to be a plain identifier and backquoted.
    """
    names = sorted(keys)
    for name in names:
        if not _PROPERTY_KEY.fullmatch(name):
            raise ValueError(f"not a property name: {name!r}")
    removals = ", ".join(f"n.`{name}`" for name in names)
    return f"MATCH (n:{label} {{{id_key}: $id}}) REMOVE {removals}"


def build_merge_relationship_query(
    from_label: str,
    from_key: str,
    rel_type: str,
    to_label: str,
    to_key: str,
    has_props: bool = False,
    merge_key_props: tuple[str, ...] = (),
) -> str:
    # merge_key_props: properties that distinguish parallel edges between the
    # same node pair (e.g. FLOWS_TO's `via`). Including them in the MERGE
    # pattern keeps each variant as its own edge instead of collapsing them
    # into one (issue #722). Every row in the batch must carry these keys.
    key_map = ""
    if merge_key_props:
        key_map = " {" + ", ".join(f"{p}: row.props.{p}" for p in merge_key_props) + "}"
    query = (
        f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
        f"(b:{to_label} {{{to_key}: row.to_val}})\n"
        f"MERGE (a)-[r:{rel_type}{key_map}]->(b)\n"
    )
    query += CYPHER_SET_PROPS_RETURN_COUNT if has_props else CYPHER_RETURN_COUNT
    return query


def build_create_node_query(label: str, id_key: str) -> str:
    return f"CREATE (n:{label} {{{id_key}: row.id}})\nSET n += row.props"


def build_create_relationship_query(
    from_label: str,
    from_key: str,
    rel_type: str,
    to_label: str,
    to_key: str,
    has_props: bool = False,
) -> str:
    query = (
        f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
        f"(b:{to_label} {{{to_key}: row.to_val}})\n"
        f"CREATE (a)-[r:{rel_type}]->(b)\n"
    )
    query += CYPHER_SET_PROPS_RETURN_COUNT if has_props else CYPHER_RETURN_COUNT
    return query


def build_missing_rel_endpoints_query(
    from_label: str, from_key: str, to_label: str, to_key: str, limit: int
) -> str:
    """The rows of a relationship batch that have an endpoint node missing.

    The write MATCHes both endpoints and returns only how many rows it wrote,
    so this read over the same batch is what names the rows it lost and which
    end of each was absent (issue #2438).
    """
    return (
        f"OPTIONAL MATCH (a:{from_label} {{{from_key}: row.from_val}})\n"
        f"OPTIONAL MATCH (b:{to_label} {{{to_key}: row.to_val}})\n"
        "WITH row, a, b WHERE a IS NULL OR b IS NULL\n"
        f"RETURN row.from_val AS {KEY_FROM_VAL}, row.to_val AS {KEY_TO_VAL}, "
        f"a IS NULL AS {KEY_FROM_MISSING}, b IS NULL AS {KEY_TO_MISSING}\n"
        f"LIMIT {limit}"
    )


# Deterministic graph queries for agents (issue #1523). All project-scoped
# through $project_prefix; walks of depth > 1 run client-side in
# codebase_rag/graph_query.py so each query stays linear.
# A C# type's natural name is `<namespace>.<name>`, which its qualified name
# no longer spells when the namespace mirrors the directory (issue #1629), so
# the namespace property answers that lookup.
CYPHER_GRAPH_RESOLVE_NAME = f"""MATCH (n)
WHERE {_any_label("n", _GRAPH_DEFINITION_LABELS)} AND n.qualified_name STARTS WITH $project_prefix
  AND (n.qualified_name = $qn OR n.qualified_name ENDS WITH $suffix OR n.name = $name
       OR (n.namespace IS NOT NULL AND n.namespace + '.' + n.name = $qn))
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line,
       n.name AS name, n.namespace AS namespace"""
# Which of $paths the project holds (issue #2611), asked only once a
# location matched no definition, or when `x:a:b` could name two files: `[]`
# for a file the graph never saw read as "no definition spans that line".
# Held means a Module of this project at the path (a source file or a
# document). File and Folder nodes are keyed by absolute path, so projects
# indexed from one root share them, and a containment walk from one Project
# reaches files only another project indexed (bot review).
CYPHER_GRAPH_LOCATION_FILES = f"""MATCH (m:{NodeLabel.MODULE.value})
WHERE m.qualified_name STARTS WITH $project_prefix AND m.path IN $paths
RETURN m.path AS path, m.qualified_name AS qualified_name"""
# Every definition registered under one natural name: the name itself and
# its `name@<line>` (and `@<line>_<col>`) variants, with the decorators that
# tell a property's getter, setter and deleter apart (issue #1808).
CYPHER_GLOSS_VARIANTS = f"""MATCH (n)
WHERE {_any_label("n", _GRAPH_DEFINITION_LABELS)} AND n.qualified_name STARTS WITH $project_prefix
  AND (n.qualified_name = $qn OR n.qualified_name STARTS WITH $variant_prefix)
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line,
       n.decorators AS decorators"""
CYPHER_GRAPH_RESOLVE_LOCATION = f"""MATCH (n)
WHERE {_any_label("n", _GRAPH_DEFINITION_LABELS)} AND n.qualified_name STARTS WITH $project_prefix AND n.path = $path
  AND n.start_line <= $line AND $line <= n.end_line
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line"""
# `decorated_start_line` is absent on a graph indexed before issue #2428 and
# on undecorated definitions; the reader falls back to `start_line`.
CYPHER_GRAPH_DEFINITION = f"""MATCH (n)
WHERE {_any_label("n", _GRAPH_DEFINITION_LABELS)} AND n.qualified_name = $qn AND n.qualified_name STARTS WITH $project_prefix
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.name AS name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line,
       n.decorated_start_line AS decorated_start_line, n.decorators AS decorators,
       n.name_start_line AS name_start_line, n.docstring AS docstring
{_DEFINITION_TIEBREAK}
LIMIT 1"""
# Gloss nodes (issue #1808). The node, its ANNOTATES edge and every MENTIONS
# edge are ONE statement, so a write is all or nothing: the subject and every
# mentioned definition are matched first, the `WHERE size(...)` gate drops the
# row when any of them is missing, and nothing after it runs. A gloss can thus
# never exist unattached or with a partial set of mentions, and a failed
# request leaves the graph exactly as it was. The caller reads the node back to
# learn whether the write landed rather than trusting a silent statement.
# `ON CREATE SET` keeps the original author and creation time on a repeat write
# of the same deterministic key; the other properties are SET by name (a null
# unsets one): the ingestor's parameter type carries scalars and string lists,
# not a nested map, so `SET g += $props` is not expressible on the wire.
# `write_id` is a per-call nonce: the statement returns nothing through the
# write API, and on a repeat write the node's mere presence cannot tell a
# gated-out request from the earlier note, so the caller reads it back and
# checks the nonce. A repeat write REPLACES the note's MENTIONS edges (collect
# then FOREACH DELETE, so an empty set is a no-op), because the edges belong to
# the note's current text and a stale one would say something the note no
# longer does. `UNWIND` of an empty list yields no rows; the MERGEs above it
# have already run.
_GLOSS = NodeLabel.GLOSS.value
_ANNOTATES = RelationshipType.ANNOTATES.value
_MENTIONS = RelationshipType.MENTIONS.value
# The hash the note records, plus what the text-quote anchor needs: the
# subject's simple name (masked out of the quote) and its span in its file.
CYPHER_GLOSS_TARGET = f"""MATCH (n)
WHERE {_any_label("n", _GRAPH_DEFINITION_LABELS)} AND n.qualified_name = $qn AND n.qualified_name STARTS WITH $project_prefix
RETURN n.anchor_hash AS target_hash, n.name AS name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line
{_DEFINITION_TIEBREAK}
LIMIT 1"""
# The key is deterministic in (subject, kind, body), so a repeat write of a
# note that has since MOVED finds the same node still attached to the
# definition it followed. A fresh write against a name is EXACT at that
# name: any other subject edge is dropped before the new one is merged, and
# the repair state (`moved_from`, `candidate_qns`) is cleared (bot review).
CYPHER_GLOSS_WRITE = f"""MATCH (t)
WHERE {_any_label("t", _GRAPH_DEFINITION_LABELS)} AND t.qualified_name = $target_qn AND t.qualified_name STARTS WITH $project_prefix
OPTIONAL MATCH (m)
WHERE {_any_label("m", _GRAPH_DEFINITION_LABELS)} AND m.qualified_name IN $mention_qns AND m.qualified_name STARTS WITH $project_prefix
WITH t, collect(DISTINCT m) AS mentioned
WHERE size(mentioned) = size($mention_qns)
MERGE (g:{_GLOSS} {{qualified_name: $qn}})
ON CREATE SET g.created_by = $created_by, g.created_at = $created_at
SET g.kind = $kind, g.status = $status, g.body = $body,
    g.commit_sha = $commit_sha, g.target_qn = $target_qn,
    g.target_hash = $target_hash, g.anchor_state = $anchor_state,
    g.anchor_quote = $anchor_quote, g.anchor_prefix = $anchor_prefix,
    g.anchor_suffix = $anchor_suffix,
    g.write_id = $write_id, g.mention_qns = $mention_qns,
    g.mention_hashes = $mention_hashes, g.mention_quotes = $mention_quotes,
    g.mentions_lost = null,
    g.project = $project_name, g.moved_from = null, g.candidate_qns = null
WITH g, t, mentioned
OPTIONAL MATCH (g)-[old:{_ANNOTATES}]->(prev)
WHERE prev <> t
WITH g, t, mentioned, collect(old) AS stale_subjects
FOREACH (edge IN stale_subjects | DELETE edge)
MERGE (g)-[:{_ANNOTATES}]->(t)
WITH g, mentioned
OPTIONAL MATCH (g)-[stale:{_MENTIONS}]->()
WITH g, mentioned, collect(stale) AS stale_edges
FOREACH (edge IN stale_edges | DELETE edge)
WITH g, mentioned
UNWIND mentioned AS m
MERGE (g)-[:{_MENTIONS}]->(m)"""
_STATE_EXACT = GlossAnchorState.EXACT.value
_STATE_MOVED = GlossAnchorState.MOVED.value
_STATE_STALE = GlossAnchorState.STALE.value
# Re-anchoring by qualified name, the EXACT tier of the repair chain the issue
# describes. A gloss records its subject (`target_qn`) and mentions
# (`mention_qns`) as properties, so when a rebuild has deleted and recreated
# the definitions, or an inbound-edge capture could not be read, the edges are
# rebuilt from the note's own record rather than lost. A subject whose name is
# gone stays unattached here; `gloss_repair` then grades it MOVED, AMBIGUOUS
# or LOST by content hash. Nothing here re-binds a note to a different name.
# Re-attaching by name resets the state: a note the previous run graded LOST
# or AMBIGUOUS whose name has come back is EXACT again (grading may demote it
# to STALE right after), and a MOVED note stays MOVED, because `target_qn` is
# by then the name it moved to.
# "Unattached" is asked with OPTIONAL MATCH plus a count, not a pattern
# predicate in WHERE, which Memgraph 3 rejects (test_memgraph3_cypher_compat).
# Every post-sync pass reads the syncing project's notes alone (issue #3236):
# by the recorded `project`, or for a note written before that property
# existed by the longest registered project prefixing its target, as
# `gloss_repair` assigns it. Another project's note changes state only when
# its own definitions do, on that project's sync.
_GLOSS_OF_SYNCING_PROJECT = """(g.project = $project_name
       OR (g.project IS NULL AND g.target_qn STARTS WITH $project_prefix
           AND ALL(longer_project IN $longer_project_prefixes
                   WHERE NOT g.target_qn STARTS WITH (longer_project + '.'))))"""
CYPHER_REANCHOR_GLOSSES = f"""MATCH (g:{_GLOSS})
WHERE {_GLOSS_OF_SYNCING_PROJECT}
OPTIONAL MATCH (g)-[:{_ANNOTATES}]->(subject)
WITH g, count(subject) AS subjects
WHERE subjects = 0
MATCH (t {{qualified_name: g.target_qn}})
WHERE {_any_label("t", _GRAPH_DEFINITION_LABELS)}
MERGE (g)-[:{_ANNOTATES}]->(t)
SET g.anchor_state = CASE WHEN g.moved_from IS NULL
    THEN '{_STATE_EXACT}' ELSE '{_STATE_MOVED}' END,
    g.candidate_qns = null"""
# The repair tiers below the name match (issue #1808, stage four), driven by
# `gloss_repair.repair_unanchored`: the notes still unattached after the
# query above, the definitions in a project carrying a set of content hashes,
# and the two outcomes -- follow the hash to its one new home (MOVED) or
# record why the note could not be placed (AMBIGUOUS with its candidates,
# LOST with none). Neither writes an edge to a guessed subject, and nothing
# here deletes.
CYPHER_UNANCHORED_GLOSSES = f"""MATCH (g:{_GLOSS})
OPTIONAL MATCH (g)-[:{_ANNOTATES}]->(subject)
WITH g, count(subject) AS subjects
WHERE subjects = 0
RETURN g.qualified_name AS qualified_name, g.target_qn AS target_qn,
       g.target_hash AS target_hash, g.anchor_state AS anchor_state,
       g.moved_from AS moved_from, g.candidate_qns AS candidate_qns,
       g.project AS project, g.anchor_quote AS anchor_quote,
       g.anchor_prefix AS anchor_prefix, g.anchor_suffix AS anchor_suffix"""
CYPHER_DEFINITIONS_BY_ANCHOR_HASH = f"""MATCH (t)
WHERE {_any_label("t", _GRAPH_DEFINITION_LABELS)} AND t.anchor_hash IN $hashes AND t.qualified_name STARTS WITH $project_prefix
RETURN t.qualified_name AS qualified_name, t.anchor_hash AS anchor_hash"""
# The move re-validates its own precondition and binds the node it found:
# the definitions carrying the note's hash in its project are collected
# INSIDE the statement, and the note is bound only if there is exactly one
# PHYSICAL node -- so a same-name pair across two labels (two nodes, one
# qualified name) is refused rather than given two edges, and a match that
# appeared or a hash that changed between the repair pass's read and this
# write makes the statement a no-op instead of a wrong binding (bot review).
# The note's own precondition is re-checked here too: the pass selected it
# while it had no subject, but a writer may have attached it since, and a
# MERGE would then give it a second subject. A note that already has one is
# left exactly as the other writer left it (bot review, second round).
# `origin` is the name the note was first written against (its first move
# recorded it in `moved_from`). Following the hash back to that name is a
# return home, not another move: the note is EXACT again with no `moved_from`.
# Computed in a WITH so both SETs read the pre-update values.
CYPHER_GLOSS_MOVE = f"""MATCH (g:{_GLOSS} {{qualified_name: $qn}})
MATCH (t)
WHERE {_any_label("t", _GRAPH_DEFINITION_LABELS)} AND t.anchor_hash = $target_hash AND t.qualified_name STARTS WITH $project_prefix
WITH g, collect(t) AS targets
WHERE size(targets) = 1
WITH g, targets[0] AS t, coalesce(g.moved_from, g.target_qn) AS origin
OPTIONAL MATCH (g)-[held:{_ANNOTATES}]->()
WITH g, t, origin, count(held) AS subjects
WHERE subjects = 0
SET g.anchor_state = CASE WHEN origin = t.qualified_name
    THEN '{_STATE_EXACT}' ELSE '{_STATE_MOVED}' END,
    g.moved_from = CASE WHEN origin = t.qualified_name THEN null ELSE origin END,
    g.target_qn = t.qualified_name, g.candidate_qns = null
MERGE (g)-[:{_ANNOTATES}]->(t)"""
# The text-quote tier (stage five) needs every definition's name and span
# in a project, to digest each one's current text the way the note's quote
# was digested when it was written. Read once per project per pass, and
# only when a note has reached this tier.
CYPHER_DEFINITION_SPANS = f"""MATCH (t)
WHERE {_any_label("t", _QUOTE_SPAN_LABELS)} AND t.qualified_name STARTS WITH $project_prefix
  AND t.path IS NOT NULL AND t.start_line IS NOT NULL AND t.end_line IS NOT NULL
RETURN t.qualified_name AS qualified_name, t.name AS name, t.path AS path,
       t.start_line AS start_line, t.end_line AS end_line,
       t.anchor_hash AS anchor_hash"""
# The quote tier's move: the same shape as `CYPHER_GLOSS_MOVE`, bound by the
# candidate's qualified name rather than its hash, since the hash is exactly
# what a rename changed. It re-validates exactly one PHYSICAL node under
# that name and that the note is still unattached, so a same-name pair is
# refused and a concurrent attach is left alone. The recorded `target_hash`
# is NOT replaced: the name is part of the hash, so a renamed definition
# grades STALE right after, which is the truthful reading of a signature
# change; `moved_from` keeps the move visible beside it. The prefix and
# suffix are re-recorded for the new location so the next tie-break reads
# the note's current neighbours; the quote is unchanged by construction.
# The candidate is re-validated as the index saw it -- same file, same
# span, same NON-NULL hash -- so a definition another updater replaced
# between the span read and this write is not bound on the strength of its
# name alone. A label without a hash (a class, until containers are hashed)
# cannot be re-validated and is never bound here: null on both sides read as
# equal would have bound a rewritten body (bot review, PR #1966, twice).
CYPHER_GLOSS_MOVE_TO_QN = f"""MATCH (g:{_GLOSS} {{qualified_name: $qn}})
MATCH (t)
WHERE {_any_label("t", _GRAPH_DEFINITION_LABELS)} AND t.qualified_name = $target_qn AND t.qualified_name STARTS WITH $project_prefix
  AND t.path = $path AND t.start_line = $start_line AND t.end_line = $end_line
  AND t.anchor_hash IS NOT NULL AND t.anchor_hash = $anchor_hash
WITH g, collect(t) AS targets
WHERE size(targets) = 1
WITH g, targets[0] AS t, coalesce(g.moved_from, g.target_qn) AS origin
OPTIONAL MATCH (g)-[held:{_ANNOTATES}]->()
WITH g, t, origin, count(held) AS subjects
WHERE subjects = 0
SET g.anchor_state = CASE WHEN origin = t.qualified_name
    THEN '{_STATE_EXACT}' ELSE '{_STATE_MOVED}' END,
    g.moved_from = CASE WHEN origin = t.qualified_name THEN null ELSE origin END,
    g.target_qn = t.qualified_name, g.candidate_qns = null,
    g.anchor_prefix = $anchor_prefix, g.anchor_suffix = $anchor_suffix
MERGE (g)-[:{_ANNOTATES}]->(t)"""
CYPHER_GLOSS_MARK = f"""MATCH (g:{_GLOSS} {{qualified_name: $qn}})
SET g.anchor_state = $anchor_state, g.candidate_qns = $candidate_qns"""
# A note's mentions are placed after its subject (issue #3230), by
# `gloss_repair.repair_mentions`: the attached notes that mention anything,
# with each mention's recorded anchors and the edges it has now; the
# definitions currently under the mentioned names; and one statement per note
# whose mentions changed. A mention is never bound by its name alone once
# that name holds other code and the recorded code is found elsewhere, and
# one that cannot be placed is listed in `mentions_lost` rather than dropped.
CYPHER_GLOSS_MENTION_ANCHORS = f"""MATCH (g:{_GLOSS})-[:{_ANNOTATES}]->()
WITH DISTINCT g
WHERE g.mention_qns IS NOT NULL AND size(g.mention_qns) > 0
OPTIONAL MATCH (g)-[:{_MENTIONS}]->(m)
RETURN g.qualified_name AS qualified_name, g.project AS project,
       g.target_qn AS target_qn, g.mention_qns AS mention_qns,
       g.mention_hashes AS mention_hashes, g.mention_quotes AS mention_quotes,
       g.mentions_lost AS mentions_lost, g.write_id AS write_id,
       collect(m.qualified_name) AS attached"""
CYPHER_DEFINITIONS_BY_QNS = f"""UNWIND $qns AS qn
MATCH (t {{qualified_name: qn}})
WHERE {_any_label("t", _GRAPH_DEFINITION_LABELS)}
RETURN t.qualified_name AS qualified_name, t.anchor_hash AS anchor_hash"""
# The note's mentions as the repair placed them, and its MENTIONS edges
# rebuilt to match. A mention kept on its name is bound by `attach_qns`; one
# followed to a new name by `attach_keys`, its name and the hash the pass
# read joined by `key_separator`, so a definition replaced between the read
# and this write is not bound on the strength of its name. The targets are
# matched BEFORE any edge is deleted, and only an edge to a node that is no
# longer a target is deleted: with the definition indexes in place, Memgraph
# 3 lost the edge when one statement deleted it and then merged it again.
# The note must still carry the `write_id` the pass read: an `annotate` that
# landed in between wrote newer mentions, which this result would overwrite.
# And every followed key must still match a target: a definition replaced
# under its new name would otherwise have that name saved without an edge,
# and the next pass would read the newcomer as an in-place edit and bind it.
# Either way nothing is written, and the next sync places the note afresh.
CYPHER_GLOSS_SET_MENTIONS = f"""MATCH (g:{_GLOSS} {{qualified_name: $qn}})
WHERE coalesce(g.write_id, '') = $write_id
OPTIONAL MATCH (m)
WHERE {_any_label("m", _GRAPH_DEFINITION_LABELS)} AND m.qualified_name STARTS WITH $project_prefix
  AND (m.qualified_name IN $attach_qns
       OR (m.qualified_name + $key_separator + m.anchor_hash) IN $attach_keys)
WITH g, collect(DISTINCT m) AS targets
WHERE all(key IN $attach_keys WHERE any(target IN targets
          WHERE target.qualified_name + $key_separator + target.anchor_hash = key))
SET g.mention_qns = $mention_qns, g.mention_hashes = $mention_hashes,
    g.mention_quotes = $mention_quotes, g.mentions_lost = $mentions_lost
WITH g, targets
OPTIONAL MATCH (g)-[old:{_MENTIONS}]->(t)
WHERE NOT t IN targets
WITH g, targets, collect(old) AS stale_edges
FOREACH (edge IN stale_edges | DELETE edge)
WITH g, targets
UNWIND targets AS m
MERGE (g)-[:{_MENTIONS}]->(m)"""
# Staleness (issue #1808): a gloss recorded its subject's `anchor_hash` as
# `target_hash` when it was written; after a sync the two are compared and the
# note graded EXACT or STALE. A subject with no hash (a class, a module) or a
# note written before hashes existed is left as it is rather than guessed at.
# So is a note whose recorded hash is not in the current format (the prefix
# gate): stage two recorded the clone skeleton, which is not comparable, and
# reading it as STALE would be wrong for an unchanged definition. A STALE
# note goes back to EXACT if the code is reverted -- or back to MOVED, if it
# had followed its definition to a new name: `moved_from` keeps the move
# visible for as long as the hashes agree. Nothing here moves a note or
# deletes one.
CYPHER_GRADE_GLOSS_ANCHORS = f"""MATCH (g:{_GLOSS})-[:{_ANNOTATES}]->(t)
WHERE {_GLOSS_OF_SYNCING_PROJECT}
  AND g.target_hash IS NOT NULL AND t.anchor_hash IS NOT NULL
  AND g.target_hash STARTS WITH '{ANCHOR_HASH_VERSION}'
SET g.anchor_state = CASE WHEN g.target_hash = t.anchor_hash
    THEN (CASE WHEN g.moved_from IS NULL
          THEN '{_STATE_EXACT}' ELSE '{_STATE_MOVED}' END)
    ELSE '{_STATE_STALE}' END"""
_GLOSS_ROW = (
    "g.qualified_name AS qualified_name, g.kind AS kind, g.status AS status, "
    "g.body AS body, g.created_by AS created_by, g.created_at AS created_at, "
    "g.commit_sha AS commit_sha, g.target_qn AS target_qn, "
    "g.target_hash AS target_hash, g.anchor_state AS anchor_state, "
    "g.moved_from AS moved_from, g.candidate_qns AS candidate_qns, "
    "g.write_id AS write_id, g.mentions_lost AS mentions_lost, "
    "collect(m.qualified_name) AS mentions"
)
CYPHER_GLOSS_READ = f"""MATCH (g:{_GLOSS} {{qualified_name: $qn}})
OPTIONAL MATCH (g)-[:{_MENTIONS}]->(m)
RETURN {_GLOSS_ROW}"""
CYPHER_GLOSSES_ANNOTATING = f"""MATCH (g:{_GLOSS})-[:{_ANNOTATES}]->(t)
WHERE t.qualified_name = $qn
OPTIONAL MATCH (g)-[:{_MENTIONS}]->(m)
RETURN {_GLOSS_ROW}"""
CYPHER_GLOSSES_MENTIONING = f"""MATCH (g:{_GLOSS})-[:{_MENTIONS}]->(t)
WHERE t.qualified_name = $qn
OPTIONAL MATCH (g)-[:{_MENTIONS}]->(m)
RETURN {_GLOSS_ROW}"""
# The notes written against a name that no longer resolves: unattached, with
# `target_qn` still naming the definition they were about. This is how a
# LOST or AMBIGUOUS note stays visible through the same read that would have
# found it attached (issue #1808).
# Scoped to the project the way every gloss read is, by the recorded
# `project` (falling back to the qn prefix for a note written before that
# property existed), and matching the target the way `resolve` names one: the
# full qualified name or a dotted suffix of it (`Store.get`).
CYPHER_GLOSSES_ORPHANED_ON = f"""MATCH (g:{_GLOSS})
WHERE (g.project = $project_name
       OR (g.project IS NULL AND g.target_qn STARTS WITH $project_prefix))
  AND (g.target_qn = $qn OR g.target_qn ENDS WITH $suffix)
OPTIONAL MATCH (g)-[:{_ANNOTATES}]->(subject)
WITH g, count(subject) AS subjects
WHERE subjects = 0
OPTIONAL MATCH (g)-[:{_MENTIONS}]->(m)
RETURN {_GLOSS_ROW}"""
# The same, for a definition and its `name@<line>` variants: a note filed
# through a descriptor (`x#setter`) sits on a variant, so the literal target
# names nothing once the member is gone (issue #1808).
CYPHER_GLOSSES_ORPHANED_UNDER = f"""MATCH (g:{_GLOSS})
WHERE (g.project = $project_name
       OR (g.project IS NULL AND g.target_qn STARTS WITH $project_prefix))
  AND (g.target_qn = $qn OR g.target_qn STARTS WITH $variant_prefix)
OPTIONAL MATCH (g)-[:{_ANNOTATES}]->(subject)
WITH g, count(subject) AS subjects
WHERE subjects = 0
OPTIONAL MATCH (g)-[:{_MENTIONS}]->(m)
RETURN {_GLOSS_ROW}"""
# One row per call SITE (edges carry the site from issue #1522).
# A call site sits in its caller's body, so both reads take `path` from the
# caller: that is the file `line`/`col` index into, whichever endpoint the
# row names. The callee's own file is `callee_path` (issue #2460).
# The named node is found in a MATCH of its own, under every definition label,
# so Memgraph starts from the label + qualified_name indexes. Looked up without
# a label it was a scan of the whole shared graph, once per node a `--depth`
# walk reached: 154 s for one depth-2 walk on django (issue #2597).
_CALL_SITE_FIELDS = """caller.path AS path, callee.path AS callee_path,
       r.line AS line, r.col AS col, r.end_line AS end_line, r.end_col AS end_col,
       r.arg_count AS arg_count, r.kwarg_names AS kwarg_names,
       r.resolution AS resolution"""
_CALLER_ROW = (
    "labels(caller)[0] AS label, caller.qualified_name AS qualified_name,\n       "
    + _CALL_SITE_FIELDS
)
_CALLEE_ROW = (
    "labels(callee)[0] AS label, callee.qualified_name AS qualified_name,\n       "
    + _CALL_SITE_FIELDS
)
CYPHER_GRAPH_CALLERS = f"""MATCH (callee {{qualified_name: $qn}})
WHERE {_any_label("callee", _GRAPH_DEFINITION_LABELS)}
MATCH (caller)-[r:CALLS]->(callee)
WHERE caller.qualified_name STARTS WITH $project_prefix
RETURN {_CALLER_ROW}"""
CYPHER_GRAPH_CALLEES = f"""MATCH (caller {{qualified_name: $qn}})
WHERE {_any_label("caller", _GRAPH_DEFINITION_LABELS)}
MATCH (caller)-[r:CALLS]->(callee)
WHERE callee.qualified_name STARTS WITH $project_prefix
RETURN {_CALLEE_ROW}"""
# The same reads for a whole level of the walk past its start: `$qns` is the
# frontier, and `to_qn` / `from_qn` names the frontier node a row hangs off,
# so a level is one round trip instead of one per node.
CYPHER_GRAPH_CALLERS_OF = f"""UNWIND $qns AS qn
MATCH (callee {{qualified_name: qn}})
WHERE {_any_label("callee", _GRAPH_DEFINITION_LABELS)}
MATCH (caller)-[r:CALLS]->(callee)
WHERE caller.qualified_name STARTS WITH $project_prefix
RETURN callee.qualified_name AS to_qn, {_CALLER_ROW}"""
CYPHER_GRAPH_CALLEES_OF = f"""UNWIND $qns AS qn
MATCH (caller {{qualified_name: qn}})
WHERE {_any_label("caller", _GRAPH_DEFINITION_LABELS)}
MATCH (caller)-[r:CALLS]->(callee)
WHERE callee.qualified_name STARTS WITH $project_prefix
RETURN caller.qualified_name AS from_qn, {_CALLEE_ROW}"""
# Cross-service edges as reads (issue #1603). The writers (`EXPOSES` from a
# handler to its ENDPOINT/RPC/DISPATCH resource, `RESOLVES_TO` from a client
# NETWORK resource to the endpoint, `READS_FROM`/`WRITES_TO` from a call site
# to a resource) existed without a consumer. A caller reaches an endpoint
# either through a NETWORK resource that RESOLVES_TO it, or directly for the
# RPC and dispatch kinds, which join without RESOLVES_TO. Callers are counted
# ACROSS projects by design: the endpoint's own project is scoped by the
# handler, the callers may live anywhere in the shared graph.
# "Is called" is asked with OPTIONAL MATCH plus a count, not a pattern
# predicate in WHERE (Memgraph 3).
CYPHER_GRAPH_ENDPOINTS = """MATCH (h)-[:EXPOSES]->(e:Resource)
WHERE h.qualified_name STARTS WITH $project_prefix
OPTIONAL MATCH (c)-[:READS_FROM|WRITES_TO]->(n:Resource)-[:RESOLVES_TO]->(e)
WITH h, e, count(DISTINCT c) AS resolved
OPTIONAL MATCH (d)-[:READS_FROM|WRITES_TO]->(e)
WITH h, e, resolved, count(DISTINCT d) AS direct
RETURN e.name AS endpoint, e.kind AS kind, labels(h)[0] AS label,
       h.qualified_name AS handler, h.path AS path,
       resolved + direct AS callers
ORDER BY endpoint, handler"""
# The project's endpoint identities and their handlers: what an
# `endpoint_callers` target is resolved against before any caller is read.
CYPHER_GRAPH_EXPOSED_ENDPOINTS = """MATCH (h)-[:EXPOSES]->(e:Resource)
WHERE h.qualified_name STARTS WITH $project_prefix
RETURN DISTINCT e.name AS endpoint, h.qualified_name AS handler
ORDER BY endpoint, handler"""
# `$qn` names the handler or the endpoint identity as stored (`GET /users/:id`).
CYPHER_GRAPH_ENDPOINT_CALLERS = """MATCH (h)-[:EXPOSES]->(e:Resource)
WHERE h.qualified_name STARTS WITH $project_prefix
  AND (h.qualified_name = $qn OR e.name = $qn)
MATCH (c)-[r:READS_FROM|WRITES_TO]->(n:Resource)-[:RESOLVES_TO]->(e)
RETURN labels(c)[0] AS label, c.qualified_name AS qualified_name, c.path AS path,
       n.name AS url, type(r) AS direction, e.name AS endpoint,
       h.qualified_name AS handler"""
CYPHER_GRAPH_ENDPOINT_DIRECT_CALLERS = """MATCH (h)-[:EXPOSES]->(e:Resource)
WHERE h.qualified_name STARTS WITH $project_prefix
  AND (h.qualified_name = $qn OR e.name = $qn)
MATCH (c)-[r:READS_FROM|WRITES_TO]->(e)
RETURN labels(c)[0] AS label, c.qualified_name AS qualified_name, c.path AS path,
       e.name AS url, type(r) AS direction, e.name AS endpoint,
       h.qualified_name AS handler"""
# Every NETWORK access a project makes, with the handler it resolves to when
# one does; an unresolved row (null endpoint) is a dependency the graph
# cannot place, which is itself the answer to "what does this depend on".
CYPHER_GRAPH_REMOTE_DEPENDENCIES = """MATCH (c)-[r:READS_FROM|WRITES_TO]->(n:Resource {kind: 'NETWORK'})
WHERE c.qualified_name STARTS WITH $project_prefix
OPTIONAL MATCH (n)-[:RESOLVES_TO]->(e:Resource)<-[:EXPOSES]-(h)
RETURN labels(c)[0] AS label, c.qualified_name AS qualified_name, c.path AS path,
       n.name AS url, type(r) AS direction, e.name AS endpoint,
       h.qualified_name AS handler, e.project AS handler_project
ORDER BY qualified_name, url, handler"""
# The dead-code walk's view of the same joins: per exposing handler, how many
# call sites reach its resource (through RESOLVES_TO, or directly). Each
# count is finished in its own WITH: `resolved + count(DISTINCT d)` in one
# RETURN mixes a variable with an aggregate, which Neo4j 5 rejects as an
# ambiguous aggregation (Memgraph accepts it).
CYPHER_DEAD_CODE_ENDPOINT_LINKS = """MATCH (h)-[:EXPOSES]->(e:Resource)
WHERE h.qualified_name STARTS WITH $project_prefix
OPTIONAL MATCH (c)-[:READS_FROM|WRITES_TO]->(n:Resource)-[:RESOLVES_TO]->(e)
WITH h, e, count(DISTINCT c) AS resolved
OPTIONAL MATCH (d)-[:READS_FROM|WRITES_TO]->(e)
WITH h, e, resolved, count(DISTINCT d) AS direct
RETURN h.qualified_name AS handler, e.name AS endpoint,
       resolved + direct AS callers"""
CYPHER_GRAPH_IMPLEMENTORS = """MATCH (impl)-[r:INHERITS|IMPLEMENTS]->(base)
WHERE base.qualified_name = $qn AND impl.qualified_name STARTS WITH $project_prefix
RETURN labels(impl)[0] AS label, impl.qualified_name AS qualified_name,
       impl.path AS path, type(r) AS rel_type"""
CYPHER_GRAPH_OVERRIDES = """MATCH (a)-[r:OVERRIDES]-(b)
WHERE b.qualified_name = $qn AND a.qualified_name STARTS WITH $project_prefix
RETURN labels(a)[0] AS label, a.qualified_name AS qualified_name, a.path AS path,
       type(r) AS rel_type, r.resolution AS resolution"""
# The Go interfaces declaring a method `$name` whose implementers cgr cannot
# tell (#3360), and why: a rename of a method of that name could leave one
# of their implementers, or the interface, under the old name.
CYPHER_GRAPH_GO_IMPLEMENTERS_UNKNOWN = """MATCH (i:Interface)-[:DEFINES_METHOD]->(m:Method)
WHERE m.name = $name AND i.qualified_name STARTS WITH $project_prefix
  AND i.implementers_unknown IS NOT NULL AND i.implementers_unknown <> ''
RETURN DISTINCT i.qualified_name AS qualified_name, i.path AS path,
       i.implementers_unknown AS implementers_unknown"""
# One hop of the `depth` walks (issue #2624): the neighbours of a whole
# frontier at once, each row naming the frontier node it hangs off as
# `through`. OVERRIDES is read one direction per query so a walk that went
# up to the overridden method never turns back down into its siblings.
CYPHER_GRAPH_IMPLEMENTORS_OF = """MATCH (impl)-[r:INHERITS|IMPLEMENTS]->(base)
WHERE base.qualified_name IN $qns AND impl.qualified_name STARTS WITH $project_prefix
RETURN base.qualified_name AS through, labels(impl)[0] AS label,
       impl.qualified_name AS qualified_name, impl.path AS path, type(r) AS rel_type"""
CYPHER_GRAPH_OVERRIDERS_OF = """MATCH (a)-[r:OVERRIDES]->(b)
WHERE b.qualified_name IN $qns AND a.qualified_name STARTS WITH $project_prefix
RETURN b.qualified_name AS through, labels(a)[0] AS label,
       a.qualified_name AS qualified_name, a.path AS path, type(r) AS rel_type"""
CYPHER_GRAPH_OVERRIDDEN_BY = """MATCH (a)-[r:OVERRIDES]->(b)
WHERE a.qualified_name IN $qns AND b.qualified_name STARTS WITH $project_prefix
RETURN a.qualified_name AS through, labels(b)[0] AS label,
       b.qualified_name AS qualified_name, b.path AS path, type(r) AS rel_type"""
# Whether the graph holds a node by this name at all (issue #2461): the edge
# walks above answer [] both for a definition nothing reaches and for a name
# the graph never saw, and only the second is a mistake. Every label keyed by
# qualified_name, not just the definitions, because a target may be an
# ExternalModule (importers of `os`) or another project's node; the label
# disjunction plans as one index lookup per label, where a bare
# `(n {qualified_name: $qn})` scans every node.
_QUALIFIED_NAME_LABELS = "|".join(
    sorted(
        label
        for label, key in NODE_UNIQUE_CONSTRAINTS.items()
        if key == UniqueKeyType.QUALIFIED_NAME
    )
)
CYPHER_GRAPH_NODE_EXISTS = f"""MATCH (n)
WHERE {_any_label("n", _QUALIFIED_NAME_LABELS)} AND n.qualified_name = $qn
RETURN n.qualified_name AS qualified_name
LIMIT 1"""
# The definitions under one dotted parent, which a name mistyped in its last
# part is closest to.
CYPHER_GRAPH_DEFINITIONS_UNDER = f"""MATCH (n)
WHERE {_any_label("n", _GRAPH_DEFINITION_LABELS)} AND n.qualified_name STARTS WITH $project_prefix
RETURN n.qualified_name AS qualified_name"""
# Structural delta after a write (issue #1525): the touched files' definitions
# with the properties the delta compares, every call/reference site touching
# them, and the project's module import graph.
_DELTA_DEFINITION_LABELS = "|".join(
    (
        NodeLabel.FUNCTION.value,
        NodeLabel.METHOD.value,
        NodeLabel.CLASS.value,
        NodeLabel.INTERFACE.value,
        NodeLabel.ENUM.value,
        NodeLabel.TYPE.value,
        NodeLabel.UNION.value,
    )
)
_DELTA_DEFINITION_FIELDS = """RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.name AS name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line,
       n.positional_params AS positional_params,
       n.ast_fingerprint AS ast_fingerprint,
       n.ast_fingerprint_nodes AS ast_fingerprint_nodes,
       n.ast_branch_fingerprints AS ast_branch_fingerprints"""
CYPHER_DELTA_DEFINITIONS = f"""MATCH (n)
WHERE {_any_label("n", _DELTA_DEFINITION_LABELS)} AND n.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE n.qualified_name <> longer_project
            AND NOT n.qualified_name STARTS WITH (longer_project + '.'))
  AND n.path IN $paths
{_DELTA_DEFINITION_FIELDS}"""
# The callees of the touched files' sites that live elsewhere: their
# declared parameters decide the arity verdict of each site.
# `$project_prefix` as well as `$qns`: the callee side of CYPHER_DELTA_SITES
# is not prefix-scoped, so an unresolved or external target name can reach
# this lookup, and a shared graph holding several projects would otherwise
# let a same-named definition from another project decide the arity verdict.
CYPHER_DELTA_DEFINITIONS_BY_QN = f"""MATCH (n)
WHERE {_any_label("n", _DELTA_DEFINITION_LABELS)} AND n.qualified_name IN $qns
  AND n.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE n.qualified_name <> longer_project
            AND NOT n.qualified_name STARTS WITH (longer_project + '.'))
{_DELTA_DEFINITION_FIELDS}"""
CYPHER_DELTA_SITES = """MATCH (a)-[r:CALLS|REFERENCES|INSTANTIATES]->(b)
WHERE a.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE a.qualified_name <> longer_project
            AND NOT a.qualified_name STARTS WITH (longer_project + '.'))
  AND (a.path IN $paths OR b.path IN $paths)
RETURN a.qualified_name AS from_qn, a.path AS from_path, type(r) AS rel_type,
       r.resolution AS resolution, r.spread_args AS spread_args,
       r.call_qualifier AS call_qualifier,
       b.qualified_name AS to_qn, b.path AS to_path, r.line AS line, r.col AS col,
       r.arg_count AS arg_count, r.kwarg_names AS kwarg_names,
       r.star_args AS star_args, r.star_kwargs AS star_kwargs"""
# One hop of the backward test-reach walk: the callers of a frontier of
# qualified names, with the properties the test classifier reads.
# The blast radius of a signature change crosses services (issue #1603): a
# changed handler's endpoint is reached by call sites in OTHER projects,
# through a NETWORK resource that RESOLVES_TO it, or directly for the RPC and
# dispatch kinds. A caller inside the handler's own project is a local
# site, whatever route it takes, and is excluded (bot review on PR #1978).
CYPHER_DELTA_REMOTE_CALLERS_OF = """MATCH (h)-[:EXPOSES]->(e:Resource)
WHERE h.qualified_name IN $qns
MATCH (c)-[r:READS_FROM|WRITES_TO]->(n:Resource {kind: 'NETWORK'})-[:RESOLVES_TO]->(e)
WHERE NOT c.qualified_name STARTS WITH $project_prefix
RETURN DISTINCT h.qualified_name AS handler, e.name AS endpoint,
       labels(c)[0] AS label, c.qualified_name AS qualified_name, c.path AS path,
       n.name AS url"""
CYPHER_DELTA_REMOTE_DIRECT_CALLERS_OF = """MATCH (h)-[:EXPOSES]->(e:Resource)
WHERE h.qualified_name IN $qns AND e.kind IN ['RPC', 'DISPATCH']
MATCH (c)-[r:READS_FROM|WRITES_TO]->(e)
WHERE NOT c.qualified_name STARTS WITH $project_prefix
RETURN DISTINCT h.qualified_name AS handler, e.name AS endpoint,
       labels(c)[0] AS label, c.qualified_name AS qualified_name, c.path AS path,
       e.name AS url"""
CYPHER_DELTA_CALLERS_OF = """MATCH (a)-[:CALLS|REFERENCES|INSTANTIATES]->(b)
WHERE b.qualified_name IN $qns AND a.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE a.qualified_name <> longer_project
            AND NOT a.qualified_name STARTS WITH (longer_project + '.'))
RETURN DISTINCT labels(a)[0] AS label, a.qualified_name AS qualified_name,
       a.name AS name, a.path AS path, a.start_line AS start_line,
       a.end_line AS end_line, a.decorators AS decorators,
       a.is_exported AS is_exported, b.qualified_name AS to_qn"""
# Rust test classification inputs, fetched only when the walk reaches Rust.
CYPHER_DELTA_RUST_MODULES = """MATCH (m:Module)
WHERE m.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE m.qualified_name <> longer_project
            AND NOT m.qualified_name STARTS WITH (longer_project + '.'))
  AND m.path ENDS WITH '.rs'
RETURN labels(m)[0] AS label, m.qualified_name AS qualified_name, m.name AS name,
       m.path AS path, m.decorators AS decorators,
       m.rust_cfg_test_mods AS rust_cfg_test_mods,
       m.rust_ungated_mods AS rust_ungated_mods"""
CYPHER_DELTA_RUST_TEST_FNS = """MATCH (n)
WHERE (n:Function OR n:Method) AND n.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE n.qualified_name <> longer_project
            AND NOT n.qualified_name STARTS WITH (longer_project + '.'))
  AND n.path IN $paths
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.name AS name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line,
       n.decorators AS decorators"""
CYPHER_DELTA_MODULE_IMPORTS = """MATCH (m:Module)-[:IMPORTS]->(t:Module)
WHERE m.qualified_name STARTS WITH $project_prefix
  AND t.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE m.qualified_name <> longer_project
            AND NOT m.qualified_name STARTS WITH (longer_project + '.'))
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE t.qualified_name <> longer_project
            AND NOT t.qualified_name STARTS WITH (longer_project + '.'))
RETURN DISTINCT m.qualified_name AS from_qn, m.path AS from_path,
       t.qualified_name AS to_qn"""
# The import statements that bind a symbol BY NAME into or out of the
# touched files (issue #2516), one row per bound name: an importer of a
# touched module still naming a symbol the edit removed, and a touched
# module's own imports, which may re-export that name from its new home.
# `to_path` lets a re-export be followed into a module the edit left alone,
# which this same query then reads one file at a time.
CYPHER_DELTA_NAMED_IMPORTS = """MATCH (m:Module)-[r:IMPORTS]->(t)
WHERE m.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE m.qualified_name <> longer_project
            AND NOT m.qualified_name STARTS WITH (longer_project + '.'))
  AND r.imported_name IS NOT NULL
  AND (m.path IN $paths OR t.path IN $paths)
RETURN m.qualified_name AS from_qn, m.path AS from_path,
       t.qualified_name AS to_qn, t.path AS to_path,
       r.imported_name AS imported_name,
       r.alias AS alias, r.line AS line, r.col AS col"""
# The modules at the touched paths, and every import of one (issue #3266):
# a module the edit deleted or moved leaves each statement naming the module
# itself dangling -- `import pkg.signals`, `export * from "./util"` -- which
# the by-name query above, keyed on a symbol, cannot see.
CYPHER_DELTA_MODULES = """MATCH (n:Module)
WHERE n.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE n.qualified_name <> longer_project
            AND NOT n.qualified_name STARTS WITH (longer_project + '.'))
  AND n.path IN $paths
RETURN n.qualified_name AS qualified_name, n.path AS path"""
CYPHER_DELTA_MODULE_IMPORTERS = """MATCH (m:Module)-[r:IMPORTS]->(t:Module)
WHERE m.qualified_name STARTS WITH $project_prefix
  AND t.qualified_name STARTS WITH $project_prefix
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE m.qualified_name <> longer_project
            AND NOT m.qualified_name STARTS WITH (longer_project + '.'))
  AND ALL(longer_project IN $longer_project_prefixes
          WHERE t.qualified_name <> longer_project
            AND NOT t.qualified_name STARTS WITH (longer_project + '.'))
  AND t.path IN $paths
RETURN m.qualified_name AS from_qn, m.path AS from_path,
       t.qualified_name AS to_qn, t.path AS to_path,
       r.imported_name AS imported_name,
       r.alias AS alias, r.line AS line, r.col AS col"""
# Context slice reads (issue #1536): trace hotness of the callers of one
# symbol, the types it returns and accepts, and the sections of the
# documents whose links point at its file.
CYPHER_CONTEXT_HOTNESS = """MATCH (caller)-[r:CALLS]->(callee)
WHERE callee.qualified_name = $qn AND caller.qualified_name STARTS WITH $project_prefix
  AND r.dynamic_call_count IS NOT NULL
RETURN caller.qualified_name AS qualified_name, r.dynamic_call_count AS dynamic_call_count"""
CYPHER_CONTEXT_TYPES = """MATCH (n)-[r:RETURNS|ACCEPTS]->(t)
WHERE n.qualified_name = $qn AND t.qualified_name STARTS WITH $project_prefix
RETURN DISTINCT type(r) AS rel_type, t.qualified_name AS qualified_name, t.path AS path,
       t.start_line AS start_line, t.end_line AS end_line"""
# A link starts at the innermost Section holding it, or at the document's
# Module above its first heading (issue #2458), so the document is found
# through any depth of its sections; one row per document however many links.
CYPHER_CONTEXT_DOC_SECTIONS = """MATCH (f:File {absolute_path: $absolute_path})<-[:LINKS_TO]-(link_source)
MATCH (doc:Module)-[:CONTAINS_SECTION*0..]->(link_source)
WHERE doc.qualified_name STARTS WITH $project_prefix
WITH DISTINCT doc
MATCH (doc)-[:CONTAINS_SECTION]->(s:Section)
RETURN doc.qualified_name AS from_qn, s.qualified_name AS qualified_name, s.name AS name,
       s.path AS path, s.start_line AS start_line, s.end_line AS end_line"""
CYPHER_GRAPH_IMPORTERS = """MATCH (m:Module)-[r:IMPORTS]->(target)
WHERE target.qualified_name = $qn AND m.qualified_name STARTS WITH $project_prefix
RETURN m.qualified_name AS qualified_name, m.path AS path, r.line AS line,
       r.col AS col, r.end_line AS end_line, r.end_col AS end_col,
       r.alias AS alias, r.imported_name AS imported_name"""
# One hop of the re-export walk (issue #2573): the importers of a frontier
# of modules at once, each row naming the module it imports as `to_qn`.
CYPHER_GRAPH_IMPORTERS_OF = """MATCH (m:Module)-[r:IMPORTS]->(target:Module)
WHERE target.qualified_name IN $qns AND m.qualified_name STARTS WITH $project_prefix
RETURN target.qualified_name AS to_qn, m.qualified_name AS qualified_name,
       m.path AS path, r.line AS line, r.col AS col, r.end_line AS end_line,
       r.end_col AS end_col, r.alias AS alias, r.imported_name AS imported_name"""

# Isolated check (issue #1718): the subgraph a scoped re-ingest is about to
# replace, read in full so it can be put back afterwards. The scope is the
# module subtrees at the re-parsed paths, walked exactly as
# `CYPHER_DELETE_MODULE` walks them so the capture equals the delete -- the
# relation list must be kept in step with that query's, or the re-ingest
# deletes nodes the capture never saw and the restore cannot put them back
# (HAS_FIELD was added to the delete by #1899 and missed here: CodeRabbit;
# HAS_VARIANT likewise by #1807, and a test now pins the two lists equal;
# the finding edges joined both with #2536), plus
# the File nodes at those paths and the containers above them (a package
# indicator appearing or vanishing flips the directory's node kind).
_CHECK_SCOPE = f"""MATCH (n)
WHERE (n:{NodeLabel.MODULE.value} AND n.path IN $paths
       AND (n.qualified_name = $project_name
            OR n.qualified_name STARTS WITH $project_prefix))
   OR ((n:{NodeLabel.FILE.value} OR n:{NodeLabel.FOLDER.value}
        OR n:{NodeLabel.PACKAGE.value}) AND n.absolute_path IN $absolute_paths)
MATCH (n)-[:DEFINES|DEFINES_METHOD|CONTAINS_SECTION|HAS_PARAMETER|HAS_FIELD|HAS_VARIANT|DEFINES_CONSTANT|IMPLEMENTS_PATTERN|HAS_SMELL|HAS_VULNERABILITY*0..]->(c)
WITH DISTINCT c"""
CYPHER_CHECK_SCOPE_NODES = f"""{_CHECK_SCOPE}
RETURN labels(c)[0] AS label, properties(c) AS props"""
# Every relationship with at least one end in the scope, either direction,
# with each end addressed by the three key fields the batch writer can key
# a node on (the label decides which one applies). The far end's properties
# come along only for the labels the check can prune, re-grade or rewrite:
# an ExternalModule a new import created, a Resource an endpoint anchored, a
# Gloss whose anchor the re-parse re-graded, and a finding (#1718). Since
# #2536 a finding is also inside the walk, because the module delete takes
# it with its module, so the scope capture already holds the same property
# set and restores the same node.
CYPHER_CHECK_SCOPE_EDGES = f"""{_CHECK_SCOPE}
MATCH (c)-[r]-(x)
RETURN labels(c)[0] AS label, c.qualified_name AS qualified_name,
       c.absolute_path AS absolute_path, c.name AS name,
       type(r) AS rel, startNode(r) = c AS outgoing, properties(r) AS props,
       labels(x)[0] AS far_label, x.qualified_name AS far_qualified_name,
       x.absolute_path AS far_absolute_path, x.name AS far_name,
       CASE WHEN x:{NodeLabel.EXTERNAL_MODULE.value} OR x:{NodeLabel.RESOURCE.value}
            OR x:{_GLOSS} OR x:{NodeLabel.CODE_SMELL.value}
            OR x:{NodeLabel.SECURITY_ISSUE.value} OR x:{NodeLabel.PATTERN.value}
            THEN properties(x) END AS far_props"""
# Findings hang off a Module without being DEFINED by it, keyed on their
# file, line, column and rule: the ones at the scope's paths that the
# capture did not see were written by the check itself. Project-scoped for
# the reason CYPHER_DELETE_MODULE is: `$paths` are repo-relative, two
# projects in the shared graph can hold the same relative path, and a
# path-only match would take the sibling project's findings with it
# (greptile-local, #1718). A finding's qualified name starts with its
# module qn, so the prefix test applies unchanged.
CYPHER_CHECK_DELETE_FINDINGS = f"""MATCH (n)
WHERE (n:{NodeLabel.CODE_SMELL.value} OR n:{NodeLabel.SECURITY_ISSUE.value} OR n:{NodeLabel.PATTERN.value})
  AND n.path IN $paths AND NOT n.qualified_name IN $keep
  AND (n.qualified_name = $project_name
       OR n.qualified_name STARTS WITH $project_prefix)
DETACH DELETE n"""

# Every shared node (ExternalModule, Resource) with its properties and
# inbound-edge count, snapshotted before the isolated check writes (#1718).
# Three uses, one scan:
# - the ones with no inbound edge are the orphans `reingest` itself sweeps
#   repo-wide (CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES), so the restore puts
#   those back;
# - a shared node the scope reaches after the check but absent here was
#   created by the check, and only those are deleted -- "newly reachable" is
#   not "newly created": a pre-existing node another project imports
#   becomes reachable the moment the check adds the same import (bot
#   review);
# - a pre-existing one the check re-emitted gets these properties back
#   verbatim, since a re-parse merges its own (an ExternalModule's `path` is
#   the importing name, not a function of the key).
# The scan is graph-wide because the sweeps it answers to are. The OPTIONAL
# MATCH + count rewrite, not `NOT ()-->(n)`: a pattern expression in a WHERE
# clause is not portable to Memgraph 3, and
# `test_no_pattern_expressions_in_where_clauses` enforces that repo-wide.
CYPHER_CHECK_SHARED_NODES = f"""MATCH (n)
WHERE n:{NodeLabel.EXTERNAL_MODULE.value} OR n:{NodeLabel.RESOURCE.value}
OPTIONAL MATCH (x)-->(n)
WITH n, count(x) AS inbound
RETURN head(labels(n)) AS label, properties(n) AS props, inbound"""

# An IO resource link (FLOWS_TO, RESOLVES_TO) the checked project anchors. An
# isolated check refuses on one: deleting a changed file's subtree can leave
# that resource unanchored, and the re-ingest's repo-wide
# `prune_unanchored_resources` would delete it, chain edges and all, beyond
# what the capture holds (bot review, #1718). Only a Resource one of the
# project's own nodes touches can be unanchored by the run: the prune keeps
# every Resource another node still anchors, and a resource reached only down
# a FLOWS_TO chain puts a FLOWS_TO edge on the one the project touches. So
# another project's links never refuse this one's check (issue #3188). The
# root module (`__init__.py`, `mod.rs`) IS the project name, not under its
# prefix, and the check cuts it like any other changed module. A registered
# project nested under the prefix (`svc.v2` under `svc`) is another project
# too: the module delete spares its nodes (issue #1985), so they are left out
# by the same rule, read from the graph's Project nodes as that delete does.
CYPHER_CHECK_PROJECT_IO_LINKS = f"""OPTIONAL MATCH (longer:Project) WHERE longer.name STARTS WITH $project_prefix
WITH collect(longer.name) AS nested
MATCH (r:Resource)--(n)
WHERE NOT n:Resource AND {_belongs_to_named("n")}
MATCH (r)-[l:FLOWS_TO|RESOLVES_TO]-()
RETURN type(l) AS rel
LIMIT 1"""

# The shared nodes the isolated check created, by label and qualified name: the ones the scope reaches that the snapshot above did not hold.
# Addressed by key rather than by an orphan sweep, so a pre-existing orphan
# elsewhere in the graph -- which this check never touched -- stays (#1718).
# Two parallel lists rather than pairs, so the parameters stay flat.
CYPHER_CHECK_DELETE_SHARED_NODES = """UNWIND range(0, size($labels) - 1) AS i
MATCH (n)
WHERE head(labels(n)) = $labels[i] AND n.qualified_name = $qualified_names[i]
DETACH DELETE n"""

# Trace write-back (issue #1526): a static edge the runtime observed is
# upgraded in place, on every site it has, so the upgrade never creates a
# site-less duplicate beside the located ones.
# Only the static edge(s) of the pair are confirmed: a trace-only edge from
# an earlier run can sit beside a static one on the same pair and must keep
# its `dynamic` label.
# Both endpoints are labelled, so each confirmation is two label + property
# index lookups: unlabelled, every one scanned every node of the whole shared
# graph (issue #3187). A label cannot be a parameter, so only the labels a
# trace frame resolves to (CYPHER_TRACE_CALLABLES) are formatted in.
_TRACE_CALLABLE_LABELS = frozenset(
    {NodeLabel.FUNCTION.value, NodeLabel.METHOD.value, NodeLabel.MODULE.value}
)


def build_trace_confirm_calls_query(from_label: str, to_label: str) -> str:
    for label in (from_label, to_label):
        if label not in _TRACE_CALLABLE_LABELS:
            raise ValueError(f"not a callable label: {label!r}")
    return (
        f"MATCH (a:{from_label} {{qualified_name: $from_qn}})"
        f"-[r:CALLS]->(b:{to_label} {{qualified_name: $to_qn}})\n"
        "WHERE coalesce(r.static_missed, false) = false\n"
        "SET r.resolution = $resolution"
    )


# Reference and construction sites of a definition (issue #1532): everything
# but CALLS that names the symbol, with the site and its resolution.
# Edges that name a definition without calling it (a base class, a parameter
# or return annotation): a rename must rewrite their sites too, and refuses
# when an edge carries none.
# Both are scoped to the renamed node's label: a TS `type input` and `function
# input` are two nodes under one qualified name (issue #2520), and the return
# annotation naming the type is not a site of the function.
CYPHER_GRAPH_TYPE_EDGES = """MATCH (src)-[r:INHERITS|ACCEPTS|RETURNS]->(target)
WHERE target.qualified_name = $qn AND $label IN labels(target)
  AND src.qualified_name STARTS WITH $project_prefix
RETURN labels(src)[0] AS label, src.qualified_name AS qualified_name,
       src.path AS path, type(r) AS rel_type, r.line AS line, r.col AS col,
       r.end_line AS end_line, r.end_col AS end_col, r.resolution AS resolution"""

CYPHER_GRAPH_REFERENCES = """MATCH (src)-[r:REFERENCES|INSTANTIATES]->(target)
WHERE target.qualified_name = $qn AND $label IN labels(target)
  AND src.qualified_name STARTS WITH $project_prefix
RETURN labels(src)[0] AS label, src.qualified_name AS qualified_name,
       src.path AS path, type(r) AS rel_type, r.line AS line, r.col AS col,
       r.end_line AS end_line, r.end_col AS end_col, r.resolution AS resolution,
       r.via_param AS via_param"""
# The CALLS sites where a function runs `$qn` after receiving it as an
# argument (issue #2459). Each spells the parameter, not `$qn`'s name, so a
# rename must leave it alone even where the two are spelled alike.
CYPHER_GRAPH_CALLBACK_SITES = """MATCH (src)-[r:CALLS]->(target)
WHERE target.qualified_name = $qn AND src.qualified_name STARTS WITH $project_prefix
  AND r.via_param IS NOT NULL
RETURN src.qualified_name AS qualified_name, r.line AS line, r.col AS col,
       r.via_param AS via_param"""
