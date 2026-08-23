"""PostgreSQL identifier normalization.

One normalizer serves both sides of SQL routine resolution: the definition
side (`CREATE FUNCTION App."MyFunc"`) and the string-call side
(`callSp('App."MyFunc"')`). If the two sides folded differently, a routine
and the call that names it would register under different keys and never
connect.

PostgreSQL's rules: an unquoted identifier folds to lowercase; a quoted one
keeps its case, may contain dots that are NOT qualifier separators, and
doubles its quotes to embed one (`"a""b"` names `a"b`).
"""

from __future__ import annotations


def normalize_sql_reference(reference: str) -> str:
    """`App."My.Func"` -> `app.My.Func`; `"a""b"` -> `a"b`; `MyFunc` -> `myfunc`.

    Splits into segments on dots OUTSIDE quotes only, folds unquoted segments
    to lowercase, and keeps quoted segments verbatim (quotes stripped, doubled
    quotes unescaped). Empty segments vanish, so a malformed reference still
    yields its salvageable parts rather than nothing.
    """
    segments: list[str] = []
    buf: list[str] = []
    in_quotes = False
    saw_quotes = False
    i = 0

    def flush() -> None:
        nonlocal saw_quotes
        segment = "".join(buf) if saw_quotes else "".join(buf).strip().lower()
        if segment:
            segments.append(segment)
        buf.clear()
        saw_quotes = False

    while i < len(reference):
        ch = reference[i]
        if ch == '"':
            if in_quotes and i + 1 < len(reference) and reference[i + 1] == '"':
                buf.append('"')
                i += 2
                continue
            in_quotes = not in_quotes
            saw_quotes = True
            i += 1
            continue
        if ch == "." and not in_quotes:
            flush()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return ".".join(segments)
