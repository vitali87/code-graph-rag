"""Python implementations of the Cypher functions SQLite has no builtin for.

Registered on every engine connection. They take and return JSON text where
the value may be a list or map, so types survive the trip through SQL.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from functools import lru_cache

from ..constants.embedded_graph import EG_COMPILE_CACHE_SIZE
from ..types_defs import JsonValue
from .types import SqlValue

_MS_PER_S = 1000
_HALF = 0.5


def _load(text: str | None) -> JsonValue:
    if text is None:
        return None
    return json.loads(text)


def _dump(value: JsonValue) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"))


@lru_cache(maxsize=EG_COMPILE_CACHE_SIZE)
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def eg_regex(value: SqlValue, pattern: SqlValue) -> int | None:
    if not isinstance(value, str) or not isinstance(pattern, str):
        return None
    return 1 if _regex(pattern).fullmatch(value) else 0


def eg_list_add(left: str | None, right: str | None) -> str | None:
    a, b = _load(left), _load(right)
    if a is None or b is None:
        return None
    a_list = a if isinstance(a, list) else [a]
    b_list = b if isinstance(b, list) else [b]
    return _dump(a_list + b_list)


def _text(value: JsonValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
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


def _bounds(n: int, start: int | None, end: int | None) -> tuple[int, int]:
    s = 0 if start is None else (start + n if start < 0 else start)
    e = n if end is None else (end + n if end < 0 else end)
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


def eg_range(start: int | None, end: int | None, step: int | None) -> str | None:
    if start is None or end is None or not step:
        return None
    stop = end + (1 if step > 0 else -1)
    return _dump(list(range(int(start), int(stop), int(step))))


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
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return int(v.lower() == "true")
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


def eg_timestamp() -> int:
    return int(time.time() * _MS_PER_S)


_DETERMINISTIC: dict[str, tuple[int, Callable[..., SqlValue]]] = {
    "eg_regex": (2, eg_regex),
    "eg_list_add": (2, eg_list_add),
    "eg_concat": (2, eg_concat),
    "eg_add": (2, eg_add),
    "eg_pow": (2, eg_pow),
    "eg_mod": (2, eg_mod),
    "eg_size": (1, eg_size),
    "eg_slice": (3, eg_slice),
    "eg_str_slice": (3, eg_str_slice),
    "eg_range": (3, eg_range),
    "eg_split": (2, eg_split),
    "eg_reverse_str": (1, eg_reverse_str),
    "eg_reverse_list": (1, eg_reverse_list),
    "eg_to_string": (1, eg_to_string),
    "eg_to_integer": (1, eg_to_integer),
    "eg_to_float": (1, eg_to_float),
    "eg_to_boolean": (1, eg_to_boolean),
    "eg_ceil": (1, _num(math.ceil)),
    "eg_floor": (1, _num(math.floor)),
    "eg_round": (1, _num(_round_half_away)),
    "eg_sqrt": (1, _num(math.sqrt)),
    "eg_log": (1, _num(math.log)),
    "eg_exp": (1, _num(math.exp)),
    "eg_sign": (1, eg_sign),
}


def register(conn: sqlite3.Connection) -> None:
    for name, (arity, fn) in _DETERMINISTIC.items():
        conn.create_function(name, arity, fn, deterministic=True)
    conn.create_function("eg_timestamp", 0, eg_timestamp)
