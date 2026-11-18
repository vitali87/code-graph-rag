"""XML project files, read by the Rust extension.

`safe_xml.find_all` is the Rust reader (rust/cgr_graph_rs/src/formats/xml.rs),
which replaced defusedxml with no Python fallback, so it must read every
document as defusedxml did. The oracle here is the parser defusedxml wrapped:
Python's own expat, with the entity declarations it refused refused. Every
case runs both ways, over hand-written documents and seeded mutations; the
name and encoding tables the reader carries are checked against the expat and
codecs of the Python running the suite.
"""

from __future__ import annotations

import codecs
import random
import re
from pathlib import Path
from xml.etree import ElementTree
from xml.parsers import expat

import pytest

from codebase_rag.utils import safe_xml
from codebase_rag.utils.safe_xml import (
    XML_CONTENT_ERRORS,
    XmlEntityError,
    XmlSyntaxError,
    find_all,
    find_all_in,
)

_REPO = Path(__file__).resolve().parents[2]
_CHARSETS = _REPO / "rust" / "cgr_graph_rs" / "src" / "formats" / "charsets.rs"
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
    "<!ELEMENT r ANY>",
    "<!ATTLIST r q CDATA 'v'>",
    "%x;",
    " standalone='yes'",
)


class _Refused(Exception):
    pass


def _refuse(*_: object) -> None:
    raise _Refused


def _oracle(doc: bytes, name: str) -> tuple[str, list[dict[str, str]]]:
    """What defusedxml answered: expat, refusing any entity declaration."""
    # expat stops at whichever comes first, an entity declaration or an
    # error, so this pass settles a refusal; ElementTree then reads the tree.
    probe = expat.ParserCreate(None, "}")
    probe.EntityDeclHandler = _refuse
    probe.UnparsedEntityDeclHandler = _refuse
    try:
        probe.Parse(doc, True)
    except _Refused:
        return "refused", []
    except (expat.ExpatError, LookupError, ValueError):
        return "malformed", []
    try:
        root = ElementTree.fromstring(doc)
    except (ElementTree.ParseError, LookupError, ValueError):
        return "malformed", []
    return "parsed", [dict(element.attrib) for element in root.iter(name)]


def _rust(doc: bytes, name: str) -> tuple[str, list[dict[str, str]]]:
    try:
        return "parsed", find_all_in(doc, name)
    except XmlEntityError:
        return "refused", []
    except XmlSyntaxError:
        return "malformed", []


_VERSION = re.compile(rb"""^(?:\xef\xbb\xbf)?<\?xml\s+version\s*=\s*['"]([^'"]*)['"]""")


def _older_expat_accepts(doc: bytes) -> bool:
    """A document only expat before 2.7 read, which the reader refuses as
    expat does now: an XML declaration whose version is not 1.x, or UTF-16
    with an unpaired surrogate (older expat paired it with the next unit)."""
    found = _VERSION.match(doc)
    if found and not re.fullmatch(rb"1\.[0-9]+", found[1]):
        return True
    codec = None
    if doc[:2] in (b"\xff\xfe", b"\xfe\xff"):
        codec = "utf-16"
    elif len(doc) >= 2 and doc[0] == 0:
        codec = "utf-16-be"
    elif len(doc) >= 2 and doc[1] == 0:
        codec = "utf-16-le"
    if codec is None:
        return False
    try:
        doc[: len(doc) - len(doc) % 2].decode(codec)
    except UnicodeDecodeError:
        return True
    return False


def _assert_agree(doc: bytes) -> None:
    for name in _NAMES:
        expected, got = _oracle(doc, name), _rust(doc, name)
        if (
            expected[0] == "parsed"
            and got[0] == "malformed"
            and _older_expat_accepts(doc)
        ):
            return
        assert got == expected, (doc, name)


@pytest.mark.parametrize("seed", list(_SEEDS))
def test_the_seeds_read_as_expat_reads_them(seed: str) -> None:
    doc = _SEEDS[seed].encode()
    assert _oracle(doc, "r")[0] == "parsed"
    _assert_agree(doc)


