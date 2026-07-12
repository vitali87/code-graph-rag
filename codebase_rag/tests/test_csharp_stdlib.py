# (H) C# Phase 4: System.* namespaces fold the trailing type into the namespace
# (H) path (external stdlib), like the Java model.
import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers import stdlib_extractor as se
from codebase_rag.parsers.stdlib_extractor import StdlibExtractor


@pytest.fixture(autouse=True)
def _reset_stdlib_cache() -> None:
    # (H) The extractor memoizes (and disk-persists) results; clear so a stale
    # (H) entry from another test or run cannot mask the real resolution.
    se._STDLIB_CACHE.clear()
    se._CACHE_TIMESTAMPS.clear()


def _path(fqn: str) -> str:
    return StdlibExtractor().extract_module_path(fqn, cs.SupportedLanguage.CSHARP)


def test_system_types_fold_into_namespace() -> None:
    assert _path("System.Collections.Generic.List") == "System.Collections.Generic"
    assert _path("System.Linq.Enumerable") == "System.Linq"
    assert _path("System.Threading.Tasks.Task") == "System.Threading.Tasks"


def test_non_type_member_keeps_full_path() -> None:
    # (H) A lowercase trailing segment is a member/namespace, not a type: keep it.
    assert _path("System.Console.writeLine") == "System.Console.writeLine"


def test_known_namespace_is_not_folded_as_a_type() -> None:
    # (H) C# namespaces are PascalCase like types, so a bare namespace reference
    # (H) must not be folded into its parent (a `using System.Text.Json;` names a
    # (H) namespace, not a `Json` type in System.Text).
    assert _path("System.Text.Json") == "System.Text.Json"
    assert _path("System.Collections.Generic") == "System.Collections.Generic"
    assert _path("System.Threading.Tasks") == "System.Threading.Tasks"
