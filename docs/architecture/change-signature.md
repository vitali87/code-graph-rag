---
description: "How cgr changes a Python definition's parameter list through the graph: the definition, its overrides and every known call site are rewritten in one transaction, and sites the mapping cannot complete are listed rather than guessed."
---

# Change Signature Operation

`change_signature` is the second graph-native edit operation (issue #1533).
Given a qualified name, the new parameter list and a mapping from new
parameters to old values, it rewrites the definition (and every override in
both directions) and every call site the graph knows, atomically, or
explains what it left alone and why.

```bash
# add a required parameter, passing 1 at every existing site
cgr change-signature myproj.pkg.util.helper a 'n: int' b --map n==1 --dry-run
# reorder
cgr change-signature myproj.pkg.util.helper b a
# rename by position: keyword callers follow the new names
cgr change-signature myproj.pkg.util.helper 'times: int' 'text: str' --map times=0 --map text=1
```

The MCP tool of the same name takes `qualified_name`, `new_params` (a list
of strings) and the optional `mapping` (an object), `allow_heuristic`,
`dry_run` and `project` fields, and returns the same report as JSON.

Python and JavaScript definitions are supported. A definition in another
language refuses, and the follow-up is issue #1908. A call site written in
another language than the definition stays unmapped.

### JavaScript

JavaScript has no keyword arguments, so every site is rewritten
positionally:

- A function declaration or a class method qualifies. Each parameter must
  be a name or a name with a default; a rest parameter (`...rest`) or a
  destructured one (`{ a }`) refuses, and so does a name declared twice. A
  constructor refuses, since its call sites are `new` expressions, and so
  does a getter or setter.
- A new parameter is written `n` or `n = 0`. A required parameter may
  follow a defaulted one, as JavaScript allows; a site that leaves it out
  stays unmapped. A mapped value must be exactly one expression, so
  `--map n==1, 2` refuses rather than writing two arguments.
- A default the site skips is passed as `void 0`, which selects the
  default as leaving the argument out does: `helper(2)` with a new `c`
  mapped after the defaulted `b` becomes `helper(2, void 0, 5)`. The name
  `undefined` is not used, since a caller can bind it to another value.
- A site stays unmapped when it spreads an array (`helper(...xs)`), passes
  more arguments than declared, calls through `.call` or `.apply`, or
  passes arguments that run code when the new order would run them in
  another order. A name, a number, a string, `true`, `false`, `null`,
  `undefined` and `this` run no code and may move.
- Renaming a parameter refuses for now. A body that reads `arguments`
  refuses, and so does dropping a parameter the body or another default
  still mentions, or adding one under a name it already mentions. Nested
  functions count, so a closure that binds the same name refuses too. A
  name spelled with escapes (`\u0061`) counts as the name it spells, and a
  parameter declared that way refuses. A body that uses `eval` refuses
  adding or dropping a parameter, since `eval('a')` reads `a` through a
  string; a reorder keeps every name and still goes through.
- A new header in which a default reads its own parameter or a later one
  refuses: parameters bind left to right, so that default would throw
  ReferenceError. Reordering `(a = 1, b = a)` to `(b = a, a = 1)` is one.
  So does a reorder that swaps a kept default that runs code with one that
  runs code or reads a name: `(a = f(), b = g())` to `(b = g(), a = f())`
  would call `g` first, and `(a = (n = 1), b = n)` to `(b = n, a = (n = 1))`
  would read the old `n`. A default counts as running code unless it is one
  of the values above that may move at a site.

## The new parameter list

Each entry of `new_params` is one parameter as it would be written in a
header: `n`, `n: int`, `n = 0` or `n: int = 0`. A bare name that matches an
old parameter carries that parameter over with its existing annotation and
default, so `["b", "a"]` on `def helper(a: int, b: str = 'x')` produces
`def helper(b: str = 'x', a: int)`, which is then refused because a required
parameter cannot follow a defaulted one. Spelling a kept parameter out
again re-annotates it: `["a", "b: int = 0"]` writes `b: int = 0` into the
header, and a literal mapped to it is checked against the new annotation.

The header must consist of plain positional-or-keyword parameters. A
definition with `*args`, `**kwargs`, a positional-only `/` or a keyword-only
`*` marker refuses: those change how call sites bind and the mapping below
cannot express them. A method's `self` or `cls` is kept in place and never
part of the list; a `@staticmethod` has no receiver and every parameter is
listed. A method whose first parameter is named anything else refuses,
since the receiver could not be told from the parameters and every bound
call would lose it.

Every override in the hierarchy (`OVERRIDES` edges in both directions) must
declare the same parameter names, or the operation refuses naming the
member that differs: rewriting one and not the other would break the
substitution the hierarchy exists for. Each override keeps its own
annotations and defaults, so every rewritten header is checked as Python
would check it when loading the module, and the operation refuses if one
fails. Moving `c` before `b` across `Base.f(self, a, b=1, c=2)` and
`Sub.f(self, a, b, c=2)` refuses, since `Sub.f` would read
`(self, a, c=2, b)`. So does a new parameter named like the receiver.

A new parameter, one no old parameter was called, also refuses when the
body of any member, or a scope nested in it, already reads that name: the
body would start reading the parameter instead. Adding `len=None` to
`def helper(a): return len(a)` refuses, while `dict(len=a)` in the body
does not count, since `len` there only labels the argument.

## The mapping

The mapping says, for each new parameter, where its value comes from at a
call site:

| Source        | Meaning                                                              |
|---------------|----------------------------------------------------------------------|
| `old_name`    | The value the site passed for that old parameter.                   |
| `0`, `1`, ... | The same, by the old parameter's position.                          |
| `=literal`    | That text, inserted at every site. The definition does NOT gain a default: the new parameter is as required or defaulted as its entry says. |

A new parameter absent from the mapping takes the old parameter of the same
name, if there is one. On the CLI the entries are `--map NEW=SOURCE`, so
`--map n==1` maps `n` to the literal `1`.

The mapping is refused, and nothing is written, when it names a parameter
that is not in the new list, feeds one from an old name or index that does
not exist, feeds two new parameters from the same old one, or gives a
literal that cannot be a value of the parameter's declared type: `=None`
fits `int | None` and `Optional[int]`, `='no'` does not fit `int`, and a
non-literal such as `=LIMIT` or an annotation the check does not read is
not checked.

## Renamed parameters

An old parameter fed to a new one of a different name (`times` from `a`)
is renamed, and the body follows: every bare reference to the old name in
the function's body becomes the new name, in the definition and in every
override. An attribute (`obj.a`), a keyword argument's name (`f(a=a)`
becomes `f(a=times)`), a class pattern's keyword (`case Point(a=a)` becomes
`case Point(a=times)`) and the attribute part of a dotted value pattern
(`case Color.a:`) share the spelling without being the parameter and are
left alone. A comprehension that reads the parameter follows the
rename too, since it binds only its own `for` targets.

