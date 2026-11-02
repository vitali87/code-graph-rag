use super::toml::{self, Value};
use super::xml::{self, Element, Error};

fn attrs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

fn element(name: &str, pairs: &[(&str, &str)]) -> Element {
    Element {
        name: name.to_string(),
        attributes: attrs(pairs),
    }
}

const CSPROJ: &str = r#"<?xml version="1.0" encoding="utf-8"?>
<Project Sdk="Microsoft.NET.Sdk">
  <!-- packages -->
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="Serilog"><Version>3.1.1</Version></PackageReference>
  </ItemGroup>
</Project>
"#;

#[test]
fn a_csproj_yields_its_package_references_in_order() {
    let found = xml::find_all(CSPROJ.as_bytes(), "PackageReference").unwrap();
    assert_eq!(
        found,
        vec![
            attrs(&[("Include", "Newtonsoft.Json"), ("Version", "13.0.3")]),
            attrs(&[("Include", "Serilog")]),
        ]
    );
}

#[test]
fn every_element_is_reported_in_document_order() {
    let found = xml::elements(b"<a x='1'><b/><c><d/></c></a>").unwrap();
    let names: Vec<&str> = found.iter().map(|e| e.name.as_str()).collect();
    assert_eq!(names, ["a", "b", "c", "d"]);
}

#[test]
fn a_byte_order_mark_is_skipped() {
    let doc = [b"\xEF\xBB\xBF".as_slice(), b"<a/>"].concat();
    assert_eq!(xml::elements(&doc).unwrap(), vec![element("a", &[])]);
}

#[test]
fn names_resolve_against_their_namespaces() {
    let doc = br#"<Project xmlns="urn:msb" xmlns:p="urn:p" p:Kind="x" xml:lang="en">
        <PackageReference Include="A"/><p:Item/><Inner xmlns=""/></Project>"#;
    assert_eq!(
        xml::elements(doc).unwrap(),
        vec![
            element(
                "{urn:msb}Project",
                &[
                    ("{urn:p}Kind", "x"),
                    ("{http://www.w3.org/XML/1998/namespace}lang", "en")
                ]
            ),
            element("{urn:msb}PackageReference", &[("Include", "A")]),
            element("{urn:p}Item", &[]),
            element("Inner", &[]),
        ]
    );
    // ElementTree matches the expanded name, so a namespaced project file
    // has no plain `PackageReference`.
    assert_eq!(xml::find_all(doc, "PackageReference").unwrap().len(), 0);
}

