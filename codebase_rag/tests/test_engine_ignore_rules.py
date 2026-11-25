"""The frame's ignore rules against pathspec and path_utils.

Phase 2's step 4 (docs/reports/PHASE2_FRAME.md, sections 5 and 13) ports the
walk's ignore rules to Rust: pathspec's pattern-to-regex translation, the
ordered `.gitignore` blocks, and the deciding functions of
`codebase_rag/utils/path_utils.py`. The port must answer exactly as they do.
A corpus of patterns crossed with a corpus of paths is asked of both, every
pattern the existing ignore tests use among them, and those tests run once
more with every decision they make asked of the Rust rules too
(`ignore_rules_shadow.py`). A pattern Python refuses must be refused too:
an answer is a bool or "raises".
"""

from __future__ import annotations

import ast
import itertools
import os
import random
import re
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterable, Iterator
from functools import cache
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.config import _gitignore_excludes
from codebase_rag.tests.ignore_rules_corpus import (
    DECISION_SEGMENTS,
    EXCLUDE_POOL,
    HAND_PATHS,
    HAND_PATTERNS,
    PATH_SEGMENTS,
    PATTERN_PREFIXES,
    PATTERN_STEMS,
    PATTERN_SUFFIXES,
    UNIGNORE_POOL,
)
from codebase_rag.utils import path_utils
from codebase_rag.utils.path_utils import ordered_ignore_block

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
SHADOW_PLUGIN = "codebase_rag.tests.ignore_rules_shadow"
# The ignore tests whose patterns join the corpus, and which run again with
# the Rust rules shadowing every decision.
IGNORE_TEST_FILES = (
    "test_gitignore_patterns.py",
    "test_gitignore_negation.py",
    "test_gitignore_ignore_semantics.py",
    "test_cgrignore.py",
    "test_exclude_patterns.py",
    "test_tracked_source_dirs.py",
)
RAISES = "raises"
SHOWN = 15
SEED = 3629

Answer = bool | str


def _python(call: Callable[[], bool]) -> Answer:
    # Python's `re` warns about sets like `[[` and `--`; a warning is no
    # answer.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return call()
        except Exception:
            return RAISES


def _rust(call: Callable[[], bool]) -> Answer:
    try:
        return call()
    except ValueError:
        return RAISES


def _rules(
    exclude: Iterable[str] = (), unignore: Iterable[str] = ()
) -> engine_frame.IgnoreRules:
    rules = engine_frame.new_ignore_rules(exclude, unignore)
    # The checkout builds its own crate, so the rules are always there.
    assert rules is not None, "the extension lacks IgnoreRules; rebuild the crate"
    return rules


def _differences(
    pairs: Iterable[tuple[object, Answer, Answer]],
) -> tuple[int, list[str]]:
    count = 0
    differing = []
    for case, expected, actual in pairs:
        count += 1
        if expected != actual:
            differing.append(f"{case!r}: Python {expected!r}, Rust {actual!r}")
    return count, differing


def _assert_same(pairs: Iterable[tuple[object, Answer, Answer]], at_least: int) -> None:
    count, differing = _differences(pairs)
    assert not differing, f"{len(differing)} of {count} answers differ:\n" + "\n".join(
        differing[:SHOWN]
    )
    # The corpus must not shrink unnoticed.
    assert count >= at_least, count


def _docstrings(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


@cache
def _test_strings() -> frozenset[str]:
    """Every string literal of the ignore tests but their docstrings."""
    found: set[str] = set()
    for name in IGNORE_TEST_FILES:
        tree = ast.parse((TESTS / name).read_text(encoding=cs.ENCODING_UTF8))
        docstrings = _docstrings(tree)
        found.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        )
    return frozenset(found)


def _file_lines(text: str) -> tuple[str, ...]:
    # What `_read_ignore_lines` keeps of an ignore file.
    stripped = (line.strip() for line in text.split("\n"))
    return tuple(line for line in stripped if line and not line.startswith("#"))


@cache
def _test_patterns() -> tuple[str, ...]:
    """Each literal of the ignore tests as a pattern, an ignore file's
    contents line by line, and each `!` line's unignore too."""
    patterns: set[str] = set()
    for text in _test_strings():
        for line in text.split("\n"):
            if len(line) > 120:
                continue
            patterns.update({line, line.strip()})
            if line.strip().startswith(cs.GITIGNORE_NEGATION):
                patterns.add(line.strip()[1:].strip())
    return tuple(sorted(patterns))


