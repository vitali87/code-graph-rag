"""The Go joins a sync runs once Pass 2 has registered every type: go/types'
implements pairs onto the type nodes, and the interface link over the
complete method sets."""

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...services import IngestorProtocol
from ..definition_processor import DefinitionProcessor


def join_implements(dp: DefinitionProcessor, sink: IngestorProtocol) -> None:
    # go/types proved each implementer->interface pair structurally; both
    # ends carry a declaring-identifier position that resolves to the Pass-2
    # type node through go.type_locations. On a two-sided hit, emit the
    # IMPLEMENTS edge and record the implementer so a call through the
    # interface with a SOLE implementer also edges to the concrete method
    # (resolver.interface_sole_impl_targets, no extra wiring). A miss on
    # either end drops the pair rather than risk a dangling edge. The
    # pairs replace the previous pass's, so a reused updater forgets one
    # the compiler no longer proves.
    proven: list[tuple[str, str]] = []
    for pair in dp.facts.frontend_facts[cs.SupportedLanguage.GO].implements_pairs:
        impl = dp.go.type_locations.get((pair.impl_file, pair.impl_line, pair.impl_col))
        iface = dp.go.type_locations.get(
            (pair.iface_file, pair.iface_line, pair.iface_col)
        )
        if impl is None or iface is None:
            continue
        impl_qn, impl_label = impl
        iface_qn, iface_label = iface
        # Through `sink` (the capture-filtering wrapper), not the raw
        # ingestor, so a capture that disables IMPLEMENTS suppresses this
        # edge too -- graph-element emission must honour the capture contract.
        sink.ensure_relationship_batch(
            (impl_label, cs.KEY_QUALIFIED_NAME, impl_qn),
            cs.RelationshipType.IMPLEMENTS,
            (iface_label, cs.KEY_QUALIFIED_NAME, iface_qn),
        )
        proven.append((iface_qn, impl_qn))
    dp.replace_go_implementers(dp.go.proven_pairs, proven, keep=dp.go.guessed_pairs)
    if proven:
        logger.info(ls.GO_FRONTEND_IMPLEMENTS_JOINED.format(count=len(proven)))


def link_interfaces(dp: DefinitionProcessor) -> None:
    # Where go/types analysed a module it already proved which of its
    # types implement which of its interfaces (join_implements), even
    # when it proved none; names and arities decide only elsewhere. Keyed
    # on the modules it analysed, not on whether it returned any facts.
    facts = dp.facts.frontend_facts[cs.SupportedLanguage.GO]
    dp.link_go_interfaces(
        frozenset(facts.analysed_roots),
        facts.checked_files,
        frozenset(
            located[0]
            for position in facts.compared_types
            if (located := dp.go.type_locations.get(position)) is not None
        ),
    )
