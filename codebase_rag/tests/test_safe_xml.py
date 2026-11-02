"""XML project files, read by the Rust reader and by the reference parser.

`safe_xml.find_all` takes the Rust extension's answer
(rust/cgr_graph_rs/src/formats/xml.rs) whenever it gives one, so a reader
that drifted from the reference would change which packages and projects
cgr finds for those users alone. The Rust reader may decline any document
(returning None) and the reference then answers; when it does answer, its
answer must be the reference's, and the reference must not have refused the
document. Every case here runs both ways, over hand-written documents and
seeded mutations of them.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from codebase_rag import exceptions as ex
from codebase_rag.utils import safe_xml
from codebase_rag.utils.safe_xml import find_all, find_all_python

_FUZZ_SEED = 2026
_MUTATIONS_PER_SEED = 300

_SDK_CSPROJ = """<?xml version="1.0" encoding="utf-8"?>
<Project Sdk="Microsoft.NET.Sdk">
  <!-- packages -->
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="Serilog"><Version>3.1.1</Version></PackageReference>
    <PackageReference Include='Polly' Version='8.*' PrivateAssets="all"/>
  </ItemGroup>
</Project>
"""

_LEGACY_CSPROJ = (
    "\ufeff<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\r\n"
    '<Project ToolsVersion="15.0" '
    'xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\r\n'
    '  <ItemGroup><PackageReference Include="A" Version="1.0"/></ItemGroup>\r\n'
    "</Project>\r\n"
)

_SLNX = """<Solution>
  <Folder Name="/src/">
    <Project Path="src\\Main\\Main.csproj" />
    <Project Path="src/Lib/Lib.csproj" Type="Classic C#" />
  </Folder>
  <Project Path="bench/Bench.csproj"/>
