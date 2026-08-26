# Gate against locale-dependent subprocess decoding (issue #1454).
#
# `subprocess.run(..., text=True)` with no `encoding=` decodes the child's
# output using the LOCALE encoding: UTF-8 on the Linux and macOS runners,
# cp1252 on the Windows ones. Every tool cgr shells out to writes UTF-8, so on
# Windows any non-ASCII identifier, path or message comes back mangled.
#
# The failure surfaces far from its cause. It is not a decode error but a
# lookup miss for a name that should have been present -- in #1450 it appeared
# as KeyError: 'Café' from a Ruby oracle that had parsed the file correctly.
#
# A grep-based gate rather than a lint rule because it needs no new tooling and
# runs in the existing suite. It parses rather than greps, so a call spanning
# several lines is still seen.
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = ("codebase_rag", "evals")

# Vendored third-party sources downloaded as eval corpora. Their code is not
# ours to edit, and cgr never executes it -- it is input to the indexer.
_VENDORED = ("evals/results/corpora/",)

# Call sites that legitimately decode as something other than UTF-8 must be
# listed here with the reason, so an exemption is a decision rather than an
# omission. Empty today, and it should stay hard to add to: a too-broad
# exemption makes this gate quiet, and quiet is indistinguishable from clean.
# Every entry needs a comment saying why that path may decode by locale.
_EXEMPT: frozenset[str] = frozenset()


def _subprocess_calls_missing_encoding(tree: ast.AST) -> list[int]:
    """Line numbers of `subprocess.run(...)`-style calls that decode by locale.

    Only calls that actually decode are reported: `text=True` (or its aliases
    `universal_newlines=True`) with no `encoding=`. A bytes-mode call has no
    encoding to get wrong.

    Two deliberate limits, both measured rather than assumed:

    - Matching is scoped to calls whose callee NAME spawns a process (`run`,
      `Popen`, `check_output`, `create_subprocess_exec`, ...), matched on the
      final attribute so `subprocess.run(...)` and a directly-imported
      `run(...)` both qualify. Scoping by NAME rather than by module is what
      keeps `from subprocess import run` in view -- naming the module would
      miss it, which is how #1454 reached production.

      Measured before adopting: across the tree, ZERO `text=True` calls are
      on non-spawning callees, and ZERO spawning calls would drop out of
      view. So the scope costs nothing today in either direction. Re-measure
      before assuming that holds -- if a non-subprocess API grows a
      `text=`/`errors=` keyword, the false positive is the cheap direction and
      a missed subprocess call is the expensive one.
    - Only a literal `True` counts as decoding; `text=some_flag` is not
      chased, since that would need dataflow in a lint gate. Measured across
      the repo: 15 calls pass a non-literal `text=`, and **none** is a
      subprocess call — they are Rich `Text`, ast-grep matches and web-search
      page bodies. So the hole costs nothing today. Re-measure before assuming
      it still costs nothing.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        values = {kw.arg: kw.value for kw in node.keywords if kw.arg}

        # An `encoding` whose VALUE is None decodes by locale exactly as
        # omitting it does, so this tests the value rather than the presence
        # of the keyword. Keying on presence let `encoding=None` silence the
        # gate while changing no behaviour -- presence-vs-meaning, sitting
        # inside the guard against it.
        encoding = values.get("encoding")
        if encoding is not None and not _is_literal_none(encoding):
            continue

        # `errors=` puts a CHILD PROCESS in text mode on its own, with no
        # `text=` anywhere. Keying only on text/universal_newlines misses that
        # spelling.
        #
        # Unlike the checks below, this one is restricted to calls that
        # plausibly spawn a process. `bytes.decode(enc, errors="replace")` is
        # extremely common here and passes its encoding POSITIONALLY, so an
        # unrestricted `errors=` rule reported 7 offenders, every one a false
        # positive on a decode call that was already explicit. Over-reporting
        # is cheap for the text= rule because `text=True` on a non-subprocess
        # call is rare; it is not cheap here.
        if (
            _looks_like_process_spawn(node)
            and "errors" in values
            and not _is_literal_none(values["errors"])
        ):
            found.append(node.lineno)
            continue

        decodes = any(
            kw.arg in {"text", "universal_newlines"}
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        )
        if decodes and _looks_like_process_spawn(node):
            found.append(node.lineno)
    return found


def _is_literal_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


# Names that spawn a child process. Used ONLY to scope the `errors=` rule,
# where the text= rule's over-report-is-cheap reasoning does not hold.
_SPAWNING_NAMES = frozenset(
    {
        "run",
        "Popen",
        "check_output",
        "check_call",
        "call",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "getoutput",
        "getstatusoutput",
    }
)


def _looks_like_process_spawn(node: ast.Call) -> bool:
    """Whether this call plausibly starts a child process.

    Matches on the final attribute or bare name, so `subprocess.run(...)`,
    `run(...)` imported directly, and `asyncio.create_subprocess_exec(...)`
    all qualify while `raw.decode(...)` does not.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _SPAWNING_NAMES
    if isinstance(func, ast.Name):
        return func.id in _SPAWNING_NAMES
    return False


