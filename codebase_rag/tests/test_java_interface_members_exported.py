"""Issues #2847 and #2701: a Java interface's members are exported.

Every member of an interface is implicitly `public` (JLS 9.4) unless it is
declared `private` (Java 9+), and the idiomatic form omits the keyword. A
method counted as exported only with an explicit `public`/`protected`, so a
public interface's abstract, `default` and `static` methods were never
dead-code roots and a library's interface API was reported dead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tests.test_is_exported_roots import _one, _run

CODEC = """\
package com.acme;

public interface Codec {
    String encode(String s);

    default String encodeTwice(String s) {
        return encode(encode(s));
    }

    static Codec identity() {
        return s -> s;
    }

    public abstract String loud(String s);

    private String trim(String s) {
        return s.trim();
    }

    interface Nested {
        void run();
    }
}
"""

UPPER = """\
package com.acme;

public class Upper implements Codec {
    public String encode(String s) {
        return s.toUpperCase();
    }

    public String loud(String s) {
        return s;
    }

    String pkgHelper() {
        return "p";
    }

    private String secret() {
        return "s";
    }

    interface Local {
        void apply();
    }
}
"""


@pytest.fixture
def exported(tmp_path: Path) -> dict[str, bool]:
    return _run(tmp_path, {"Codec.java": CODEC, "Upper.java": UPPER})


@pytest.mark.parametrize(
    "method",
    [
        "Codec.encode(String)",
        "Codec.encodeTwice(String)",
        "Codec.identity()",
        "Codec.Nested.run()",
        "Upper.Local.apply()",
    ],
)
def test_an_interface_member_without_private_is_exported(
    exported: dict[str, bool], method: str
) -> None:
    assert _one(exported, method) is True


# Negative: what must not change.


@pytest.mark.parametrize(
    ("method", "is_exported"),
    [
        ("Codec.loud(String)", True),
        ("Codec.trim(String)", False),
        ("Upper.encode(String)", True),
        ("Upper.pkgHelper()", False),
        ("Upper.secret()", False),
    ],
)
def test_explicit_and_class_member_visibility_is_unchanged(
    exported: dict[str, bool], method: str, is_exported: bool
) -> None:
    assert _one(exported, method) is is_exported
