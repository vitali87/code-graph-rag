# (H) Pure-function coverage for the C# type-reference helpers: the CLR-style
# (H) arity annotation must round-trip through every stored type map, and the
# (H) arity counter must ignore nested generics and array brackets.
from codebase_rag.parsers.csharp.utils import (
    annotate_type_ref,
    generic_arity_of_type_text,
    split_type_ref,
)


def test_generic_arity_counts_top_level_arguments_only() -> None:
    assert generic_arity_of_type_text("Builder") == 0
    assert generic_arity_of_type_text("Builder<T>") == 1
    assert generic_arity_of_type_text("Map<K, List<V>>") == 2
    assert generic_arity_of_type_text("Func<Tuple<A, B>, C>") == 2
    assert generic_arity_of_type_text("string[]") == 0


def test_annotate_and_split_round_trip() -> None:
    assert annotate_type_ref("Builder") == "Builder"
    assert annotate_type_ref("Builder<T>") == "Builder`1"
    assert annotate_type_ref("Options<TResult>?") == "Options`1"
    assert split_type_ref("Builder`1") == ("Builder", 1)
    assert split_type_ref("Builder") == ("Builder", 0)
    # (H) A backtick with a non-numeric tail is not an arity marker.
    assert split_type_ref("Weird`name") == ("Weird`name", 0)