def test_package_references_come_back_in_document_order() -> None:
    assert find_all_in(_SDK_CSPROJ.encode(), "PackageReference") == [
        {"Include": "Newtonsoft.Json", "Version": "13.0.3"},
        {"Include": "Serilog"},
        {"Include": "Polly", "Version": "8.*", "PrivateAssets": "all"},
    ]


def test_a_namespaced_project_has_no_plain_package_references() -> None:
    # ElementTree matches expanded names, so a legacy project's
    # `PackageReference` is `{msbuild-uri}PackageReference`, as before.
    assert find_all_in(_LEGACY_CSPROJ.encode(), "PackageReference") == []


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


# UTF-16 with and without a byte-order mark or a declaration: expat detects
# the byte order from the first two bytes.
_UTF16_SEEDS = {
    f"{order}-{form}": (bom + text).encode(codec)
    for order, codec, bom in (("le", "utf-16-le", ""), ("be", "utf-16-be", ""))
    for form, text in (
        ("bare", _SDK_CSPROJ.split("\n", 1)[1]),
        ("spaced", " \n" + _SLNX),
        ("bom", "\ufeff" + _SLNX),
        ("declared", "<?xml version='1.0' encoding='UTF-16'?>" + _SLNX),
    )
}


@pytest.mark.parametrize("seed", list(_UTF16_SEEDS))
def test_utf16_documents_read_as_expat_reads_them(seed: str) -> None:
    doc = _UTF16_SEEDS[seed]
    assert _oracle(doc, "Project")[0] == "parsed"
    for name in ("PackageReference", "Project"):
        assert _rust(doc, name) == _oracle(doc, name), name
    rng = random.Random(_FUZZ_SEED)
    for _ in range(_MUTATIONS_PER_SEED // 3):
        mutant = doc
        for _ in range(rng.randint(1, 3)):
            mutant = _mutate(rng, mutant)
        _assert_agree(mutant)


def test_mutated_documents_read_as_expat_reads_them() -> None:
    rng = random.Random(_FUZZ_SEED)
    for seed in _SEEDS.values():
        for _ in range(_MUTATIONS_PER_SEED):
            doc = seed.encode()
            for _ in range(rng.randint(1, 3)):
                doc = _mutate(rng, doc)
            _assert_agree(doc)


@pytest.mark.parametrize(
    "doc",
    [
        '<!DOCTYPE r [<!ATTLIST r a CDATA "d" b NMTOKEN "  x  y ">]><r b=" p  q "/>',
        '<!DOCTYPE r [<!ATTLIST r xmlns CDATA "urn:d" xmlns:p CDATA "urn:p" p:a CDATA "v">]><r><s/></r>',
        '<!DOCTYPE r [<!ELEMENT r (s|t)*><!NOTATION n PUBLIC "-//n//EN">]><r/>',
        '<!DOCTYPE r SYSTEM "r.dtd"><r a="x&foo;y"/>',
        '<!DOCTYPE r [%p;<!ENTITY e "x"><!ATTLIST r a CDATA "d">]><r/>',
        "<?xml version='1.0' encoding='windows-1251'?><r v='\xcf\xf0\xe8'/>",
        "<?xml version='1.0' encoding='ISO-8859-1'?><r\xba/>",
        "\ufeff<?xml version='1.0' encoding='UTF-16'?><r v='\xe9'/>",
    ],
    ids=[
        "attribute-defaults",
        "defaulted-namespaces",
        "element-and-notation",
        "external-subset",
        "parameter-entity",
        "windows-1251",
        "latin-1-letter",
        "utf-16",
    ],
)
def test_dtds_and_encodings_read_as_expat_reads_them(doc: str) -> None:
    if doc.startswith("\ufeff"):
        data = doc.encode("utf-16-le")
    elif "windows-1251" in doc or "ISO-8859-1" in doc:
        data = doc.encode("latin-1")
    else:
        data = doc.encode()
    assert _rust(data, "r") == _oracle(data, "r")
    assert _rust(data, "s") == _oracle(data, "s")


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
        pytest.param(
            '<!DOCTYPE r [<!ENTITY e SYSTEM "u" NDATA n>]><r/>', id="unparsed-entity"
        ),
    ],
)
def test_entity_declarations_are_refused_however_declared(
    doc: str, tmp_path: Path
) -> None:
    path = tmp_path / "Repo.csproj"
    path.write_text(doc, encoding="utf-8")
    assert _oracle(doc.encode(), "r")[0] == "refused"
    with pytest.raises(XmlEntityError):
        find_all(path, "r")