def _scan() -> tuple[list[str], list[str], list[str]]:
    """Offenders, files successfully parsed, and files that could not be.

    Parsed and unreadable are returned separately because they answer
    different questions: a clean run means "these N files were inspected and
    none offend", which is only meaningful if N is large AND the unreadable
    list is empty. Collapsing them lets a tree that fails to parse read as a
    tree with no offenders.
    """
    offenders: list[str] = []
    parsed: list[str] = []
    unreadable: list[str] = []
    for package in _SCANNED:
        for path in sorted((_REPO_ROOT / package).rglob("*.py")):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel in _EXEMPT or rel.startswith(_VENDORED):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                unreadable.append(rel)
                continue
            parsed.append(rel)
            offenders.extend(
                f"{rel}:{line}" for line in _subprocess_calls_missing_encoding(tree)
            )
    return offenders, parsed, unreadable


def test_no_subprocess_call_decodes_with_the_locale_encoding() -> None:
    """An explicit encoding at every decoding call site.

    Without this gate the next `subprocess.run` written re-introduces the bug,
    which is exactly what happened between the oracles being written and #1454.

    A file that cannot be parsed fails THIS gate, not merely a sibling test.
    An earlier version skipped unparseable files here and reported them from a
    separate test, which left the gate itself green on a tree containing a
    real violation: Greptile demonstrated it with a syntactically invalid file
    holding `subprocess.run(..., text=True)`. The offending call was concealed
    by the syntax error, and the check that mattered still passed.

    "Could not check" must fail the check that does the checking. Reporting it
    elsewhere makes the failure visible to someone reading the whole suite,
    which is not the same as making it block.
    """
    offenders, _parsed, unreadable = _scan()

    assert not unreadable, (
        f"{len(unreadable)} file(s) could not be parsed, so this gate did not "
        "inspect them and cannot claim they are clean:\n" + "\n".join(unreadable[:20])
    )

    assert not offenders, (
        f"{len(offenders)} call(s) decode subprocess output with the locale "
        "encoding; pass encoding=cs.ENCODING_UTF8 (issue #1454):\n"
        + "\n".join(offenders[:40])
    )


def test_the_scan_actually_reaches_the_source_tree() -> None:
    """A control: the gate above passes vacuously if it parses nothing.

    An empty file list and a clean tree are indistinguishable in the gate's
    assertion, which is the failure this repo has been bitten by repeatedly.

    Counts files the gate actually PARSED, not paths `rglob` discovered. An
    earlier version counted discovery, which could not tell a scanned tree
    from one where every single file raised on `ast.parse` and was skipped --
    the control would still have seen a thousand paths and passed while the
    gate inspected nothing. Discovery is not inspection.
    """
    _offenders, parsed, _unreadable = _scan()

    assert len(parsed) > 100, f"only {len(parsed)} files parsed; wrong root?"


