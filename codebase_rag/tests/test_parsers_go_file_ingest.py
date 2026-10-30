"""`GoFileIngest` records the package a Go file's `package` clause puts it
in, which the package-block resolution groups files by."""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.parsers.go.file_ingest import GoFileIngest
from codebase_rag.tests.collaborator_support import parse


def test_a_files_package_clause_is_recorded_under_its_module() -> None:
    facts = DefinitionFacts()
    ingest = GoFileIngest(facts)
    ingest.record_module_facts(
        "p.svc.a", parse(cs.SupportedLanguage.GO, "package svc\n")
    )
    ingest.record_module_facts(
        "p.svc.a_test", parse(cs.SupportedLanguage.GO, "package svc_test\n")
    )
    # Two files of one directory may be two packages.
    assert facts.go_package_names == {"p.svc.a": "svc", "p.svc.a_test": "svc_test"}


def test_a_file_without_a_package_clause_records_nothing() -> None:
    facts = DefinitionFacts()
    GoFileIngest(facts).record_module_facts(
        "p.broken", parse(cs.SupportedLanguage.GO, "func f() {}\n")
    )
    assert facts.go_package_names == {}


def test_a_later_parse_replaces_the_recorded_package() -> None:
    facts = DefinitionFacts()
    ingest = GoFileIngest(facts)
    ingest.record_module_facts("p.a", parse(cs.SupportedLanguage.GO, "package old\n"))
    ingest.record_module_facts("p.a", parse(cs.SupportedLanguage.GO, "package new\n"))
    assert facts.go_package_names == {"p.a": "new"}