@cache
def _test_blocks() -> tuple[str, ...]:
    """The ordered blocks the ignore tests' `.gitignore` contents load as."""
    blocks: set[str] = set()
    for text in _test_strings():
        if "\n" not in text:
            continue
        lines = _file_lines(text)
        blocks.update(
            entry
            for entry in _gitignore_excludes(lines, frozenset())
            if cs.IGNORE_BLOCK_SEPARATOR in entry
        )
    return tuple(sorted(blocks))


@cache
def _test_paths() -> tuple[str, ...]:
    paths: set[str] = set()
    for text in _test_strings():
        if "\n" in text or len(text) > 120:
            continue
        if "/" in text or "." in text:
            paths.add(text)
            paths.add(f"{text.rstrip('/')}/")
    return tuple(sorted(paths))


@cache
def _patterns() -> tuple[str, ...]:
    generated = (
        f"{prefix}{stem}{suffix}"
        for prefix, stem, suffix in itertools.product(
            PATTERN_PREFIXES, PATTERN_STEMS, PATTERN_SUFFIXES
        )
    )
    return tuple(sorted({*HAND_PATTERNS, *_test_patterns(), *generated}))


def _generated_paths(segments: tuple[str, ...], depth: int) -> Iterator[str]:
    for length in range(1, depth + 1):
        for parts in itertools.product(segments, repeat=length):
            path = "/".join(parts)
            yield path
            yield f"{path}/"


@cache
def _paths() -> tuple[str, ...]:
    return tuple(
        sorted({*HAND_PATHS, *_test_paths(), *_generated_paths(PATH_SEGMENTS, 2)})
    )


def test_the_rules_hold_the_built_in_names_and_endings() -> None:
    rules_type = type(_rules())
    unconditional = frozenset(getattr(rules_type, "UNCONDITIONAL_IGNORE_SUFFIXES"))
    rescuable = frozenset(getattr(rules_type, "RESCUABLE_IGNORE_SUFFIXES"))
    assert frozenset(getattr(rules_type, "BUILTIN_IGNORED_DIRS")) == cs.IGNORE_PATTERNS
    assert frozenset(getattr(rules_type, "VCS_METADATA_DIRS")) == cs.VCS_METADATA_DIRS
    assert unconditional == cs.UNCONDITIONAL_IGNORE_SUFFIXES
    assert rescuable == cs.RESCUABLE_IGNORE_SUFFIXES


def test_the_corpus_holds_every_pattern_of_the_ignore_tests() -> None:
    patterns = set(_patterns())
    for expected in ("src/gen/*.py", "**/fixtures/**", "*.[Cc]ache/", "/vendor/*"):
        assert expected in patterns
    assert set(_test_patterns()) <= patterns
    assert _test_blocks(), "no .gitignore with a `!` line found in the ignore tests"


def test_each_pattern_matches_as_pathspec_does() -> None:
    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for pattern in _patterns():
            patterns = frozenset({pattern})
            rules = _rules([pattern])
            for path in _paths():
                yield (
                    (pattern, path),
                    _python(lambda: path_utils.matches_ignore_patterns(path, patterns)),
                    _rust(lambda: rules.matches_exclude(path)),
                )

    _assert_same(pairs(), at_least=200_000)


def test_sets_of_patterns_match_as_pathspec_does() -> None:
    # Several patterns at once: the last match in sorted order decides, so
    # a `!` pattern can undo another.
    rng = random.Random(SEED)
    pool = sorted({*HAND_PATTERNS, *PATTERN_STEMS, *EXCLUDE_POOL})
    sets = [frozenset(rng.sample(pool, rng.randint(2, 6))) for _ in range(300)]

    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for patterns in sets:
            rules = _rules(patterns)
            for path in _paths():
                yield (
                    (sorted(patterns), path),
                    _python(lambda: path_utils.matches_ignore_patterns(path, patterns)),
                    _rust(lambda: rules.matches_exclude(path)),
                )

    _assert_same(pairs(), at_least=100_000)


def _blocks() -> list[str]:
    rng = random.Random(SEED)
    lines = sorted({*HAND_PATTERNS, *PATTERN_STEMS, *_generated_block_lines()} - {""})
    generated = [
        ordered_ignore_block(
            rng.choice(lines) if rng.random() < 0.6 else f"!{rng.choice(lines)}"
            for _ in range(rng.randint(2, 5))
        )
        for _ in range(300)
    ]
    return [*_test_blocks(), *generated]


