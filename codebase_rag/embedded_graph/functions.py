"""Python implementations of the Cypher functions SQLite has no builtin for.

Registered on every engine connection. They take and return JSON text where
the value may be a list or map, so types survive the trip through SQL.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from functools import partial

import regex

from ..constants.embedded_graph import (
    EG_BYTES_PER_MB,
    EG_CLOCK_POLL_S,
    EG_EMPTY,
    EG_ERR_RANGE_TOO_LARGE,
    EG_JSON_FALSE,
    EG_JSON_SEPARATORS,
    EG_JSON_TRUE,
    EG_PATH_SEPARATOR,
    EG_RANGE_ITEM_BYTES,
    EgSqlFn,
)
from ..types_defs import JsonValue
from .limits import seconds_left
from .types import SqlValue

_MS_PER_S = 1000
_HALF = 0.5
_UNCOUNTABLE = 1 << 64


def _load(text: str | None) -> JsonValue:
    if text is None:
        return None
    return json.loads(text)


def _dump(value: JsonValue) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=EG_JSON_SEPARATORS)


def eg_regex(value: SqlValue, pattern: SqlValue) -> int | None:
    """`value =~ pattern`, stopped at the statement's deadline.

    SQLite's progress handler cannot interrupt a function while it runs, so
    a backtracking pattern would otherwise hold the statement past its time.
    """
    if not isinstance(value, str) or not isinstance(pattern, str):
        return None
    left = seconds_left()
    try:
        # A negative timeout means none to `regex`, which caches the
        # compiled pattern itself.
        matched = regex.fullmatch(
            pattern, value, timeout=None if left is None else max(left, 0.0)
        )
    except TimeoutError:
        # `regex` keeps its own clock; the executor reports a timeout only
        # once the deadline's clock has passed too. That clock can sit a
        # whole tick behind (15.6 ms on Windows before Python 3.13), so one
        # sleep for the time it shows left can end before it moves on.
        while (rest := seconds_left()) is not None and rest >= 0:
            time.sleep(max(rest, EG_CLOCK_POLL_S))
        raise
    return 1 if matched else 0


def eg_list_add(left: str | None, right: str | None) -> str | None:
    a, b = _load(left), _load(right)
    if a is None or b is None:
        return None
    a_list = a if isinstance(a, list) else [a]
    b_list = b if isinstance(b, list) else [b]
    return _dump(a_list + b_list)


def _text(value: JsonValue) -> str:
    if isinstance(value, bool):
        return EG_JSON_TRUE if value else EG_JSON_FALSE
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def eg_concat(left: str | None, right: str | None) -> str | None:
    a, b = _load(left), _load(right)
    if a is None or b is None:
        return None
    return _text(a) + _text(b)


def eg_add(left: str | None, right: str | None) -> str | None:
    a, b = _load(left), _load(right)
    if a is None or b is None:
        return None
    if isinstance(a, list) or isinstance(b, list):
        return eg_list_add(left, right)
    if isinstance(a, str) or isinstance(b, str):
        return _dump(_text(a) + _text(b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return _dump(a + b)
    return None


def eg_pow(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return float(a) ** float(b)


def eg_mod(a: float | None, b: float | None) -> float | int | None:
    if a is None or b is None or b == 0:
        return None
    result = math.fmod(a, b)
    if isinstance(a, int) and isinstance(b, int):
        return int(result)
    return result


def eg_size(value: str | None) -> int | None:
    v = _load(value)
    if v is None:
        return None
    if isinstance(v, (list, str, dict)):
        return len(v)
    return None


def _offset(index: int | None, n: int, default: int) -> int:
    """`index` into a length-`n` sequence, counting from the end if negative."""
    if index is None:
        return default
    return index + n if index < 0 else index


def _bounds(n: int, start: int | None, end: int | None) -> tuple[int, int]:
    s = _offset(start, n, 0)
    e = _offset(end, n, n)
    return max(0, min(s, n)), max(0, min(e, n))


def eg_slice(value: str | None, start: int | None, end: int | None) -> str | None:
    v = _load(value)
    if not isinstance(v, list):
        return None
    s, e = _bounds(len(v), start, end)
    return _dump(v[s:e])


def eg_str_slice(value: str | None, start: int | None, end: int | None) -> str | None:
    if value is None:
        return None
    s, e = _bounds(len(value), start, end)
    return value[s:e]


def _length(items: range) -> int:
    try:
        return len(items)
    except OverflowError:
        # Longer than an index can count, so longer than any limit.
        return _UNCOUNTABLE


def eg_range(
    start: int | None, end: int | None, step: int | None, max_items: int | None = None
) -> str | None:
    """`range(start, end, step)`, refused past `max_items` before it is built.

    Its list is Python's memory, outside the statement's SQLite heap limit;
    `register` derives `max_items` from that limit. MemoryError is what
    SQLite's own limit raises.
    """
    if start is None or end is None or not step:
        return None
    stop = end + (1 if step > 0 else -1)
    items = range(int(start), int(stop), int(step))
    count = _length(items)
    if max_items is not None and count > max_items:
        raise MemoryError(EG_ERR_RANGE_TOO_LARGE.format(count=count))
    return _dump(list(items))


def eg_split(value: str | None, sep: str | None) -> str | None:
    if value is None or sep is None:
        return None
    parts: list[JsonValue] = list(value.split(sep) if sep else value)
    return _dump(parts)


def eg_reverse_str(value: str | None) -> str | None:
    return None if value is None else value[::-1]


def eg_reverse_list(value: str | None) -> str | None:
    v = _load(value)
    return _dump(v[::-1]) if isinstance(v, list) else None


def eg_to_string(value: str | None) -> str | None:
    v = _load(value)
    return None if v is None else _text(v)


def eg_to_integer(value: str | None) -> int | None:
    v = _load(value)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return int(float(v))
            except ValueError:
                return None
    return None


def eg_to_float(value: str | None) -> float | None:
    v = _load(value)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def eg_to_boolean(value: str | None) -> int | None:
    v = _load(value)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, str) and v.lower() in (EG_JSON_TRUE, EG_JSON_FALSE):
        return int(v.lower() == EG_JSON_TRUE)
    return None


def _num(fn: Callable[[float], float]) -> Callable[[float | None], float | None]:
    def wrapped(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            return float(fn(value))
        except ValueError:
            return None

    return wrapped


def _round_half_away(value: float) -> float:
    return math.floor(value + _HALF) if value >= 0 else -math.floor(-value + _HALF)


def eg_sign(value: float | None) -> int | None:
    if value is None:
        return None
    return (value > 0) - (value < 0)


def eg_paths_disjoint(left: str | None, right: str | None) -> int | None:
    """Whether two walks' edge lists (`,1,7,`) share no relationship."""
    if left is None or right is None:
        return None
    ids = set(left.split(EG_PATH_SEPARATOR)) & set(right.split(EG_PATH_SEPARATOR))
    return int(ids <= {EG_EMPTY})