#[test]
fn reserved_and_xml_prefixed_declarations_follow_expat() {
    let doc = br#"<a xmlns:xml="http://www.w3.org/XML/1998/namespace" xmlns:xmlx="urn:x" xmlx:k="1" xml:space="preserve"/>"#;
    assert_eq!(
        xml::elements(doc).unwrap(),
        vec![element(
            "a",
            &[
                ("{urn:x}k", "1"),
                ("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            ]
        )]
    );
}

#[test]
fn a_binding_ends_with_its_element() {
    let doc = br#"<r><a xmlns:p="urn:p"><p:b/></a><p:c/></r>"#;
    assert!(matches!(xml::elements(doc), Err(Error::Syntax { .. })));
}

#[test]
fn attribute_values_are_normalised_and_unescaped() {
    let doc = b"<a v='x\ty\r\nz\rw\n' r='&lt;&amp;&gt;&quot;&apos;&#65;&#x42;&#10;'/>";
    assert_eq!(
        xml::elements(doc).unwrap(),
        vec![element("a", &[("v", "x y z w "), ("r", "<&>\"'AB\n")])]
    );
}

#[test]
fn text_comments_cdata_and_instructions_are_read_past() {
    let doc = b"<?pi data?><!-- c --><a>t &amp; <![CDATA[<x>]]><?p?><!----></a><!-- e -->\n";
    assert_eq!(xml::elements(doc).unwrap(), vec![element("a", &[])]);
}

#[test]
fn non_ascii_names_are_names() {
    let doc = "<café ñame='ü'><日本 x·y='1'/></café>".as_bytes();
    assert_eq!(
        xml::elements(doc).unwrap(),
        vec![
            element("café", &[("ñame", "ü")]),
            element("日本", &[("x·y", "1")])
        ]
    );
}

#[test]
fn a_dtd_without_entities_is_read_past() {
    let doc = br#"<!DOCTYPE Solution PUBLIC "-//x//EN" "s.dtd" [
        <!-- c --><?pi x?><!ELEMENT Solution (Project)*><!NOTATION n SYSTEM "a>b">
    ]><Solution><Project Path="a"/></Solution>"#;
    assert_eq!(
        xml::find_all(doc, "Project").unwrap(),
        vec![attrs(&[("Path", "a")])]
    );
    assert!(xml::elements(b"<!DOCTYPE a SYSTEM 'a.dtd'><a/>").is_ok());
    assert!(xml::elements(b"<!DOCTYPE a><a/>").is_ok());
}

#[test]
fn attribute_list_defaults_apply_as_expat_applies_them() {
    let cases: &[(&str, Vec<Element>)] = &[
        (
            r#"<!DOCTYPE r [<!ATTLIST r a CDATA "d" b NMTOKEN "  x  y ">]><r/>"#,
            vec![element("r", &[("a", "d"), ("b", "x y")])],
        ),
        (
            r#"<!DOCTYPE r [<!ATTLIST r a ID #IMPLIED c CDATA #REQUIRED>]><r a=" x  y " c=" z "/>"#,
            vec![element("r", &[("a", "x y"), ("c", " z ")])],
        ),
        (
            r#"<!DOCTYPE r [<!ATTLIST r a CDATA "1"><!ATTLIST r a CDATA "2" b CDATA "3">]><r a="0"/>"#,
            vec![element("r", &[("a", "0"), ("b", "3")])],
        ),
        (
            r#"<!DOCTYPE r [<!ATTLIST r xmlns CDATA "urn:d" xmlns:p CDATA "urn:p" p:a CDATA "v">]><r><s/></r>"#,
            vec![
                element("{urn:d}r", &[("{urn:p}a", "v")]),
                element("{urn:d}s", &[]),
            ],
        ),
        (
            r#"<!DOCTYPE r [<!ATTLIST q a (x|y) "x" n NOTATION (q) #IMPLIED>]><r><q/><x:q xmlns:x="u"/></r>"#,
            vec![
                element("r", &[]),
                element("q", &[("a", "x")]),
                element("{u}q", &[]),
            ],
        ),
        (
            r#"<!DOCTYPE r [<!ATTLIST r a CDATA "&lt;x&#65;	">]><r/>"#,
            vec![element("r", &[("a", "<xA ")])],
        ),
        (
            r#"<!DOCTYPE r [<!ATTLIST r>]><r/>"#,
            vec![element("r", &[])],
        ),
    ];
    for (doc, expected) in cases {
        assert_eq!(&xml::elements(doc.as_bytes()).unwrap(), expected, "{doc}");
    }
}

#[test]
fn an_unseen_dtd_part_changes_what_declarations_do() {
    // A parameter-entity reference stops expat processing the declarations
    // after it, unless the document is standalone.
    assert_eq!(
        xml::elements(br#"<!DOCTYPE r [%p;<!ENTITY e "x"><!ATTLIST r a CDATA "d">]><r/>"#).unwrap(),
        vec![element("r", &[])]
    );
    assert!(matches!(
        xml::elements(
            br#"<?xml version="1.0" standalone="yes"?><!DOCTYPE r [%p;<!ENTITY e "x">]><r/>"#
        ),
        Err(Error::EntityDeclared { .. })
    ));
    // With an external subset or a parameter-entity reference, an unknown
    // reference in an attribute value is dropped, not an error.
    for doc in [
        r#"<!DOCTYPE r SYSTEM "x"><r a="x&foo;y"/>"#,
        r#"<!DOCTYPE r [%p;]><r a="x&foo;y"/>"#,
    ] {
        assert_eq!(
            xml::elements(doc.as_bytes()).unwrap(),
            vec![element("r", &[("a", "xy")])],
            "{doc}"
        );
    }
    assert!(xml::elements(br#"<!DOCTYPE r SYSTEM "x"><r>&foo;</r>"#).is_err());
}

#[test]
fn an_entity_is_refused_where_expat_refuses_it() {
    // Just after an internal entity's value, before the rest is read.
    assert!(matches!(
        xml::elements(br#"<!DOCTYPE r [<!ENTITY e "x"[>]><r/>"#),
        Err(Error::EntityDeclared { .. })
    ));
    // After an unparsed entity's notation.
    assert!(matches!(
        xml::elements(br#"<!DOCTYPE r [<!ENTITY e SYSTEM "u" NDATA n[>]><r/>"#),
        Err(Error::EntityDeclared { .. })
    ));
    // Before a bad byte that comes later.
    assert!(matches!(
        xml::elements(b"<!DOCTYPE r [<!ENTITY e \"x\">]><r\xf8/>"),
        Err(Error::EntityDeclared { .. })
    ));
    // But not past an illegal character that comes first.
    assert!(matches!(
        xml::elements(b"<!DOCTYPE r [<!ENTITY e \"x\x00\">]><r\xf8/>"),
        Err(Error::Syntax { .. })
    ));
}

#[test]
fn every_entity_declaration_refuses_the_document() {
    for (doc, entity) in [
        (r#"<!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>"#, "e"),
        (
            r#"<!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><a/>"#,
            "e",
        ),
        (r#"<!DOCTYPE a [<!ENTITY % p "x">]><a/>"#, "p"),
        (
            r#"<!DOCTYPE a [<!ELEMENT a ANY><!ENTITY b "&b;&b;">]><a>&b;</a>"#,
            "b",
        ),
    ] {
        assert_eq!(
            xml::elements(doc.as_bytes()),
            Err(Error::EntityDeclared {
                name: entity.to_string()
            }),
            "{doc}"
        );
    }
}

#[test]
fn utf16_latin1_ascii_and_windows_1252_documents_are_decoded() {
    let text = "<?xml version='1.0' encoding='UTF-16'?><a v='é€'/>";
    let le: Vec<u8> = [0xFF, 0xFE]
        .into_iter()
        .chain(text.encode_utf16().flat_map(u16::to_le_bytes))
        .collect();
    let be: Vec<u8> = [0xFE, 0xFF]
        .into_iter()
        .chain(text.encode_utf16().flat_map(u16::to_be_bytes))
        .collect();
    let bare_le: Vec<u8> = text.encode_utf16().flat_map(u16::to_le_bytes).collect();
    for doc in [le, be, bare_le] {
        assert_eq!(
            xml::find_all(&doc, "a").unwrap(),
            vec![attrs(&[("v", "é€")])]
        );
    }
    let latin1 = b"<?xml version='1.0' encoding='ISO-8859-1'?><a v='caf\xe9'/>";
    assert_eq!(
        xml::find_all(latin1, "a").unwrap(),
        vec![attrs(&[("v", "café")])]
    );
    let cp1252 = b"<?xml version='1.0' encoding='windows-1252'?><a v='\x80\x93'/>";
    assert_eq!(
        xml::find_all(cp1252, "a").unwrap(),
        vec![attrs(&[("v", "€“")])]
    );
    let ascii = b"<?xml version='1.0' encoding='us-ascii'?><a v='x'/>";
    assert_eq!(
        xml::find_all(ascii, "a").unwrap(),
        vec![attrs(&[("v", "x")])]
    );
}

#[test]
fn encodings_follow_expat_and_pythons_codec_names() {
    // A declared single-byte encoding wins over a UTF-8 byte-order mark.
    let bom_latin1 = b"\xEF\xBB\xBF<?xml version='1.0' encoding='latin-1'?><a v='\xe9'/>";
    assert_eq!(
        xml::find_all(bom_latin1, "a").unwrap(),
        vec![attrs(&[("v", "é")])]
    );
    // Python's other names for a codec, normalised as Python normalises them.
    let alias = b"<?xml version='1.0' encoding='ISO_8859-1_1987'?><a v='\xe9'/>";
    assert_eq!(
        xml::find_all(alias, "a").unwrap(),
        vec![attrs(&[("v", "é")])]
    );
    // A UTF-8 alias expat does not know reads ASCII only.
    assert!(xml::elements(b"<?xml version='1.0' encoding='U8'?><a v='x'/>").is_ok());
    // UTF-16 may name its own byte order, not the other one.
    let text = "<?xml version='1.0' encoding='UTF-16LE'?><a/>";
    let le: Vec<u8> = [0xFF, 0xFE]
        .into_iter()
        .chain(text.encode_utf16().flat_map(u16::to_le_bytes))
        .collect();
    assert!(xml::elements(&le).is_ok());
    let be: Vec<u8> = [0xFE, 0xFF]
        .into_iter()
        .chain(text.encode_utf16().flat_map(u16::to_be_bytes))
        .collect();
    assert!(xml::elements(&be).is_err());
    // Python's other single-byte codecs, as pyexpat hands them to expat.
    let cyrillic = b"<?xml version='1.0' encoding='windows-1251'?><a v='\xcf\xf0\xe8'/>";
    assert_eq!(
        xml::find_all(cyrillic, "a").unwrap(),
        vec![attrs(&[("v", "При")])]
    );
    let koi8 = b"<?xml version='1.0' encoding='KOI8-R'?><a v='\xf0\xd2\xc9'/>";
    assert_eq!(
        xml::find_all(koi8, "a").unwrap(),
        vec![attrs(&[("v", "При")])]
    );
    let dotted = b"<?xml version='1.0' encoding='ISO-8859.1'?><a v='\xe9'/>";
    assert_eq!(
        xml::find_all(dotted, "a").unwrap(),
        vec![attrs(&[("v", "é")])]
    );
    // `º` is a letter in ISO-8859-1 by expat's name for it, not by Python's.
    let native = b"<?xml version='1.0' encoding='ISO-8859-1'?><a\xba/>";
    assert_eq!(
        xml::elements(native).unwrap(),
        vec![element("a\u{ba}", &[])]
    );
}

#[test]
fn malformed_or_unread_documents_are_errors() {
    let malformed: &[&[u8]] = &[
        b"",
        b"   ",
        b"text",
        b"<a>",
        b"<a></b>",
        b"<a/><b/>",
        b"<a/>text",
        b"<a x='1' x='2'/>",
        b"<a xmlns:p='u' xmlns:q='u' p:x='1' q:x='2'/>",
        b"<a x='1'y='2'/>",
        b"<a x=1/>",
        b"<a x='<'/>",
        b"<a x='&nope;'/>",
        b"<a x='&#0;'/>",
        b"<a x='&#x110000;'/>",
        b"<a x='&#xD800;'/>",
        b"<a x='&amp'/>",
        b"<a>]]></a>",
        b"<a>&</a>",
        b"<a><!-- a -- b --></a>",
        b"<a><!-- a ---></a>",
        b"<a><?xml x?></a>",
        b"<?XML version='1.0'?><a/>",
        b" <?xml version='1.0'?><a/>",
        b"<?xml version='2.0'?><a/>",
        b"<?xml version='1.0'encoding='utf-8'?><a/>",
        b"<?xml version='1.0' standalone='maybe'?><a/>",
        b"<a>\x01</a>",
        b"<a>\xEF\xBF\xBE</a>",
        b"<a v='\xE9'/>",
        b"<p:a/>",
        b"<a p:x='1'/>",
        b"<a><![CDATA[x</a>",
        b"<a></a ",
        b"<a/></a>",
        b"<a:b:c xmlns:a='u'/>",
        b"<:a/>",
        b"<a xmlns:p='u' p:5='1'/>",
        b"<a xmlns:p='u' p:-x='1'/>",
        b"<p:5 xmlns:p='u'/>",
        b"<a xmlns:5='u'/>",
        b"<a xmlns:p=''/>",
        b"<a xmlns:xml='urn:other'/>",
        b"<a xmlns:xmlns='u'/>",
        b"<a xmlns:p='http://www.w3.org/XML/1998/namespace'/>",
        b"<a xmlns='http://www.w3.org/2000/xmlns/'/>",
        b"<a xmlns:p='u}v'/>",
        b"<xmlns:a/>",
        b"<!DOCTYPE a><!DOCTYPE a><a/>",
        b"<!DOCTYPE a [%p]><a/>",
        b"<!DOCTYPE a [<!ATTLIST a v CDATA>]><a/>",
        b"<!DOCTYPE a [<!ATTLIST a v CDATA 'x<y'>]><a/>",
        b"<!DOCTYPE a [<!ATTLIST a v CDATA '&u;'>]><a/>",
        b"<!DOCTYPE a [<!ATTLIST a p:v CDATA '1'>]><a/>",
        b"<!DOCTYPE a [<!ATTLIST a a:b:c CDATA '1'>]><a/>",
        b"<!DOCTYPE a [<!ENTITY a:b 'x'>]><a/>",
        b"<!DOCTYPE a [<!ENTITY e SYSTEM 'u'[>]><a/>",
        b"<!DOCTYPE a [<!ELEMENT a:b:c ANY>]><a/>",
        b"<!DOCTYPE a [<!ELEMENT a (b|c,d)>]><a/>",
        b"<!DOCTYPE a [<!ELEMENT a (#PCDATA|b)>]><a/>",
        b"<!DOCTYPE a [<!NOTATION n EM 'a'>]><a/>",
        b"<!DOCTYPE a PUBLIC 'bad{id}' 'u'><a/>",
        b"<!DOCTYPE a [%p;<!ENTITY e 'x'>]><a>&e;</a>",
        b"<a v='&u;'/>",
        b"<a><!DOCTYPE a></a>",
        b"<?xml version='1.0' encoding='x-no-such-codec'?><a/>",
        b"<?xml version='1.0' encoding='cp037'?><a/>",
        b"<?xml version='1.0' encoding='us-ascii'?><a v='\xe9'/>",
        b"<?xml version='1.0' encoding='windows-1252'?><a v='\x81'/>",
        b"\xEF\xBB\xBF<?xml version='1.0' encoding='UTF-16'?><a/>",
        b"<?xml version='1.0' encoding='utf16'?><a/>",
        b"<?xml version='1.0' encoding='U8'?><a v='\xc3\xa9'/>",
        b"<?xml version='1.0' encoding='latin-1'?><a\xba/>",
        b"<?xml version='1.0' encoding='utf-16'?><a/>",
        b"\xFF\xFE<\x00",
    ];
    for doc in malformed {
        assert!(
            matches!(xml::elements(doc), Err(Error::Syntax { .. })),
            "{}",
            String::from_utf8_lossy(doc)
        );
    }
}

#[test]
fn errors_say_where() {
    let error = xml::elements(b"<a>\n  <b></c>\n</a>").unwrap_err();
    assert_eq!(error.to_string(), "mismatched tag: line 2, column 5");
}

#[test]
fn deep_nesting_does_not_recurse() {
    let depth = 100_000;
    let doc = format!("{}{}", "<a>".repeat(depth), "</a>".repeat(depth));
    assert_eq!(xml::elements(doc.as_bytes()).unwrap().len(), depth);
}

#[test]
fn declarations_are_accepted_as_written() {
    for doc in [
        "<?xml version=\"1.0\"?><a/>",
        "<?xml version='1.0' ?><a/>",
        "<?xml version='1.1'?><a/>",
        "<?xml version = '1.0' encoding = 'UTF-8' standalone = 'no' ?><a/>",
        "<?xml-stylesheet href='s'?><a/>",
    ] {
        assert_eq!(
            xml::elements(doc.as_bytes()),
            Ok(vec![element("a", &[])]),
            "{doc}"
        );
    }
}

fn s(text: &str) -> Value {
    Value::Str(text.to_string())
}

fn table(entries: Vec<(&str, Value)>) -> Vec<(String, Value)> {
    entries
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect()
}

fn repo(path: &str, project: &str, named: bool) -> Value {
    Value::Table(table(vec![
        ("path", s(path)),
        ("project_name", s(project)),
        ("project_named", Value::Bool(named)),
    ]))
}

// The expected texts are tomli-w 1.2.0's output for the same values.

#[test]
fn a_workspace_with_short_repos_keeps_them_inline() {
    let doc = table(vec![(
        "workspace",
        Value::Table(table(vec![
            ("name", s("demo")),
            ("description", s("")),
            (
                "repos",
                Value::Array(vec![repo("/r/a", "a", true), repo("/r/b", "b", false)]),
            ),
        ])),
    )]);
    assert_eq!(
        toml::dumps(&doc),
        "[workspace]\nname = \"demo\"\ndescription = \"\"\nrepos = [\n    \
         { path = \"/r/a\", project_name = \"a\", project_named = true },\n    \
         { path = \"/r/b\", project_name = \"b\", project_named = false },\n]\n"
    );
}

#[test]
fn one_long_repo_turns_every_repo_into_a_section() {
    let long = format!("/very/long/path/{}", "x".repeat(80));
    let doc = table(vec![(
        "workspace",
        Value::Table(table(vec![
            ("name", s("demo")),
            ("description", s("two\nlines \"q\" \\ \t\u{1}\u{7f} é")),
            (
                "repos",
                Value::Array(vec![repo(&long, "a", true), repo("/r/b", "b", false)]),
            ),
        ])),
    )]);
    assert_eq!(
        toml::dumps(&doc),
        format!(
            "[workspace]\nname = \"demo\"\n\
             description = \"two\\nlines \\\"q\\\" \\\\ \t\\u0001\\u007f é\"\n\n\
             [[workspace.repos]]\npath = \"{long}\"\nproject_name = \"a\"\nproject_named = true\n\n\
             [[workspace.repos]]\npath = \"/r/b\"\nproject_name = \"b\"\nproject_named = false\n"
        )
    );
}

#[test]
fn an_empty_repo_list_is_an_empty_array() {
    let doc = table(vec![(
        "workspace",
        Value::Table(table(vec![
            ("name", s("demo")),
            ("description", s("")),
            ("repos", Value::Array(vec![])),
        ])),
    )]);
    assert_eq!(
        toml::dumps(&doc),
        "[workspace]\nname = \"demo\"\ndescription = \"\"\nrepos = []\n"
    );
}

#[test]
fn keys_arrays_and_nested_tables_follow_tomli_w() {
    let doc = table(vec![
        ("a b", Value::Number("1".into())),
        ("", Value::Number("-2".into())),
        (
            "ok-key_1",
            Value::Array(vec![
                Value::Number("1".into()),
                Value::Array(vec![Value::Number("2".into()), Value::Number("3".into())]),
                Value::Table(vec![]),
            ]),
        ),
        (
            "nested",
            Value::Table(table(vec![(
                "t",
                Value::Table(table(vec![("x", Value::Bool(true))])),
            )])),
        ),
    ]);
    assert_eq!(
        toml::dumps(&doc),
        "\"a b\" = 1\n\"\" = -2\nok-key_1 = [\n    1,\n    [\n        2,\n        3,\n    ],\n    {},\n]\n\n[nested.t]\nx = true\n"
    );
}
