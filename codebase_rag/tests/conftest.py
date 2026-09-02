from __future__ import annotations

import builtins
import functools
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self
from unittest.mock import MagicMock, call

import pytest
from loguru import logger

from codebase_rag import constants as rag_cs
from codebase_rag import graph_audit
from codebase_rag.capture import CaptureSelection
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import LANGUAGE_SPECS, get_language_for_extension
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord

if TYPE_CHECKING:
    pass  # ty: ignore[unresolved-import]

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# pytester drives the cgr-trace pytest plugin end-to-end in its own sessions.
pytest_plugins = ["pytester"]


class NodeProtocol(Protocol):
    @property
    def type(self) -> str: ...
    @property
    def children(self) -> list[Self]: ...
    @property
    def parent(self) -> Self | None: ...
    @property
    def text(self) -> bytes: ...
    def child_by_field_name(self, name: str) -> Self | None: ...


@dataclass
class MockNode:
    node_type: str
    node_children: list[MockNode] = field(default_factory=list)
    node_parent: MockNode | None = None
    node_fields: dict[str, MockNode | None] = field(default_factory=dict)
    node_text: bytes = b""

    @property
    def type(self) -> str:
        return self.node_type

    @property
    def id(self) -> int:
        # Real tree-sitter nodes expose a per-parse identity; object identity
        # is the mock's equivalent.
        return builtins.id(self)

    @property
    def children(self) -> list[MockNode]:
        return self.node_children

    @property
    def parent(self) -> MockNode | None:
        return self.node_parent

    @parent.setter
    def parent(self, value: MockNode | None) -> None:
        self.node_parent = value

    @property
    def text(self) -> bytes:
        return self.node_text

    def child_by_field_name(self, name: str) -> MockNode | None:
        return self.node_fields.get(name)


def create_mock_node(
    node_type: str,
    text: str = "",
    fields: dict[str, MockNode | None] | None = None,
    children: list[MockNode] | None = None,
    parent: MockNode | None = None,
) -> MockNode:
    node = MockNode(
        node_type=node_type,
        node_children=children or [],
        node_parent=parent,
        node_fields=fields or {},
        node_text=text.encode(),
    )
    for child in node.node_children:
        child.node_parent = node
    return node


logger.remove()

# Every per-file pass swallows all exceptions so one bad file cannot abandon a
# whole index, and only logs. Nothing looked at those logs, so a bug inside a
# pass dropped the file's edges while the run reported success, and the suite
# saw it at best as unrelated assertion failures elsewhere (issue #1070). One
# entry per pass, and the import pass logs at WARNING rather than ERROR.
_PASS_FAILURE_PREFIXES = (
    "Failed to process calls in ",
    "Failed to parse or ingest ",
    "Failed to parse imports in ",
    "Error parsing ",
)


@pytest.fixture(autouse=True)
def _fail_on_swallowed_pass_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[list[str], None, None]:
    """Fail any test whose indexing run swallowed a pass failure (issue #1070).

    Armed around `GraphUpdater.run` rather than around the whole test, and
    autouse rather than folded into the updater helpers. Most test files build
    an updater and call `run()` themselves, so a gate the next test bypasses by
    not calling a helper is no gate; and scoping it to the run keeps it off the
    unit tests that call a parser directly to exercise its own error path.

    A test that means to index a broken file requests this fixture, asserts on
    the list and clears it.
    """
    failures: list[str] = []
    original_run = GraphUpdater.run

    def run_watching_for_pass_failures(self: GraphUpdater, force: bool = False) -> None:
        sink_id = logger.add(
            lambda message: failures.append(message.record["message"]),
            level="WARNING",
            filter=lambda record: record["message"].startswith(_PASS_FAILURE_PREFIXES),
        )
        try:
            original_run(self, force)
        finally:
            logger.remove(sink_id)

    monkeypatch.setattr(GraphUpdater, "run", run_watching_for_pass_failures)
    yield failures
    assert_no_pass_failures(failures)