def _generated_block_lines() -> Iterator[str]:
    for prefix, stem, suffix in itertools.product(
        ("", "/", "**/", "a/"), ("a", "*.py", "*", "b"), ("", "/", "/**", "/**/", "/*")
    ):
        yield f"{prefix}{stem}{suffix}"


def test_ordered_blocks_judge_each_level_as_path_utils_does() -> None:
    blocks = _blocks()
    paths = sorted(
        {*HAND_PATHS, *_generated_paths(("a", "b", "c.py", "src", "gen"), 3)}
    )

    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for block in blocks:
            patterns = frozenset({block})
            rules = _rules([block])
            for path in paths:
                yield (
                    (block, path),
                    _python(lambda: path_utils.matches_ignore_patterns(path, patterns)),
                    _rust(lambda: rules.matches_exclude(path)),
                )

    _assert_same(pairs(), at_least=100_000)


def test_the_unignore_helpers_answer_as_path_utils_does() -> None:
    patterns = sorted({*_patterns(), *UNIGNORE_POOL})
    dirs = sorted(
        {path.rstrip("/") for path in _paths()} | {"sub/.git", ".git", "a/b/.git"}
    )
    files = [path for path in _paths() if not path.endswith("/")]

    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for pattern in patterns:
            rules = _rules(unignore=[pattern])
            single = frozenset({pattern})
            for rel_dir in dirs:
                yield (
                    ("unignore_could_match_within", pattern, rel_dir),
                    _python(
                        lambda: path_utils.unignore_could_match_within(pattern, rel_dir)
                    ),
                    _rust(lambda: rules.unignore_could_match_within(rel_dir)),
                )
                yield (
                    ("unignore_names_vcs_dir", pattern, rel_dir),
                    _python(lambda: path_utils.unignore_names_vcs_dir(rel_dir, single)),
                    _rust(lambda: rules.unignore_names_vcs_dir(rel_dir)),
                )
            for rel_path in files:
                yield (
                    ("unignore_names_this_file", pattern, rel_path),
                    _python(
                        lambda: path_utils.unignore_names_this_file(rel_path, single)
                    ),
                    _rust(lambda: rules.unignore_names_this_file(rel_path)),
                )

    _assert_same(pairs(), at_least=200_000)


def _instance(glob: str, rng: random.Random, alphabet: str) -> str:
    # A name the glob may match: wildcards filled in, the rest mostly kept.
    out = []
    for c in glob:
        if c == "*":
            out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 2))))
        elif c == "?" or rng.random() < 0.2:
            out.append(rng.choice(alphabet))
        else:
            out.append(c)
    return "".join(out)


def test_unignore_segments_match_directory_names_as_fnmatch_does() -> None:
    # `_pattern_reaches_dir` matches each directory name with
    # `fnmatch.fnmatchcase`: random globs over the characters its sets read
    # specially, against names made from each glob and names of their own.
    rng = random.Random(SEED)
    specials = "ab-!]^[\\*?&~|é."
    plain = "ab-!]^[\\&~|é."
    globs = sorted(
        {
            "".join(rng.choice(specials) for _ in range(rng.randint(1, 8)))
            for _ in range(4000)
        }
    )
    shared = sorted(
        {
            "".join(rng.choice(plain) for _ in range(rng.randint(1, 4)))
            for _ in range(30)
        }
    )

    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for glob in globs:
            unignore = frozenset({f"x/{glob}"})
            rules = _rules(unignore=unignore)
            own = {_instance(glob, rng, plain) for _ in range(10)}
            for name in sorted(own.union(shared) - {""}):
                rel_dir = f"x/{name}"
                yield (
                    (glob, name),
                    _python(
                        lambda: path_utils.unignore_names_vcs_dir(rel_dir, unignore)
                    ),
                    _rust(lambda: rules.unignore_names_vcs_dir(rel_dir)),
                )

    _assert_same(pairs(), at_least=100_000)


def _decision_paths() -> list[str]:
    return sorted({*HAND_PATHS, *_generated_paths(DECISION_SEGMENTS, 2)} - {"", "/"})