def test_every_discovered_file_parses() -> None:
    """No file may be silently skipped.

    The gate swallows OSError and SyntaxError so one unparseable file cannot
    fail the build for an unrelated reason. That is the right behaviour for
    the gate and the wrong behaviour to leave unmeasured: a file it cannot
    read is a file it cannot check, and the gate's silence would look
    identical to a clean result.

    Asserting emptiness here keeps the skip non-silent -- if a file ever
    becomes unreadable, this names it rather than the gate quietly shrinking.
    """
    _offenders, _parsed, unreadable = _scan()

    assert not unreadable, (
        f"{len(unreadable)} file(s) could not be parsed and were therefore "
        "never checked for locale decoding:\n" + "\n".join(unreadable[:20])
    )


def test_the_detector_recognises_a_bad_call() -> None:
    """A second control: the detector must fire on a known-positive.

    Proves the gate can fail, rather than being green because its matcher is
    broken. Both an aliased and a plain form are checked.
    """
    bad = ast.parse("subprocess.run(['x'], capture_output=True, text=True)")
    assert _subprocess_calls_missing_encoding(bad) == [1]

    aliased = ast.parse("subprocess.run(['x'], universal_newlines=True)")
    assert _subprocess_calls_missing_encoding(aliased) == [1]

    good = ast.parse("subprocess.run(['x'], text=True, encoding='utf-8')")
    assert _subprocess_calls_missing_encoding(good) == []

    bytes_mode = ast.parse("subprocess.run(['x'], capture_output=True)")
    assert _subprocess_calls_missing_encoding(bytes_mode) == []


def test_explicit_encoding_none_is_not_treated_as_safe() -> None:
    """`encoding=None` decodes by locale exactly as omitting it does.

    The detector skipped on the PRESENCE of an `encoding` keyword rather than
    its value, so writing `encoding=None` silenced the gate while changing
    nothing about the behaviour -- the presence-vs-meaning defect this repo
    keeps hitting, sitting inside the guard against it.
    """
    tree = ast.parse("subprocess.run(['x'], text=True, encoding=None)\n")

    assert _subprocess_calls_missing_encoding(tree) == [1]


def test_errors_kwarg_enables_text_mode_and_is_detected() -> None:
    """`errors=` puts the child in text mode without any `text=True`.

    A call with `errors="replace"` and no encoding decodes by locale, so the
    gate must see it. Keying only on `text`/`universal_newlines` misses an
    entire spelling of the same defect.
    """
    tree = ast.parse("subprocess.run(['x'], errors='replace')\n")

    assert _subprocess_calls_missing_encoding(tree) == [1]


def test_a_bytes_mode_call_is_still_ignored() -> None:
    """No text mode means no decoding, so nothing to get wrong.

    Paired with the two above so the widened detector cannot be implemented by
    flagging every call -- that would pass both new tests while burying the
    gate in false positives.
    """
    tree = ast.parse(
        "subprocess.run(['x'], capture_output=True)\n"
        "subprocess.run(['x'], text=True, encoding='utf-8')\n"
        "subprocess.run(['x'], errors=None)\n"
    )

    assert _subprocess_calls_missing_encoding(tree) == []


def test_a_directly_imported_run_is_still_detected() -> None:
    """`from subprocess import run` must stay in view.

    The scope keys on the callee NAME, not the module, precisely so this form
    is caught. Naming the module would miss it -- and a bare `run(...)` with
    `text=True` is exactly how #1454 reached production in the first place.

    This is the expensive direction: a false positive costs one `_EXEMPT`
    line, a false negative costs silent mojibake on Windows that surfaces as
    a wrong graph rather than an error.
    """
    tree = ast.parse("run(['x'], text=True)\n")

    assert _subprocess_calls_missing_encoding(tree) == [1]


def test_a_non_spawning_call_with_text_true_is_ignored() -> None:
    """An unrelated API taking `text=True` must not fail the gate.

    Measured as zero occurrences today, so this pins the scope rather than
    fixing an observed break -- but two independent reviewers raised it, and
    the guard costs nothing.
    """
    tree = ast.parse("render(template, text=True)\n")

    assert _subprocess_calls_missing_encoding(tree) == []