def assert_no_pass_failures(failures: list[str]) -> None:
    assert not failures, "a per-file pass failed:\n" + "\n".join(failures)


@pytest.fixture(autouse=True)
def _disable_stack_autostart() -> Generator[None, None, None]:
    from unittest.mock import patch

    with patch("codebase_rag.cli._maybe_start_stack"):
        yield


@functools.cache
def _unavailable_grammars() -> frozenset[rag_cs.SupportedLanguage]:
    # Probed once per process: `lang in parsers` makes the lazy store attempt
    # the load, so this is the authoritative "installed or not" answer.
    parsers, _queries = load_parsers()
    return frozenset(lang for lang in LANGUAGE_SPECS if lang not in parsers)


def _grammars_missing_for(updater: GraphUpdater) -> frozenset[rag_cs.SupportedLanguage]:
    unavailable = _unavailable_grammars()
    if not unavailable:
        return unavailable
    # Same discovery-before-walk ordering as run() itself: generated-source
    # roots are carved out of the build-dir prune only once registered, and
    # the registration is recomputed per run, so doing it here is idempotent.
    updater._register_generated_sources()
    # The updater's own eligibility walk, not a raw rglob: a file the run
    # would ignore anyway (node_modules, exclusions, hidden dirs) must not
    # gate the test on its language's grammar.
    return frozenset(
        language
        for path, _rel_path in updater._collect_eligible_files()
        if (language := get_language_for_extension(path.suffix)) is not None
        and language in unavailable
    )


@pytest.fixture(autouse=True)
def _skip_when_grammar_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip, not fail, when the test repo needs an uninstalled grammar (#1371).

    A base install (no `treesitter-full` extra) ships only the Python grammar.
    The updater silently ignores files whose parser is missing, so every
    grammar-dependent test failed downstream on an empty graph instead of
    skipping. Wrapped around `GraphUpdater.run` like the pass-failure gate
    above: on a full install `_unavailable_grammars()` is empty and this is a
    no-op, so CI behavior is unchanged; on a base install any run over a repo
    that contains files of a missing language becomes a skip with the same
    reason `create_and_run_updater`'s explicit `skip_if_missing` uses.
    """
    original_run = GraphUpdater.run

    def run_or_skip_missing_grammars(self: GraphUpdater, force: bool = False) -> None:
        missing = _grammars_missing_for(self)
        if missing:
            names = ", ".join(sorted(str(lang.value) for lang in missing))
            pytest.skip(f"{names} parser not available")
        original_run(self, force)

    monkeypatch.setattr(GraphUpdater, "run", run_or_skip_missing_grammars)


@pytest.fixture(autouse=True)
def _skip_on_missing_grammar_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn a lookup of an uninstalled grammar into a skip (#1371).

    Tests that grab a parser directly (`parsers[lang]`, `queries[lang]`, or
    `.get(lang)` followed by use) fail with KeyError or AttributeError on a
    base install. The lookup itself is the exact moment the missing optional
    dependency is discovered, so it becomes the skip point. Only languages the
    store genuinely cannot load are affected; on a full install the wrapper
    re-raises and nothing changes.
    """
    from codebase_rag import parser_loader

    # Primed before the patch goes on: the availability probe itself walks the
    # views with `in`, whose Mapping default routes through __getitem__, so
    # probing from inside the wrapper would recurse forever.
    unavailable = _unavailable_grammars()
    original_getitem = parser_loader._LazyLanguageView.__getitem__

    def getitem_or_skip(
        self: parser_loader._LazyLanguageView, lang_name: rag_cs.SupportedLanguage
    ) -> object:
        try:
            return original_getitem(self, lang_name)
        except KeyError:
            if lang_name in LANGUAGE_SPECS and lang_name in unavailable:
                pytest.skip(f"{lang_name} parser not available")
            raise

    monkeypatch.setattr(parser_loader._LazyLanguageView, "__getitem__", getitem_or_skip)


@pytest.fixture(autouse=True)
def _pin_csharp_frontend_treesitter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped default is AUTO (hybrid wherever dotnet exists), which would
    # run a real MSBuild workspace load in any unit test whose fixture carries
    # a .csproj. Tests pin pure tree-sitter and opt into Roslyn explicitly.
    from codebase_rag import constants as cs
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)


