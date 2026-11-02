use super::toml::{self, Value};
use super::xml::{self, Element};

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
fn a_binding_ends_with_its_element() {
    let doc = br#"<r><a xmlns:p="urn:p"><p:b/></a><p:c/></r>"#;
    assert_eq!(xml::elements(doc), None);
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
fn documents_outside_the_reader_are_declined() {
    let declined: &[&[u8]] = &[
        // A DTD is the Python parser's: it refuses entity declarations.
        b"<!DOCTYPE a [<!ENTITY e 'x'>]><a>&e;</a>",
        b"<!DOCTYPE a><a/>",
        // Encodings other than UTF-8, declared or detected.
        b"<?xml version='1.0' encoding='latin-1'?><a/>",
        b"\xFF\xFE<\x00a\x00/\x00>\x00",
        b"<a v='\xE9'/>",
        // Non-ASCII names, and names namespaces reject.
        "<é/>".as_bytes(),
        "<aé/>".as_bytes(),
        b"<a:b:c xmlns:a='u'/>",
        b"<:a/>",
        b"<a xmlns:p='u' p:5='1'/>",
        b"<a xmlns:p='u' p:-x='1'/>",
        b"<p:5 xmlns:p='u'/>",
        b"<a xmlns:5='u'/>",
        b"<a xmlns:-p='u'/>",
        // Namespace declarations past the plain cases.
        b"<a xmlns:p=''/>",
        b"<a xmlns:xml='http://www.w3.org/XML/1998/namespace'/>",
        b"<a xmlns:xmlns='u'/>",
        b"<a xmlns:p='u}v'/>",
        b"<a xmlns='http://www.w3.org/XML/1998/namespace'/>",
        b"<xml:a/>",
    ];
    for doc in declined {
        assert_eq!(xml::elements(doc), None, "{}", String::from_utf8_lossy(doc));
    }
}

#[test]
fn malformed_documents_are_declined() {
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
        b"<?xml version='1.1'?><a/>",
        b"<?xml version='1.0'encoding='utf-8'?><a/>",
        b"<?xml version='1.0' standalone='maybe'?><a/>",
        b"<a>\x01</a>",
        b"<a>\xEF\xBF\xBE</a>",
        b"<p:a/>",
        b"<a p:x='1'/>",
        b"<a><![CDATA[x</a>",
        b"<a></a ",
        b"<a/></a>",
    ];
    for doc in malformed {
        assert_eq!(xml::elements(doc), None, "{}", String::from_utf8_lossy(doc));
    }
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
        "<?xml version = '1.0' encoding = 'UTF-8' standalone = 'no' ?><a/>",
        "<?xml-stylesheet href='s'?><a/>",
    ] {
        assert_eq!(
            xml::elements(doc.as_bytes()),
            Some(vec![element("a", &[])]),
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
        ("a b", Value::Int(1)),
        ("", Value::Int(-2)),
        (
            "ok-key_1",
            Value::Array(vec![
                Value::Int(1),
                Value::Array(vec![Value::Int(2), Value::Int(3)]),
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
