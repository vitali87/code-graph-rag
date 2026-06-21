// Authoritative Lua structure oracle for the cgr eval harness.
//
// Parses every .lua file with luaparse and emits one JSON record per function
// declaration/expression, in cgr's NodeLabel vocabulary. Lua has no classes, so
// cgr models every function (global, local, table `t.f`, method `t:m`, and
// anonymous function expressions) as a Function node, joined on (kind, file, line).
//
// Run: node lua_ast.js <dir>

const luaparse = require("luaparse");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor"]);
const out = [];

function walk(node, file) {
  if (node === null || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const c of node) walk(c, file);
    return;
  }
  if (node.type === "FunctionDeclaration" && node.loc) {
    out.push({ kind: "Function", file, line: node.loc.start.line, name: "fn" });
  }
  for (const k of Object.keys(node)) {
    if (k === "loc" || k === "range") continue;
    walk(node[k], file);
  }
}

function visitDir(dir, root) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!IGNORED.has(entry.name)) visitDir(p, root);
    } else if (entry.name.endsWith(".lua")) {
      const src = fs.readFileSync(p, "utf8");
      try {
        // luaVersion 5.3 enables bitwise operators / integer division so the
        // oracle parses the same modern Lua that cgr's tree-sitter grammar does.
        const ast = luaparse.parse(src, {
          locations: true,
          comments: false,
          luaVersion: "5.3",
        });
        const rel = path.relative(root, p).split(path.sep).join("/");
        walk(ast, rel);
      } catch (e) {
        // skip files luaparse cannot parse
      }
    }
  }
}

const root = process.argv[2] || ".";
visitDir(root, root);
process.stdout.write(JSON.stringify(out));