@pytest.fixture(autouse=True)
def _isolate_vector_store(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tests must never touch the developer's real vector store: a clean-path
    # test would purge it for real, and parallel xdist workers would collide
    # on its file lock. A developer .env sets QDRANT_URL to the live daemon
    # stack, and URL mode never reads QDRANT_DB_PATH, so the URL must be
    # cleared too or the purge lands on the live server.
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "QDRANT_URL", None)
    monkeypatch.setattr(
        settings, "QDRANT_DB_PATH", str(tmp_path_factory.mktemp("qdrant-iso"))
    )
    monkeypatch.setattr(
        settings,
        "MILVUS_URI",
        str(tmp_path_factory.mktemp("milvus-iso") / "milvus.db"),
    )


@pytest.fixture(autouse=True)
def _isolate_cgr_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    from codebase_rag.config import settings

    home = tmp_path_factory.mktemp("cgr-home-iso")
    monkeypatch.setattr(settings, "CGR_HOME", home)
    yield home


# What `shutil.rmtree` passes to `onexc` besides the removals. None of these
# can be re-called with a single path argument: `os.open` needs `flags`,
# `os.close` needs a descriptor, and `os.path.islink`/`os.scandir`/`os.lstat`
# are queries that remove nothing (#1622).
_NON_REMOVAL_FUNCS = frozenset(
    {os.scandir, os.open, os.lstat, os.close, os.path.islink}
)


