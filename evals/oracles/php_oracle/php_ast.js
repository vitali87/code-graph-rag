// Authoritative PHP structure oracle for the cgr eval harness.
//
// Parses every .php file with php-parser (a pure-JS PHP parser) and emits one
// JSON record per declaration, in cgr's NodeLabel vocabulary, joined on
// (kind, file, line).
//
// Mapping (PHP construct -> cgr NodeLabel), matching how cgr models PHP:
//
//   class                       -> Class
//   interface                   -> Interface  (+ its methods -> Method)
//   trait                       -> Class       (cgr models a trait as a Class)
//   enum                        -> Enum
//   method (in named type)      -> Method
//   method (in anonymous class) -> Function     (cgr models these as Functions)
//   function                    -> Function
//   closure / arrow fn          -> Function     (anonymous)
//
// A declaration's line is the line of its first attribute (`#[Attr]`) when
// present, matching cgr's node span; anonymous classes (`new class {...}`) get
// no Class node, like cgr.
//
// Run: node php_ast.js <dir>

const phpParser = require("php-parser");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor"]);
const out = [];

function emit(kind, file, line) {
  out.push({ kind, file, line, name: "decl" });
}

function declLine(node) {
  let line = node.loc.start.line;
  if (Array.isArray(node.attrGroups)) {
    for (const g of node.attrGroups) {
      if (g.loc && g.loc.start.line < line) line = g.loc.start.line;
    }
  }
  return line;
}

function isAnonymous(node) {
  return node.isAnonymous === true || node.name === null;
}

function walkChildren(node, file, container) {
  for (const k of Object.keys(node)) {
    if (k === "loc") continue;
    walk(node[k], file, container);
  }
}

function walk(node, file, container) {
  if (node === null || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const c of node) walk(c, file, container);
    return;
  }
  switch (node.kind) {
    case "class":
      if (isAnonymous(node)) {
        walkChildren(node, file, "anon");
      } else {
        emit("Class", file, declLine(node));
        walkChildren(node, file, "class");
      }
      return;
    case "interface":
      emit("Interface", file, declLine(node));
      walkChildren(node, file, "class");
      return;
    case "trait":
      emit("Class", file, declLine(node));
      walkChildren(node, file, "class");
      return;
    case "enum":
      emit("Enum", file, declLine(node));
      walkChildren(node, file, "class");
      return;
    case "method":
      emit(container === "anon" ? "Function" : "Method", file, declLine(node));
      walkChildren(node, file, "function");
      return;
    case "function":
      emit("Function", file, declLine(node));
      walkChildren(node, file, "function");
      return;
    case "closure":
    case "arrowfunc":
      emit("Function", file, node.loc.start.line);
      walkChildren(node, file, "function");
      return;
    default:
      walkChildren(node, file, container);
  }
}

function visitDir(dir, root, parser) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!IGNORED.has(entry.name)) visitDir(p, root, parser);
    } else if (entry.name.endsWith(".php")) {
      try {
        const ast = parser.parseCode(fs.readFileSync(p, "utf8"));
        const rel = path.relative(root, p).split(path.sep).join("/");
        walk(ast, rel, "module");
      } catch (e) {
        // skip files php-parser cannot parse
      }
    }
  }
}

const root = process.argv[2] || ".";
const parser = new phpParser.Engine({
  parser: { extractDoc: false, suppressErrors: true },
  ast: { withPositions: true },
});
visitDir(root, root, parser);
process.stdout.write(JSON.stringify(out));
