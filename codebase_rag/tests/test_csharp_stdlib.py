# (H) C# Phase 4: System.* namespaces fold the trailing type into the namespace
# (H) path (external stdlib), like the Java model.
from codebase_rag import constants as cs
from codebase_rag.parsers.stdlib_extractor import StdlibExtractor


def _path(fqn: str) -> str:
    return StdlibExtractor().extract_module_path(fqn, cs.SupportedLanguage.CSHARP)


def test_system_types_fold_into_namespace() -> None:
    assert _path("System.Collections.Generic.List") == "System.Collections.Generic"
    assert _path("System.Linq.Enumerable") == "System.Linq"
    assert _path("System.Threading.Tasks.Task") == "System.Threading.Tasks"


def test_non_type_member_keeps_full_path() -> None:
    # (H) A lowercase trailing segment is a member/namespace, not a type: keep it.
    assert _path("System.Console.writeLine") == "System.Console.writeLine"