def eg_timestamp() -> int:
    return int(time.time() * _MS_PER_S)


_DETERMINISTIC: dict[str, tuple[int, Callable[..., SqlValue]]] = {
    EgSqlFn.REGEX: (2, eg_regex),
    EgSqlFn.LIST_ADD: (2, eg_list_add),
    EgSqlFn.CONCAT: (2, eg_concat),
    EgSqlFn.ADD: (2, eg_add),
    EgSqlFn.POW: (2, eg_pow),
    EgSqlFn.MOD: (2, eg_mod),
    EgSqlFn.SIZE: (1, eg_size),
    EgSqlFn.SLICE: (3, eg_slice),
    EgSqlFn.STR_SLICE: (3, eg_str_slice),
    EgSqlFn.SPLIT: (2, eg_split),
    EgSqlFn.REVERSE_STR: (1, eg_reverse_str),
    EgSqlFn.REVERSE_LIST: (1, eg_reverse_list),
    EgSqlFn.TO_STRING: (1, eg_to_string),
    EgSqlFn.TO_INTEGER: (1, eg_to_integer),
    EgSqlFn.TO_FLOAT: (1, eg_to_float),
    EgSqlFn.TO_BOOLEAN: (1, eg_to_boolean),
    EgSqlFn.CEIL: (1, _num(math.ceil)),
    EgSqlFn.FLOOR: (1, _num(math.floor)),
    EgSqlFn.ROUND: (1, _num(_round_half_away)),
    EgSqlFn.SQRT: (1, _num(math.sqrt)),
    EgSqlFn.LOG: (1, _num(math.log)),
    EgSqlFn.EXP: (1, _num(math.exp)),
    EgSqlFn.SIGN: (1, eg_sign),
    EgSqlFn.PATHS_DISJOINT: (2, eg_paths_disjoint),
}


def register(conn: sqlite3.Connection, heap_limit_mb: int | None = None) -> None:
    for name, (arity, fn) in _DETERMINISTIC.items():
        conn.create_function(name, arity, fn, deterministic=True)
    max_items = (
        None
        if heap_limit_mb is None
        else heap_limit_mb * EG_BYTES_PER_MB // EG_RANGE_ITEM_BYTES
    )
    conn.create_function(
        EgSqlFn.RANGE, 3, partial(eg_range, max_items=max_items), deterministic=True
    )
    conn.create_function(EgSqlFn.TIMESTAMP, 0, eg_timestamp)