</Solution>
"""

_EDGES = (
    "<?pi data?><r xmlns:p='urn:p' xml:lang='en'>"
    "<a v='x\ty\r\nz\rw&#10;' r='&lt;&amp;&gt;&quot;&apos;&#65;&#x42;'/>"
    "<p:b p:k='1' k='2'/><c xmlns='urn:d'><b/></c>"
    "text &amp; <![CDATA[<b>]]><?p?><!----><item n='é ✓ \U0001f600'/>"
    "</r><!-- after -->\n"
)

_SEEDS = {
    "sdk-csproj": _SDK_CSPROJ,
    "legacy-csproj": _LEGACY_CSPROJ,
    "slnx": _SLNX,
    "edges": _EDGES,
}

_NAMES = (
    "PackageReference",
    "{http://schemas.microsoft.com/developer/msbuild/2003}PackageReference",
    "Project",
    "Version",
    "Folder",
    "r",
    "a",
    "b",
    "{urn:p}b",
    "{urn:d}b",
    "item",
)

# Fragments a mutation splices in: the syntax the reader must judge.
_FRAGMENTS = (
    "<",
    ">",
    "/",
    "&",
    ";",
    "'",
    '"',
    "=",
    ":",
    "!",
    "?",
    "-",
    "]]>",
    "<!--",
    "-->",
    "&amp;",
    "&#0;",
    "&#x10FFFF;",
    "xmlns=''",
    " xmlns:q='urn:q'",
    " q:x='1'",
    "<!DOCTYPE r>",
    "<?xml version='1.0'?>",
    "\r",
    "\t",
    "\x00",
    "é",
    "\ufeff",
    "5",
    " p:5='1'",
    " xmlns:5='urn:5'",
)

rust = pytest.importorskip("cgr_graph_rs")
if not hasattr(rust, "xml_find_all"):
    pytest.skip(
        "this cgr_graph_rs build predates its XML reader, so cgr reads XML in "
        "Python; reinstall ./rust/cgr_graph_rs to test it",
        allow_module_level=True,
    )


def _reference(doc: bytes, name: str) -> tuple[str, list[dict[str, str]]]:
    try:
        return "parsed", find_all_python(doc, name)
    except ElementTree.ParseError:
        return "malformed", []
    except ex.XmlEntityRefusedError:
        return "refused", []
    except LookupError:
        # An encoding Python has no codec for, as defusedxml raised too.
        return "unknown-encoding", []


def _assert_agree(doc: bytes) -> bool:
    """Whether the Rust reader answered; raises if it answered wrongly."""
    answered = False
    for name in _NAMES:
        found = rust.xml_find_all(doc, name)
        if found is None:
            continue
        answered = True
        assert _reference(doc, name) == ("parsed", found), (doc, name)
    return answered


@pytest.mark.parametrize("seed", list(_SEEDS))
def test_the_rust_reader_answers_the_seeds_as_the_reference_does(seed: str) -> None:
    doc = _SEEDS[seed].encode()
    assert _assert_agree(doc)


def test_package_references_come_back_in_document_order() -> None:
    assert rust.xml_find_all(_SDK_CSPROJ.encode(), "PackageReference") == [
        {"Include": "Newtonsoft.Json", "Version": "13.0.3"},
        {"Include": "Serilog"},
        {"Include": "Polly", "Version": "8.*", "PrivateAssets": "all"},
    ]


def test_a_namespaced_project_has_no_plain_package_references() -> None:
    # ElementTree matches expanded names, so a legacy project's
    # `PackageReference` is `{msbuild-uri}PackageReference`, as before.
    doc = _LEGACY_CSPROJ.encode()
    assert rust.xml_find_all(doc, "PackageReference") == []
    assert find_all_python(doc, "PackageReference") == []


def _mutate(rng: random.Random, doc: bytes) -> bytes:
    at = rng.randrange(len(doc) + 1)
    match rng.randrange(4):
        case 0:
            return doc[:at] + doc[at + rng.randint(1, 4) :]
        case 1:
            return doc[:at] + rng.choice(_FRAGMENTS).encode() + doc[at:]
        case 2:
            end = min(len(doc), at + rng.randint(1, 40))
            return doc[:end] + doc[at:end] + doc[end:]
        case _:
            return doc[:at] + bytes([rng.randrange(256)]) + doc[at + 1 :]


def test_mutated_documents_get_the_reference_answer_or_none() -> None:
    rng = random.Random(_FUZZ_SEED)
    answered = well_formed = 0
    for seed in _SEEDS.values():
        for _ in range(_MUTATIONS_PER_SEED):
            doc = seed.encode()
            for _ in range(rng.randint(1, 3)):
                doc = _mutate(rng, doc)
            agreed = _assert_agree(doc)
            if b"<!DOCTYPE" not in doc and _reference(doc, "r")[0] == "parsed":
                well_formed += 1
                answered += agreed
    # Most mutants are malformed; the ones that stay well-formed (a changed
    # value, a moved space) must mostly be answered, or the reader could
    # decline its way past this test. It declines the odd few on purpose: a
    # non-ASCII name, version "1.-0", encoding "UTF8".
    assert well_formed > 100
    assert answered >= well_formed * 9 // 10, (answered, well_formed)


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(
            '<!DOCTYPE r [<!ENTITY e "x">]><r a="&e;"/>', id="internal-entity"
        ),
        pytest.param(
            '<!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><r>&e;</r>',
            id="external-entity",
        ),
        pytest.param(
            '<!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">'
            '<!ENTITY c "&b;&b;&b;&b;&b;">]><r>&c;</r>',
            id="expansion-bomb",
        ),
        pytest.param(
            '<!DOCTYPE r [<!ENTITY % p SYSTEM "http://example.invalid/x">]><r/>',
            id="parameter-entity",
        ),
    ],
)
def test_entity_declarations_are_refused_however_declared(
    doc: str, tmp_path: Path
) -> None:
    path = tmp_path / "Repo.csproj"
    path.write_text(doc, encoding="utf-8")
    assert rust.xml_find_all(doc.encode(), "r") is None
    with pytest.raises(ex.XmlEntityRefusedError):
        find_all(path, "r")


def test_a_dtd_without_entities_is_read_by_the_reference(tmp_path: Path) -> None:
    path = tmp_path / "Repo.slnx"
    path.write_text(
        '<!DOCTYPE Solution><Solution><Project Path="a"/></Solution>', encoding="utf-8"
    )
    assert find_all(path, "Project") == [{"Path": "a"}]


def test_a_declined_encoding_is_read_by_the_reference(tmp_path: Path) -> None:
    doc = "<?xml version='1.0' encoding='latin-1'?><r><a v='caf\xe9'/></r>"
    path = tmp_path / "Repo.csproj"
    path.write_bytes(doc.encode("latin-1"))
    assert rust.xml_find_all(path.read_bytes(), "a") is None
    assert find_all(path, "a") == [{"v": "café"}]


def test_without_the_extension_the_reference_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Repo.csproj"
    path.write_text(_SDK_CSPROJ, encoding="utf-8")
    monkeypatch.setattr(safe_xml, "rust_extension", lambda: None)
    assert [ref["Include"] for ref in find_all(path, "PackageReference")] == [
        "Newtonsoft.Json",
        "Serilog",
        "Polly",
    ]


def test_malformed_xml_raises_the_parse_error_callers_catch(tmp_path: Path) -> None:
    path = tmp_path / "Repo.csproj"
    path.write_text("<Project><ItemGroup></Project>", encoding="utf-8")
    with pytest.raises(ElementTree.ParseError):
        find_all(path, "PackageReference")


def test_loading_the_reference_leaves_elementtree_accelerated() -> None:
    import _elementtree

    assert sys.modules["xml.etree.ElementTree"] is ElementTree
    assert sys.modules["_elementtree"] is _elementtree
    assert ElementTree.XMLParser is _elementtree.XMLParser