def test_the_walk_decisions_are_path_utils_decisions() -> None:
    rng = random.Random(SEED)
    rule_sets = [
        (
            frozenset(rng.sample(EXCLUDE_POOL, rng.randint(0, 2))),
            frozenset(rng.sample(UNIGNORE_POOL, rng.randint(0, 3))),
        )
        for _ in range(120)
    ]
    rule_sets += [(frozenset(), frozenset()), (frozenset(), frozenset({""}))]
    paths = _decision_paths()

    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for exclude, unignore in rule_sets:
            rules = _rules(exclude, unignore)
            ex, un = exclude or None, unignore or None
            for path in paths:
                rel = path.rstrip("/")
                parts = tuple(rel.split("/"))
                case = (sorted(exclude), sorted(unignore), path)
                if path.endswith("/"):
                    prefix = "".join(f"{part}/" for part in parts[:-1])
                    yield (
                        ("should_keep_dir", *case),
                        _python(
                            lambda: path_utils.should_keep_dir(
                                parts[-1], prefix, ex, un
                            )
                        ),
                        _rust(lambda: rules.should_keep_dir(parts[-1], prefix)),
                    )
                else:
                    yield (
                        ("should_skip_rel_file", *case),
                        _python(
                            lambda: path_utils.should_skip_rel_file(
                                rel, parts[:-1], ex, un
                            )
                        ),
                        _rust(lambda: rules.should_skip_rel_file(rel, parts[:-1])),
                    )

    _assert_same(pairs(), at_least=50_000)


def test_the_path_decision_is_should_skip_path(tmp_path: Path) -> None:
    # `should_skip_path` past its containment check, which reads the disk:
    # the paths need not exist, since `is_file` is given.
    rng = random.Random(SEED)
    rule_sets = [
        (
            frozenset(rng.sample(EXCLUDE_POOL, rng.randint(0, 2))),
            frozenset(rng.sample(UNIGNORE_POOL, rng.randint(0, 3))),
        )
        for _ in range(60)
    ]
    paths = _decision_paths()

    # The relative spelling `should_skip_path` reads, for paths inside the
    # root (the rest are skipped before any rule is asked).
    inside = {
        path: (tmp_path / path).relative_to(tmp_path).as_posix()
        for path in paths
        if (tmp_path / path).resolve().is_relative_to(tmp_path.resolve())
    }

    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for exclude, unignore in rule_sets:
            rules = _rules(exclude, unignore)
            ex, un = exclude or None, unignore or None
            for path, rel in inside.items():
                is_file = not path.endswith("/")
                yield (
                    (sorted(exclude), sorted(unignore), path),
                    _python(
                        lambda: path_utils.should_skip_path(
                            tmp_path / path, tmp_path, ex, un, is_file=is_file
                        )
                    ),
                    _rust(lambda: rules.should_skip_rel_path(rel, is_file)),
                )

    _assert_same(pairs(), at_least=20_000)


def test_python_whitespace_is_stripped_as_python_strips_it() -> None:
    # `str.strip()` and pathspec's `rstrip()` take the characters Python
    # counts as space, which are not quite Rust's `char::is_whitespace`.
    # Every space character is below U+3100.
    candidates = [chr(c) for c in range(0x3100) if not 0xD800 <= c <= 0xDFFF]
    candidates += ["\u200b", "\ufeff", "\U0001f389"]

    def pairs() -> Iterator[tuple[object, Answer, Answer]]:
        for c in candidates:
            padded = f"{c}jquery.min.js{c}"
            rules = _rules(exclude=[f"jquery{c}"], unignore=[padded])
            yield (
                ("strip", hex(ord(c))),
                _python(
                    lambda: path_utils.unignore_names_this_file(
                        "a/jquery.min.js", frozenset({padded})
                    )
                ),
                _rust(lambda: rules.unignore_names_this_file("a/jquery.min.js")),
            )
            yield (
                ("rstrip", hex(ord(c))),
                _python(
                    lambda: path_utils.matches_ignore_patterns(
                        "jquery", frozenset({f"jquery{c}"})
                    )
                ),
                _rust(lambda: rules.matches_exclude("jquery")),
            )

    _assert_same(pairs(), at_least=20_000)


def test_a_huge_pattern_is_refused_or_matched() -> None:
    # The regex crate bounds what one pattern may compile to; pathspec does
    # not, so this is the one refusal Python never makes.
    rules = _rules(["*" * 100_000])
    try:
        answer = rules.matches_exclude("a")
    except ValueError as error:
        assert "compiles to more than" in str(error)
    else:
        assert answer is True