The rename refuses, with nothing written, when the old name is used inside
a nested function, lambda or class, or re-bound by a `global`, `nonlocal`
or import statement, because the body walk cannot tell those uses from the
parameter's; and when the new name is already read in the body, or inside
a nested scope in it, because the parameter would shadow it. Swapping two
parameters (`b` from `a`, `a` from `b`) is a rename in each direction and
is allowed. Docstrings are prose and are not rewritten.

A call to the function inside its own body, or a call nested in another
call's arguments, is rewritten inside out: the inner rewrite (a renamed
argument, an inner site's new argument list) is folded into the enclosing
value before the enclosing site is rendered, so `helper(helper(1))` becomes
`helper(helper(1, 1), 1)` as one edit per site rather than two that overlap.

## What gets rewritten

| Site kind    | Source                                                                 |
|--------------|------------------------------------------------------------------------|
| `definition` | The `parameters` node of the definition and of every override, replaced with the new list. |
| `call`       | `CALLS` edges into the definition or an override, using the per-site `line`/`col`/`end_line`/`end_col` recorded at ingest (see [graph schema](graph-schema.md#edge-site-properties)); the argument list is re-rendered. |

At a call site the arguments are bound to the old parameters as Python
would bind them, then re-rendered for the new list. A leading run of
values stays positional; it ends at a defaulted parameter left out or at a
value that was a keyword. Every later value is spelled by keyword under its
new name, in the order the site wrote it, and a mapped literal outside the
run follows them. So with `helper(a, b='x')` becoming
`helper(a, b='x', n=0)` and `n` mapped to `=1`:

| Before               | After                     |
|----------------------|---------------------------|
| `helper(2)`          | `helper(2, n=1)`          |
| `helper(2, b='y')`   | `helper(2, b='y', n=1)`   |
| `helper(3, 'z')`     | `helper(3, 'z', 1)`       |

Python evaluates arguments left to right as written. A site whose values
are all names and plain literals (numbers, strings without interpolation,
`True`, `False`, `None`) runs no code while they are read, so reordering
`helper(a, b)` to `helper(b, a)` rewrites `helper(3, 'z')` positionally, as
`helper('z', 3)`. Once any value runs code (a call, an attribute, an
f-string), the site's values must come out in the order it wrote them: the
positional run also ends before a value written ahead of one already in it,
and before any value written after one left for keywords. So the same
reorder turns `helper(x(), y())` into `helper(a=x(), b=y())`, since
positional `helper(y(), x())` would run `y()` first.

A site whose rendered text equals the original is counted but not edited.
Every edit is a span-preserving [patcher](patchers.md) edit; a multi-line
argument list is rendered on one line.

## Unmapped sites

A site is left exactly as written, and listed in `unmapped` with its owner,
path, line, column and reason, when:

- it passes no value for a new parameter without a default and the mapping
  supplies none;
- it passes more positional arguments than the definition declares, a
  keyword the definition does not declare, or the same parameter twice;
- it uses `*args`, `**kwargs`, or a bare generator as its arguments;
- it passes a value that runs code for a parameter the new list drops
  (`helper(1, save())` with `b` dropped), since leaving the value out
  would skip that code; a dropped name or plain literal is simply removed;
- it calls an instance method through a class and passes the receiver
  itself (`Base.f(obj, a)`, `type(obj).f(obj, a)`). A name counts as a
  class when it is the method's own class or is spelled `CapWords`,
  including through an alias (`from pkg.shapes import Base as base`);
- the graph bound it by `heuristic`, `overload` or `dynamic` resolution and
  `allow_heuristic` is false;
- it carries no location, or its file cannot be read.

Unlike `rename`, an unmapped site does not refuse the operation: the
definition still changes, and the list is what the caller (or the
[postcondition contract](postcondition-contract.md)) checks. The contract
requires every call site of the changed signature to read as mapped after
the re-ingest or to be in that list, so a caller the graph never saw is
caught after the fact and the transaction is undone.

## Transaction and contract

The definition and the sites are patched into one
[edit transaction](edit-transactions.md). Every patched file must still
parse, or nothing is written and the report says which files would break.
`dry_run` stages, reports the diff and rolls back. An applied change is
recorded for `cgr edits undo`.

With a re-ingest available (always on the CLI and the MCP server, when the
project is indexed from this tree) the applied change is measured through
the [structural delta](structural-delta.md) and checked against the
contract: symbol and caller counts unchanged, no dangling callers, every
site mapped or listed, no guessed site rewritten without `allow_heuristic`.
A failure undoes the transaction, re-ingests the restored files and reports
the reasons; `verdict.affected_tests` lists the tests reaching the changed
symbols either way.
