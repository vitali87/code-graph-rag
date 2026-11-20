"""cgr does not run without its Rust extension.

The extension does the work behind the Python interface and there is no
pure-Python stand-in for it, so importing the package without it must stop
at once, saying what is missing and how to get it, rather than fail later
somewhere less clear.
"""

from __future__ import annotations

import subprocess
import sys

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex


def test_importing_cgr_without_the_extension_stops_and_says_why() -> None:
    # A None entry in sys.modules makes the import fail as a missing module
    # does, without touching the installed one.
    script = (
        "import sys\n"
        "sys.modules['cgr_graph_rs'] = None\n"
        "try:\n"
        "    import codebase_rag\n"
        "except ImportError as error:\n"
        "    print(error)\n"
        "else:\n"
        "    print('imported')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=True,
        timeout=60,
    )
    output = result.stdout.strip()
    assert output != "imported"
    assert output.startswith(ex.RUST_EXTENSION_MISSING.split("{error}")[0])
    assert "cgr-graph-rs" in output


def test_the_extension_is_installed_here() -> None:
    import cgr_graph_rs

    assert callable(cgr_graph_rs.xml_find_all)
    assert callable(cgr_graph_rs.toml_dumps)