def test_every_ignore_test_agrees_with_the_rust_rules() -> None:
    # The existing ignore tests, with each decision they make asked of the
    # Rust rules as well; any difference fails the test that made it.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_XDIST", "PYTEST_CURRENT_TEST"))
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            SHADOW_PLUGIN,
            *(str(TESTS / name) for name in IGNORE_TEST_FILES),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=900,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-8000:]
    summary = re.search(
        r"ignore-rules shadow: (\d+) decisions compared, (\d+) differed", output
    )
    assert summary is not None, output[-4000:]
    compared, differed = int(summary[1]), int(summary[2])
    assert differed == 0, output[-8000:]
    assert compared > 1000, output[-4000:]


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # Answered as pathspec answers, where git answers otherwise: a
        # negated set matches `/`, `docs/*` stops at the first level, and
        # Python's `$` matches before a final newline.
        ("a[!b]c", "a/c", True),
        ("docs/*", "docs/a/b.md", False),
        ("x", "x\n", True),
    ],
)
def test_pathspec_oddities_are_kept(pattern: str, path: str, expected: bool) -> None:
    assert path_utils.matches_ignore_patterns(path, frozenset({pattern})) is expected
    assert _rules([pattern]).matches_exclude(path) is expected


def _python_answer(
    method: str,
    args: tuple[object, ...],
    exclude: frozenset[str],
    unignore: frozenset[str],
    root: Path,
) -> Answer:
    ex, un = exclude or None, unignore or None
    calls: dict[str, Callable[..., bool]] = {
        "matches_exclude": lambda rel: path_utils.matches_ignore_patterns(rel, exclude),
        "should_keep_dir": lambda name, prefix: path_utils.should_keep_dir(
            name, prefix, ex, un
        ),
        "should_skip_rel_file": lambda rel, parts: path_utils.should_skip_rel_file(
            rel, parts, ex, un
        ),
        "should_skip_rel_path": lambda rel, is_file: path_utils.should_skip_path(
            root / rel, root, ex, un, is_file=is_file
        ),
        "unignore_names_this_file": lambda rel: path_utils.unignore_names_this_file(
            rel, unignore
        ),
        "unignore_could_match_within": lambda rel_dir: any(
            path_utils.unignore_could_match_within(u, rel_dir) for u in unignore
        ),
        "unignore_names_vcs_dir": lambda rel_dir: path_utils.unignore_names_vcs_dir(
            rel_dir, un
        ),
    }
    return _python(lambda: calls[method](*args))