def _clear_readonly(
    func: Any, path: Any, _exc: BaseException, _attempted: set[str] | None = None
) -> None:
    """Retry a failed removal after clearing the read-only bit.

    On Windows `os.unlink` refuses a read-only file outright, so a tree
    containing one cannot be removed; POSIX only needs write permission on
    the containing DIRECTORY, which is why this never fires there. Git sets
    that bit on every loose object it writes, so any fixture that runs
    `git init` inside the temp repo leaves `.git/objects/**` unremovable
    (issue #1586).

    The mode is ADDED to the existing bits rather than replacing them.
    `chmod(path, S_IWRITE)` sets the mode to exactly `0o200`, which on a
    DIRECTORY strips the search bit and makes it permanently untraversable --
    so a removal that failed for any other reason leaves behind a tree nobody
    can delete, turning a recoverable error into a permanent one. That is the
    opposite of this handler's purpose, and it bites on POSIX, where the
    read-only case it exists for cannot even occur.

    A path that VANISHED between the walk and the removal has already reached
    the state the teardown wanted, so it is not an error to recover from. Git
    runs background maintenance after `git commit` -- `git commit` spawns
    `git maintenance run --auto --quiet --detach`, `git init` spawns nothing
    (verified under `GIT_TRACE=1`, git 2.47.1) -- and that deletes its own
    `.git/objects/maintenance.lock` asynchronously, so `rmtree` can list the
    entry and find it gone by the time it unlinks -- a TOCTOU that surfaced
    only under `pytest-xdist` on a CI runner, never on a developer machine.
    """
    # A symlink has no mode of its own worth clearing, and every stat/chmod
    # here FOLLOWS it: on a DANGLING link `os.stat` raises FileNotFoundError
    # and the early return leaves the link in place, abandoning a removal the
    # handler was asked to perform. On POSIX that costs nothing end-to-end
    # (rmtree unlinks a dangling link unaided in a writable parent, and where
    # it cannot the blocker is the parent's mode, not the link); the rescued
    # platform is WINDOWS, where `os.unlink` refuses the link outright and the
    # handler is the only removal path, so rmtree then fails on the parent.
    # See the SCOPE block in test_temp_repo_readonly_cleanup.py. `os.lstat`
    # alone does not fix it -- `os.chmod` follows links too, and
    # `follow_symlinks=False` is unsupported for chmod on LINUX, one of the
    # three unit-matrix platforms and the one this handler must survive.
    # CPython is built without `lchmod` there (configure.ac forces it off for
    # every Linux build: "Linux disallows changing the mode of symbolic links.
    # Some libc implementations have a stub lchmod implementation that always
    # returns an error." -- the kernel restriction is the reason; the stub is a
    # secondary note and names no libc), and `os.py` gates chmod's entry into
    # `os.supports_follow_symlinks` on HAVE_LCHMOD alone -- the HAVE_FCHMODAT
    # line is deliberately commented out -- so the capability is absent from
    # the support set -- advisory metadata about the build, not enforcement.
    # Absent from the SET is not refused at the CALL: HAVE_FCHMODAT *is*
    # defined on Linux, so posixmodule.c (v3.12.3) compiles out its early
    # `follow_symlinks_specified` guard at :3348, the call reaches
    # `fchmodat(AT_SYMLINK_NOFOLLOW)` at :3400, and NotImplementedError comes
    # only from ENOTSUP/EOPNOTSUPP there (:3408) -- i.e. on a link. The refusal
    # is FILE-TYPE dependent, not platform dependent. Measured on Ubuntu/glibc
    # 2.39, x86_64, CPython 3.12.3 and 3.12.13 (musl not measured): HAVE_LCHMOD
    # 0, `os.chmod not in os.supports_follow_symlinks`, `follow_symlinks=False`
    # SUCCEEDS on a regular file and raises NotImplementedError("chmod:
    # follow_symlinks unavailable on this platform") on a dangling link. macOS
    # 3.12.7 is the mirror image: chmod IS in the set and both cases succeed.
    #
    # The set IS readable at runtime, so the skip is a deliberate choice rather
    # than a workaround for an undetectable gap -- but branching on it would be
    # branching on the wrong thing, since it does not predict the call.
    # It also matters that NotImplementedError is a RuntimeError, NOT an
    # OSError -- the `except FileNotFoundError` below would not catch it, so
    # taking the chmod route would raise straight out of teardown on the Linux
    # CI runners (the unit matrix also runs Windows and macOS, where it would
    # not). Skipping the mode work is the portable answer.
    # On Windows `os.path.islink` is true for both symlink kinds, so this keeps
    # the handler platform-independent rather than trading one for another.
    # Scoped to one top-level `rmtree`, not module-level: a module-level set
    # would never be cleared and would suppress a legitimate second attempt on
    # the same path in a later test. `functools.partial` carries it into the
    # recursive call below.
    if _attempted is None:
        _attempted = set()
    path_key = os.fspath(path)

    if os.path.islink(path):
        # Same vanished-path tolerance as the non-link path below: the link
        # can be gone by the time this retry runs, and that is the state the
        # teardown wanted. Without this the link branch would raise out of
        # teardown for a case every other branch tolerates.
        try:
            func(path)
        except FileNotFoundError:
            pass
        return
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return
    except OSError:
        mode = 0
    try:
        # S_IREAD as well as S_IEXEC on a directory: EXECUTE makes it
        # traversable, READ makes it listable, and a removal needs both.
        # Without READ a 0o000 directory becomes 0o300, which `rmtree` can
        # enter but not enumerate, so the retry fails on the same directory
        # this handler just "fixed" (#1622). Pytest's own `chmod_rw` adds
        # S_IRUSR|S_IWUSR and no S_IXUSR, so it cannot repair that either.
        extra = stat.S_IREAD | stat.S_IEXEC if os.path.isdir(path) else 0
        os.chmod(path, mode | stat.S_IWRITE | extra)
        # `func` is not always a one-argument removal, and the cases need
        # different handling (#1622).
        #
        # The discriminator names the callables that must NOT be re-called
        # with a bare path, and retries everything else. `rmtree` passes
        # `os.scandir`, `os.open`, `os.lstat`, `os.close` and
        # `os.path.islink` besides the removals, and each is wrong to call
        # differently: `os.open` wants `flags` and `os.close` wants a
        # descriptor (both raise TypeError out of teardown), while
        # `os.path.islink` is a pure query that removes nothing while looking
        # like a successful retry.
        #
        # Named exclusions rather than an `(os.unlink, os.rmdir)` whitelist
        # because callers legitimately pass their own removal callable -- the
        # handler's own tests pass `retried.append` and bare lambdas, and a
        # whitelist silently skips the retry for every one of them, which is
        # the same "looks like a retry, removes nothing" failure this branch
        # exists to stop.
        if func not in _NON_REMOVAL_FUNCS:
            func(path)
        elif os.path.isdir(path) and path_key not in _attempted:
            # A LISTING failed. `rmtree` does NOT retry this directory after
            # the handler returns -- it abandons the subtree, so the parent's
            # `rmdir` then fails "Directory not empty" and nothing revisits
            # the children. Recursing is what removes them.
            #
            # Guarded by `_attempted`: the recursive call re-enters this
            # handler for the SAME path whenever a child cannot be removed
            # (an immutable file, a read-only mount), and without the guard
            # that is unbounded recursion ending in `RecursionError` raised
            # from inside teardown -- strictly harder to attribute than the
            # underlying `PermissionError`. Seen once, the path is left to
            # report its own error.
            _attempted.add(path_key)
            shutil.rmtree(
                path,
                onexc=functools.partial(_clear_readonly, _attempted=_attempted),
            )
    except FileNotFoundError:
        return


