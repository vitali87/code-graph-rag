"""Messages and descriptors for gloss notes."""

MCP_GLOSS_TARGET_NOT_FOUND = "No definition matches {target!r} in project {project!r}."
# A property's getter, setter and deleter share one qualified name; the
# registry names the later ones `x@<line>`, which shifts with the file. The
# descriptor names the member stably (issue #1808).
GLOSS_DESCRIPTOR_SEPARATOR = "#"
GLOSS_DESCRIPTORS: dict[str, str] = {
    "getter": "@property",
    "setter": ".setter",
    "deleter": ".deleter",
}
MCP_GLOSS_DESCRIPTOR_UNKNOWN = (
    "{descriptor!r} is not a descriptor; use one of {known} after '#'."
)
MCP_GLOSS_DESCRIPTOR_NOT_FOUND = (
    "{target!r} resolved to {qn!r}, but no definition of that name carries the "
    "{descriptor} decorator."
)
MCP_GLOSS_TARGET_AMBIGUOUS = (
    "{target!r} names {count} definitions; pass one of the qualified names "
    "listed under 'candidates'."
)
MCP_GLOSS_MENTION_REFUSED = "mention {name!r}: {error}"
MCP_GLOSS_KIND_UNKNOWN = "Unknown gloss kind {kind!r}. Kinds: {kinds}"
MCP_GLOSS_BODY_EMPTY = "A gloss needs a non-empty body."
MCP_GLOSS_NOT_WRITTEN = (
    "The gloss on {target!r} was not written: its subject or a mentioned "
    "definition left the graph between resolving and writing. Re-index and retry."
)