# A name whose bytes are not UTF-8 reaches Python with each stray byte as a
# lone surrogate (`os.fsdecode`'s surrogateescape: b"\xff" is "\udcff"). The
# walk asks the ignore rules about such a name before it reports the name as
# undecodable, and a pattern may hold one too, so the rules answer for them
# as Python does: a surrogate is one character to `?`, `*` and `[...]`.
@pytest.mark.parametrize(
    ("exclude", "unignore", "method", "args", "expected"),
    [
        pytest.param(
            ("*.o",),
            (),
            "should_skip_rel_file",
            ("bad\udcff.o", ()),
            True,
            id="an-object-file",
        ),
        pytest.param(
            ("*.c",),
            (),
            "should_skip_rel_file",
            ("src/bad\udcff.c", ("src",)),
            True,
            id="a-file-an-exclude-matches",
        ),
        pytest.param(
            ("*.o",),
            (),
            "should_skip_rel_file",
            ("bad\udcff.c", ()),
            False,
            id="a-file-no-rule-matches",
        ),
        pytest.param(
            (),
            (),
            "should_skip_rel_file",
            ("vendor/bad\udcff.c", ("vendor",)),
            True,
            id="a-file-in-a-built-in-dir",
        ),
        pytest.param(
            ("x?/",),
            (),
            "should_skip_rel_file",
            ("x\udcff/a.c", ("x\udcff",)),
            True,
            id="a-file-in-an-excluded-dir",
        ),
        pytest.param(
            ("gen?/",),
            (),
            "should_keep_dir",
            ("gen\udcff", ""),
            False,
            id="a-dir-an-exclude-matches",
        ),
        pytest.param(
            ("gen?/",),
            (),
            "should_keep_dir",
            ("gen\udcff\udcfe", "src/"),
            True,
            id="a-dir-no-rule-matches",
        ),
        pytest.param(
            (),
            ("x\udcff/node_modules/keep.js",),
            "should_keep_dir",
            ("node_modules", "x\udcff/"),
            True,
            id="a-built-in-dir-an-unignore-reaches",
        ),
        pytest.param(
            (),
            ("x\udcff/node_modules/keep.js",),
            "should_keep_dir",
            ("node_modules", "x\udcfe/"),
            False,
            id="a-built-in-dir-an-unignore-misses",
        ),
        pytest.param(
            ("a?b",),
            (),
            "matches_exclude",
            ("a\udcffb",),
            True,
            id="question-mark-takes-one-surrogate",
        ),
        pytest.param(
            ("a?b",),
            (),
            "matches_exclude",
            ("a\udcff\udcfeb",),
            False,
            id="question-mark-takes-no-more",
        ),
        pytest.param(
            ("bad\udcff*",),
            (),
            "matches_exclude",
            ("bad\udcff.c",),
            True,
            id="a-pattern-holding-the-surrogate",
        ),
        pytest.param(
            ("bad\udcff*",),
            (),
            "matches_exclude",
            ("bad\udcfe.c",),
            False,
            id="a-pattern-holding-another-surrogate",
        ),
        pytest.param(
            ("a[\udc80-\udcff]b",),
            (),
            "matches_exclude",
            ("a\udcffb",),
            True,
            id="a-set-of-surrogates-holding-it",
        ),
        pytest.param(
            ("a[\udc80-\udcff]b",),
            (),
            "matches_exclude",
            ("a\udc7fb",),
            False,
            id="a-set-of-surrogates-without-it",
        ),
        pytest.param(
            (ordered_ignore_block(["*.c", "!x\udcff.c"]),),
            (),
            "matches_exclude",
            ("x\udcff.c",),
            False,
            id="an-ordered-block-re-including-it",
        ),
        pytest.param(
            (ordered_ignore_block(["*.c", "!x\udcff.c"]),),
            (),
            "matches_exclude",
            ("x\udcfe.c",),
            True,
            id="an-ordered-block-re-including-another",
        ),
        pytest.param(
            (),
            (),
            "should_skip_rel_path",
            ("bad\udcff.o", True),
            True,
            id="a-path-to-an-object-file",
        ),
        pytest.param(
            ("gen?/",),
            (),
            "should_skip_rel_path",
            ("gen\udcff", False),
            True,
            id="a-path-to-an-excluded-dir",
        ),
        pytest.param(
            ("*.o",),
            (),
            "should_skip_rel_path",
            ("ok\udcff.c", True),
            False,
            id="a-path-no-rule-matches",
        ),
        pytest.param(
            (),
            ("jq\udcff.min.js",),
            "unignore_names_this_file",
            ("a/jq\udcff.min.js",),
            True,
            id="an-unignore-naming-the-file",
        ),
        pytest.param(
            (),
            ("x\udcff/node_modules/*.js",),
            "unignore_could_match_within",
            ("x\udcff",),
            True,
            id="an-unignore-below-the-dir",
        ),
        pytest.param(
            (),
            ("x\udcff/node_modules/*.js",),
            "unignore_could_match_within",
            ("x\udcfe",),
            False,
            id="an-unignore-below-another-dir",
        ),
        pytest.param(
            (),
            ("?\udcff/.git/config",),
            "unignore_names_vcs_dir",
            ("x\udcff/.git",),
            True,
            id="fnmatch-question-mark-takes-one-surrogate",
        ),
        pytest.param(
            (),
            ("?\udcff/.git/config",),
            "unignore_names_vcs_dir",
            ("x\udcff\udcfe/.git",),
            False,
            id="fnmatch-question-mark-takes-no-more",
        ),
        pytest.param(
            (),
            ("x[\udc80-\udcff]/.git/config",),
            "unignore_names_vcs_dir",
            ("x\udcff/.git",),
            True,
            id="an-fnmatch-set-of-surrogates",
        ),
    ],
)
def test_names_with_undecodable_bytes_are_judged_as_python_judges_them(
    tmp_path: Path,
    exclude: tuple[str, ...],
    unignore: tuple[str, ...],
    method: str,
    args: tuple[object, ...],
    expected: bool,
) -> None:
    python = _python_answer(
        method, args, frozenset(exclude), frozenset(unignore), tmp_path
    )
    rust = _rust(lambda: getattr(_rules(exclude, unignore), method)(*args))
    assert (python, rust) == (expected, expected)