def _make_tmp_path_removable(root: Path) -> None:
    """Add the owner write bit to everything under `root`, bottom-up.

    `_clear_readonly` rescues fixtures that tear themselves down through it.
    Nothing rescues a test that runs `git init` under a bare `tmp_path` and
    leaves the tree to pytest's own retention cleanup, which is what the
    sweep on issue #1622 found in five places. Git writes loose objects
    read-only, so on Windows that tree cannot be removed; pytest keeps the
    last few basetemps and collects them in a LATER session with errors
    ignored, so the failure never lands on the test that created it.

    This runs for every test, so it must be cheap and total: it walks only
    the test's own `tmp_path` (usually absent or tiny) and never raises.

    Three constraints inherited from `_clear_readonly`, each load-bearing:

    * The bit is ADDED to the existing mode, never assigned. `chmod(p, 0o200)`
      sets a DIRECTORY untraversable for good, turning a recoverable state
      into a permanent one.
    * Symlinks are skipped. `os.chmod` follows links and `follow_symlinks`
      is unsupported for chmod on Linux, one of the three matrix platforms.
    * A path that vanishes mid-walk is already in the state teardown wanted;
      git's background maintenance deletes its own lock asynchronously.
    """
    if not root.exists():
        return

    def widen(target: str) -> None:
        if os.path.islink(target):
            return
        try:
            mode = os.stat(target).st_mode
            # A directory needs READ to be listed as well as EXECUTE to be
            # entered: at 0o300 it is traversable but `os.walk` still cannot
            # enumerate it, so its children stay unreachable and unremovable.
            extra = stat.S_IREAD | stat.S_IEXEC if os.path.isdir(target) else 0
            os.chmod(target, mode | stat.S_IWRITE | extra)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError:
            # A mode we cannot widen is not worth failing teardown over: the
            # removal that follows reports the real problem.
            return

    # The root FIRST: every walk below has to list it, so a restrictive mode
    # on `tmp_path` itself blocks all of them and the retry re-walks a root
    # that is still unreadable. Fixing it last cannot help the passes that
    # already failed.
    widen(str(root))
    # Then top-down, widening each directory as it ARRIVES rather than its
    # children afterwards. `os.walk` is lazy and lists a directory when it
    # yields it, so fixing a mode on arrival fixes it before the listing for
    # the level below. Bottom-up would fix only the ORDER of widening, never
    # the reachability the listing itself needs.
    #
    # `dirnames` as well as `dirpath`, because a directory at mode 0o000 is
    # never YIELDED at all -- `os.walk` raises while listing it and skips it
    # silently -- so its name in its PARENT's listing is the only handle on
    # it. `onerror` collects what would otherwise be swallowed, turning a
    # silently empty subtree into a signal.
    walk_errors: list[OSError] = []
    for dirpath, dirnames, _filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=walk_errors.append
    ):
        widen(dirpath)
        for name in dirnames:
            widen(os.path.join(dirpath, name))
    if walk_errors:
        # A listing failed before its mode was fixed; the modes are fixed now,
        # so a second walk reaches what the first could not. Bounded at one
        # retry: anything still unlistable is a genuine failure, and the
        # removal that follows reports it rather than this helper looping.
        for dirpath, dirnames, _filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            widen(dirpath)
            for name in dirnames:
                widen(os.path.join(dirpath, name))
    # Finally the files, bottom-up, now that every directory holding one can
    # actually be listed.
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in (*filenames, *dirnames):
            widen(os.path.join(dirpath, name))


