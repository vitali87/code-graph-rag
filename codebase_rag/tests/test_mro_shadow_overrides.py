# (H) A mixin's method can shadow a same-name method from a SIBLING base branch
# (H) only in a combining subclass's MRO: django's
# (H) SearchVector(SearchVectorCombinable, Func) dispatches Combinable's
# (H) `self._combine()` calls to SearchVectorCombinable._combine, yet the mixin
# (H) never inherits Combinable, so the per-class override walk sees nothing
# (H) and dead-code reports the mixin method. The shadow pass must link the
# (H) first provider in a class's linearized ancestry to the later ones.
from __future__ import annotations

from pathlib import Path

from evals.cgr_graph import _capture
from evals.dead_code import cgr_dead_code, default_dead_code_config

MIXIN_SHADOW_PY = """\
class Combinable:
    def combine_or(self):
        return self._combine()

    def _combine(self):
        return 1


class SearchVectorCombinable:
    def _combine(self):
        return 2


class SearchVector(SearchVectorCombinable, Combinable):
    pass
"""

UNRELATED_PY = """\
class Left:
    def use(self):
        return self._helper()

    def _helper(self):
        return 1


class Right:
    def _helper(self):
        return 2
"""


def _overrides(root: Path) -> set[tuple[str, str]]:
    ingestor = _capture(root, "proj")
    return {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "OVERRIDES"
    }


def test_sibling_mixin_shadow_gets_overrides_edge(tmp_path: Path) -> None:
    root = tmp_path / "shadow"
    root.mkdir()
    (root / "search.py").write_text(MIXIN_SHADOW_PY, encoding="utf-8")

    assert (
        "proj.search.SearchVectorCombinable._combine",
        "proj.search.Combinable._combine",
    ) in _overrides(root)


def test_sibling_mixin_shadow_method_is_not_dead(tmp_path: Path) -> None:
    root = tmp_path / "shadow_dead"
    root.mkdir()
    (root / "search.py").write_text(MIXIN_SHADOW_PY, encoding="utf-8")

    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    assert not any("_combine" in qn for qn in dead)


def test_unrelated_same_name_methods_stay_independent(tmp_path: Path) -> None:
    # (H) No class combines Left and Right, so Right._helper is not a dispatch
    # (H) target of Left's call and must stay reported.
    root = tmp_path / "unrelated"
    root.mkdir()
    (root / "mod.py").write_text(UNRELATED_PY, encoding="utf-8")

    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    assert any(qn.endswith("Right._helper") for qn in dead)


DIAMOND_PY = """\
class A:
    def use(self):
        return self.m()

    def m(self):
        return 1


class B(A):
    pass


class C(A):
    def m(self):
        return 2


class D(B, C):
    pass
"""

PRECEDENCE_PY = """\
class A:
    def m(self):
        return 1


class B(A):
    pass


class C:
    def use(self):
        return self.m()

    def m(self):
        return 2


class E(B, C):
    pass
"""


def test_diamond_common_ancestor_does_not_shadow_sibling(tmp_path: Path) -> None:
    # (H) D(B, C) with B(A), C(A): the C3 MRO is [D, B, C, A], so C.m is D's
    # (H) dispatch target and the normal per-method walk already links
    # (H) C.m -> A.m. A depth-first ancestry would visit A (via B) before C
    # (H) and emit the REVERSED edge A.m -> C.m, wrongly reviving a dead A.m
    # (H) whenever C.m is live.
    root = tmp_path / "diamond"
    root.mkdir()
    (root / "d.py").write_text(DIAMOND_PY, encoding="utf-8")

    assert ("proj.d.A.m", "proj.d.C.m") not in _overrides(root)


def test_inherited_branch_method_shadows_later_sibling(tmp_path: Path) -> None:
    # (H) E(B, C) with B(A) and standalone C: the C3 MRO is [E, B, A, C], so
    # (H) A.m (inherited through B) shadows C.m for E instances and the edge
    # (H) A.m -> C.m is correct.
    root = tmp_path / "precedence"
    root.mkdir()
    (root / "p.py").write_text(PRECEDENCE_PY, encoding="utf-8")

    assert ("proj.p.A.m", "proj.p.C.m") in _overrides(root)


TWO_COMBINERS_PY = """\
class Combinable:
    def combine_or(self):
        return self._combine()

    def _combine(self):
        return 1


class SearchVectorCombinable:
    def _combine(self):
        return 2


class SearchVector(SearchVectorCombinable, Combinable):
    pass


class SearchText(SearchVectorCombinable, Combinable):
    pass
"""


def test_shadow_edge_emitted_once_across_combining_classes(tmp_path: Path) -> None:
    # (H) Two classes combining the same mixin pair surface the same shadow
    # (H) pair twice; the edge must be emitted exactly once.
    root = tmp_path / "twocombine"
    root.mkdir()
    (root / "s.py").write_text(TWO_COMBINERS_PY, encoding="utf-8")

    ingestor = _capture(root, "proj")
    shadow_edges = [
        (f, t)
        for _fl, f, rel, _tl, t in ingestor.rels
        if rel == "OVERRIDES"
        and (f, t)
        == ("proj.s.SearchVectorCombinable._combine", "proj.s.Combinable._combine")
    ]
    assert len(shadow_edges) == 1
