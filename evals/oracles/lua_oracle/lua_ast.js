// Authoritative Lua structure oracle for the cgr eval harness.
//
// Parses every .lua file with luaparse and emits one JSON record per function
// declaration/expression, in cgr's NodeLabel vocabulary. Lua has no classes, so
// cgr models every function (global, local, table `t.f`, method `t:m`, and
// anonymous function expressions) as a Function node, joined on (kind, file, line).
//
// Containment edges: Lua has no classes/methods, so the only edge is DEFINES,
// from the enclosing function (for a nested function) else the file module
// (keyed at line 0) -> Function.
//
// Call sites: every CallExpression / StringCallExpression / TableCallExpression
// whose callee resolves to a static simple name (bare `foo()`, member `t.f()`,
// method `t:m()`); dynamic callees (`t[k]()`, `(expr)()`) yield no name. The
// Python side keeps only callees whose simple name is a declared first-party
// Function so this measures cgr's cross-file call resolution against ground
// truth. Output is a {nodes, edges, calls} payload.
//
// Run: node lua_ast.js <dir>

const luaparse = require("luaparse");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor"]);
const MODULE_LINE = 0;
const ANONYMOUS = "anonymous";
const nodes = [];
const edges = [];
const calls = [];

// The simple name of a luaparse name reference: a bare Identifier's name, or
// the trailing member of a MemberExpression (`t.f` / `t:m` -> f / m). A
// dynamic index (`t["k"]`) or any other base has no static name.
function refName(ref) {
  if (!ref) return null;
  if (ref.type === "Identifier") return ref.name;
  if (ref.type === "MemberExpression" && ref.identifier) {
    return ref.identifier.name;
  }
  return null;
}

// A function declaration is named by its own identifier when present
// (`function foo`, `function t.f`, `function t:m`), else by the variable it is
// assigned to (`local foo = function`, `t.f = function`), else anonymous.
function declName(node, assignedNames) {
  return refName(node.identifier) || assignedNames.get(node) || ANONYMOUS;
}

function walk(node, file, parentRef, assignedNames) {
  if (node === null || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const c of node) walk(c, file, parentRef, assignedNames);
    return;
  }
  // Record the binding name of any function expression assigned in this
  // statement before recursing, so the FunctionDeclaration handler can name
  // it the way cgr does (lua_utils.extract_assigned_name).
  if (
    (node.type === "LocalStatement" || node.type === "AssignmentStatement") &&
    Array.isArray(node.variables) &&
    Array.isArray(node.init)
  ) {
    for (let i = 0; i < node.init.length; i++) {
      const value = node.init[i];
      if (value && value.type === "FunctionDeclaration") {
        const name = refName(node.variables[i]);
        if (name) assignedNames.set(value, name);
      }
    }
  }
  if (node.type === "FunctionDeclaration" && node.loc) {
    const line = node.loc.start.line;
    nodes.push({
      kind: "Function",
      file,
      line,
      end_line: node.loc.end.line,
      name: declName(node, assignedNames),
    });
    edges.push({
      rel: "DEFINES",
      parent: { kind: parentRef.kind, file, line: parentRef.line },
      child: { kind: "Function", file, line },
    });
    // Functions nested in this one bind to it (its lexical parent).
    const sub = { kind: "Function", line };
    for (const k of Object.keys(node)) {
      if (k === "loc" || k === "range") continue;
      walk(node[k], file, sub, assignedNames);
    }
    return;
  }
  if (
    node.type === "CallExpression" ||
    node.type === "StringCallExpression" ||
    node.type === "TableCallExpression"
  ) {
    const name = refName(node.base);
    if (name) calls.push({ file, name });
  }
  for (const k of Object.keys(node)) {
    if (k === "loc" || k === "range") continue;
    walk(node[k], file, parentRef, assignedNames);
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
        walk(ast, rel, { kind: "Module", line: MODULE_LINE }, new Map());
      } catch (e) {
        // skip files luaparse cannot parse
      }
    }
  }
}

const root = process.argv[2] || ".";
visitDir(root, root);
process.stdout.write(JSON.stringify({ nodes, edges, calls }));