@pytest.fixture(autouse=True)
def _tmp_path_stays_removable(request: pytest.FixtureRequest) -> Generator[None]:
    """Leave no read-only object behind for pytest's cleanup to trip over.

    Autouse and keyed on whether the test actually asked for `tmp_path`, so a
    test that never used one does no work. This is the DEFAULT the issue asks
    for: `git_repo` covers the fixtures that opt in, and this covers the ones
    that build a repository by hand, including ones not yet written.
    """
    # Resolved BEFORE the yield: after the test, `tmp_path` may already be
    # torn down and `getfixturevalue` then raises rather than returning it.
    # Requesting it here does not create one for a test that never asked --
    # the membership check gates that -- so an unrelated test still does no
    # filesystem work.
    root = (
        request.getfixturevalue("tmp_path")
        if "tmp_path" in request.fixturenames
        else None
    )
    yield
    if root is not None:
        _make_tmp_path_removable(root)


@pytest.fixture
def git_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """A real git repository under `tmp_path`, torn down safely.

    Prefer this over calling `git init` by hand: teardown goes through
    `_clear_readonly`, so the read-only loose objects git writes cannot
    strand the tree on Windows (issue #1622).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig-absent"),
        "GIT_CONFIG_SYSTEM": str(tmp_path / "gitconfig-absent"),
        # Pinned for the same reason the config files are neutralised: the
        # env is inherited, and an ambient GIT_DEFAULT_HASH=sha256 would give
        # 62-hex loose-object basenames instead of 38-hex, changing what
        # callers see.
        "GIT_DEFAULT_HASH": "sha1",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    yield repo
    shutil.rmtree(repo, onexc=_clear_readonly, ignore_errors=False)


@pytest.fixture
def temp_repo() -> Generator[Path, None, None]:
    """Creates a temporary repository path for a test and cleans up afterward."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    # `onexc` rather than a bare rmtree: see `_clear_readonly`. Teardown
    # failures surface as an ERROR on a test that already passed, which is
    # why this is worth handling here rather than per fixture -- `temp_repo`
    # is used by 327 test modules and any of them may write a read-only file.
    shutil.rmtree(temp_dir, onexc=_clear_readonly)


class _MockIngestor:
    _TRACKED = (
        "fetch_all",
        "execute_write",
        "ensure_node_batch",
        "ensure_relationship_batch",
        "flush_all",
    )

    def __init__(self) -> None:
        self.fetch_all = MagicMock()
        self.execute_write = MagicMock()
        self.ensure_node_batch = MagicMock()
        self.ensure_relationship_batch = MagicMock()
        self.flush_all = MagicMock()
        self._fallback = MagicMock()

    def reset_mock(self) -> None:
        for name in (*self._TRACKED, "_fallback"):
            getattr(self, name).reset_mock()

    @property
    def method_calls(self) -> list:
        result = []
        for name in self._TRACKED:
            mock_attr = self.__dict__[name]
            for c in mock_attr.call_args_list:
                result.append(getattr(call, name)(*c.args, **c.kwargs))
        result.extend(self._fallback.method_calls)
        return result

    def __getattr__(self, name: str) -> MagicMock:
        return getattr(self._fallback, name)