def test_both_errors_are_content_errors_callers_catch(tmp_path: Path) -> None:
    path = tmp_path / "Repo.csproj"
    path.write_text("<Project><ItemGroup></Project>", encoding="utf-8")
    with pytest.raises(XML_CONTENT_ERRORS):
        find_all(path, "PackageReference")
    assert issubclass(XmlSyntaxError, ValueError)
    assert issubclass(XmlEntityError, ValueError)
    with pytest.raises(OSError):
        find_all(tmp_path / "missing.csproj", "PackageReference")


def _legal(code: int) -> bool:
    return code in (0x9, 0xA, 0xD) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD


def test_name_characters_are_expats_in_every_encoding() -> None:
    mismatches = []
    for code in range(0x10000):
        if not _legal(code) or chr(code) in ": \t\r\n":
            continue
        c = chr(code)
        for doc in (f"<{c}/>", f"<a{c}/>"):
            data = doc.encode()
            if _rust(data, "")[0] != _oracle(data, "")[0]:
                mismatches.append(hex(code))
    # expat's Latin-1 table counts three more letters than its Unicode one.
    for c in "\xaa\xb5\xba":
        for data in (
            f"\ufeff<{c}/>".encode("utf-16-le"),
            f"<?xml version='1.0' encoding='ISO-8859-1'?><a{c}/>".encode("latin-1"),
            f"<?xml version='1.0' encoding='latin-1'?><a{c}/>".encode("latin-1"),
        ):
            if _rust(data, "")[0] != _oracle(data, "")[0]:
                mismatches.append(repr(data))
    assert not mismatches, mismatches[:20]


def _carried_codec_names() -> list[str]:
    return re.findall(r'^    \("([^"]+)", \d+\),$', _CHARSETS.read_text(), re.MULTILINE)


# Python's unicode_escape codec warns about the backslash among the bytes.
@pytest.mark.filterwarnings("ignore:invalid escape sequence:DeprecationWarning")
def test_every_carried_encoding_reads_as_pythons_codec_reads_it() -> None:
    names = _carried_codec_names()
    assert {"cp1251", "koi8_r", "latin_1", "windows_1252", "utf8"} <= set(names)
    for name in names:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*", name):
            continue  # not an encoding name XML lets a declaration use
        try:
            codec = codecs.lookup(name).name
        except LookupError:
            continue  # an alias a newer Python added; the tables carry it
        high = bytes(
            b
            for b in range(0x80, 0x100)
            if bytes([b]).decode(name, "replace") != "\ufffd"
            and not _older_palmos_byte(codec, b)
        )
        doc = f"<?xml version='1.0' encoding='{name}'?><r v='".encode() + high + b"'/>"
        assert _rust(doc, "r") == _oracle(doc, "r"), name


def _older_palmos_byte(codec: str, b: int) -> bool:
    # Python 3.13 mapped palmos 0x9B to U+203A, which 3.12 left as U+009B; the
    # tables carry the newer mapping (scripts/generate_xml_charsets.py).
    return codec == "palmos" and b == 0x9B and bytes([b]).decode(codec) == "\x9b"


def test_the_tables_carry_the_newest_pythons_corrections() -> None:
    doc = "<?xml version='1.0' encoding='palmos'?><r v='\x9b'/>".encode("latin-1")
    assert find_all_in(doc, "r") == [{"v": "\u203a"}]
    doc = b"<?xml version='1.0' encoding='windows-874'?><r v='\xa1'/>"
    assert find_all_in(doc, "r") == [{"v": "\u0e01"}]


def test_an_encoding_no_codec_names_is_an_error() -> None:
    doc = b"<?xml version='1.0' encoding='x-no-such-codec'?><r/>"
    assert _rust(doc, "r") == ("malformed", [])


def test_find_all_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "Repo.slnx"
    path.write_text(_SLNX, encoding="utf-8")
    assert [p["Path"] for p in find_all(path, "Project")] == [
        "src\\Main\\Main.csproj",
        "src/Lib/Lib.csproj",
        "bench/Bench.csproj",
    ]
    assert safe_xml.find_all is find_all
