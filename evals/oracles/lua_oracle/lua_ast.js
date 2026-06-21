// Authoritative Lua structure oracle for the cgr eval harness.
//
// Parses every .lua file with luaparse and emits one JSON record per function
// declaration/expression, in cgr's NodeLabel vocabulary. Lua has no classes, so
// cgr models every function (global, local, table `t.f`, method `t:m`, and
// anonymous function expressions) as a Function node, joined on (kind, file, line).
//
// Containment edges: Lua has no classes/methods, so the only edge is DEFINES,
// from the enclosing function (for a nested function) else the file module
// (keyed at line 0) -> Function. Output is a {nodes, edges} payload.
//
// Run: node lua_ast.js <dir>

const luaparse = require("luaparse");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor"]);
const MODULE_LINE = 0;
const nodes = [];
const edges = [];

function walk(node, file, parentRef) {
  if (node === null || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const c of node) walk(c, file, parentRef);
    return;
  }
  if (node.type === "FunctionDeclaration" && node.loc) {
    const line = node.loc.start.line;
    nodes.push({ kind: "Function", file, line, name: "fn" });
    edges.push({
      rel: "DEFINES",
      parent: { kind: parentRef.kind, file, line: parentRef.line },
      child: { kind: "Function", file, line },
    });
    // (H) Functions nested in this one bind to it (its lexical parent).
    const sub = { kind: "Function", line };
    for (const k of Object.keys(node)) {
      if (k === "loc" || k === "range") continue;
      walk(node[k], file, sub);
    }
    return;
  }
  for (const k of Object.keys(node)) {
    if (k === "loc" || k === "range") continue;
    walk(node[k], file, parentRef);
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
        walk(ast, rel, { kind: "Module", line: MODULE_LINE });
      } catch (e) {
        // skip files luaparse cannot parse
      }
    }
  }
}

const root = process.argv[2] || ".";
visitDir(root, root);
process.stdout.write(JSON.stringify({ nodes, edges }));