@pytest.fixture
def mock_ingestor() -> _MockIngestor:
    return _MockIngestor()


def run_updater(
    repo_path: Path, mock_ingestor: MagicMock, skip_if_missing: str | None = None
) -> None:
    create_and_run_updater(repo_path, mock_ingestor, skip_if_missing)


def create_and_run_updater(
    repo_path: Path,
    mock_ingestor: MagicMock,
    skip_if_missing: str | None = None,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
    capture: CaptureSelection | None = None,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if skip_if_missing and skip_if_missing not in parsers:
        pytest.skip(f"{skip_if_missing} parser not available")
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
        exclude_paths=exclude_paths,
        unignore_paths=unignore_paths,
        capture=capture,
    )
    updater.run()
    _audit_recorded_graph(mock_ingestor)
    return updater


def _audit_recorded_graph(mock_ingestor: MagicMock) -> None:
    """Structural integrity audit of the recorded batches (issue #646).

    Every test that indexes a fixture also asserts the resulting graph is
    schema-conformant, orphan-free, and free of dangling relationships (issue
    #652: an edge with a phantom endpoint is silently dropped by the
    database). CGR_AUDIT_SWEEP=<file> switches to collect mode, appending
    violations as JSON lines instead of failing.
    """
    nodes = [
        GraphNodeRecord(str(c.args[0]), c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
    ]
    rels = [
        GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
    ]
    violations = graph_audit.collect_violations(nodes, rels)
    if sweep_path := os.environ.get("CGR_AUDIT_SWEEP"):
        import json

        test_id = os.environ.get("PYTEST_CURRENT_TEST", "")
        with open(sweep_path, "a") as f:
            for v in violations:
                f.write(json.dumps([str(v.check), v.detail, test_id]) + "\n")
        return
    assert not violations, "\n".join(v.detail for v in violations)


def get_relationships(mock_ingestor: MagicMock, rel_type: str) -> list:
    """Extract relationships of a specific type from mock_ingestor calls."""
    return [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == rel_type
    ]


def get_nodes(mock_ingestor: MagicMock, node_type: str) -> list:
    """Extract nodes of a specific type from mock_ingestor calls."""
    return [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == node_type
    ]


def get_qualified_names(calls: list) -> set[str]:
    """Extract qualified names from a list of node calls."""
    return {call[0][1]["qualified_name"] for call in calls}


def get_node_names(mock_ingestor: MagicMock, node_type: str) -> set[str]:
    """Get qualified names of all nodes of a specific type."""
    return get_qualified_names(get_nodes(mock_ingestor, node_type))


@pytest.fixture
def mock_updater(temp_repo: Path, mock_ingestor: MagicMock) -> MagicMock:
    """Provides a mocked GraphUpdater instance with necessary dependencies."""
    parsers, queries = load_parsers()
    mock = MagicMock(spec=GraphUpdater)
    mock.repo_path = temp_repo
    mock.ingestor = mock_ingestor
    mock.parsers = parsers
    mock.queries = queries
    mock.project_name = temp_repo.resolve().name

    mock.factory = MagicMock()
    mock.factory.definition_processor = MagicMock()
    mock.factory.structure_processor = MagicMock()
    mock.factory.structure_processor.structural_elements = {}

    mock_root_node = MagicMock()
    mock.factory.definition_processor.process_file.return_value = (
        mock_root_node,
        "python",
    )

    mock.ast_cache = {}

    return mock


@pytest.fixture(scope="session", autouse=True)
def cleanup_qdrant_client() -> Generator[None, None, None]:
    yield

    try:
        import codebase_rag.vector_store as vs

        vs.close_vector_store_client()
    except Exception:
        pass
