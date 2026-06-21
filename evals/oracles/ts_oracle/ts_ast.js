// Authoritative TypeScript structure oracle for the cgr eval harness.
//
// Parses every .ts/.tsx file under a directory with the TypeScript compiler API
// and emits one JSON record per declaration, in cgr's NodeLabel vocabulary, so
// records join cgr's graph on (kind, file, line).
//
// Mapping (TS construct -> cgr NodeLabel), matching how cgr models TypeScript:
//
//   class                         -> Class
//   interface                     -> Interface
//   enum                          -> Enum
//   type alias                    -> Type
//   namespace / module            -> Class   (cgr treats it as a class container)
//   function (top-level/in-fn)    -> Function
//   function (in namespace/class) -> Method
//   const x = () => ... / fn expr -> Function (or Method inside a namespace)
//   method / constructor          -> Method
//
// Run: node ts_ast.js <dir>

const ts = require("typescript");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor", "dist", "build", "out"]);
const out = [];

function emit(kind, file, line, name) {
  out.push({ kind, file, line, name });
}

function lineOf(sf, node) {
  return sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
}

function methodKind(container) {
  return container === "namespace" || container === "class" ? "Method" : "Function";
}

// container: "module" | "class" | "namespace" | "function"
function walk(node, sf, file, container) {
  if (ts.isClassDeclaration(node) && node.name) {
    emit("Class", file, lineOf(sf, node), node.name.text);
    node.members.forEach((m) => walk(m, sf, file, "class"));
    return;
  }
  if (ts.isInterfaceDeclaration(node) && node.name) {
    emit("Interface", file, lineOf(sf, node), node.name.text);
    return;
  }
  if (ts.isEnumDeclaration(node) && node.name) {
    emit("Enum", file, lineOf(sf, node), node.name.text);
    return;
  }
  if (ts.isTypeAliasDeclaration(node) && node.name) {
    emit("Type", file, lineOf(sf, node), node.name.text);
    return;
  }
  if (ts.isModuleDeclaration(node) && node.name) {
    emit("Class", file, lineOf(sf, node), node.name.text || "");
    if (node.body) node.body.forEachChild((c) => walk(c, sf, file, "namespace"));
    return;
  }
  if (ts.isFunctionDeclaration(node) && node.name) {
    emit(methodKind(container), file, lineOf(sf, node), node.name.text);
    if (node.body) node.body.forEachChild((c) => walk(c, sf, file, "function"));
    return;
  }
  if (ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node)) {
    const nm = ts.isConstructorDeclaration(node)
      ? "constructor"
      : node.name && ts.isIdentifier(node.name)
        ? node.name.text
        : node.name && node.name.text;
    if (nm) emit("Method", file, lineOf(sf, node), nm);
    if (node.body) node.body.forEachChild((c) => walk(c, sf, file, "function"));
    return;
  }
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    // (H) cgr captures every arrow/function expression as a Function node (named
    // by its variable when assigned, else anonymous), at the expression's own
    // line. The name is irrelevant to the (kind, file, line) join.
    emit(methodKind(container), file, lineOf(sf, node), "anonymous");
    node.forEachChild((c) => walk(c, sf, file, "function"));
    return;
  }
  node.forEachChild((c) => walk(c, sf, file, container));
}

function visitDir(dir, root) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!IGNORED.has(entry.name)) visitDir(p, root);
    } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.d\.ts$/.test(entry.name)) {
      const src = fs.readFileSync(p, "utf8");
      const sf = ts.createSourceFile(p, src, ts.ScriptTarget.Latest, true);
      const rel = path.relative(root, p).split(path.sep).join("/");
      sf.forEachChild((c) => walk(c, sf, rel, "module"));
    }
  }
}

const root = process.argv[2] || ".";
visitDir(root, root);
process.stdout.write(JSON.stringify(out));
