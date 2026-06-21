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
// Containment edges (matching how cgr models TypeScript containment):
//
//   DEFINES        : the file module -> every named type (class/interface/enum/
//                    namespace, even when nested) and every Function
//   DEFINES_METHOD : the enclosing class/namespace -> Method
//
// cgr keeps type containment flat (all types DEFINEd by the file module, keyed
// at line 0); a Method binds to its enclosing class/namespace; a Function binds
// to its nearest enclosing function, else the module. Output is a {nodes, edges}
// payload joining cgr on (kind, file, line).
//
// Run: node ts_ast.js <dir>

const ts = require("typescript");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor", "dist", "build", "out"]);
const MODULE_LINE = 0;
const nodes = [];
const edges = [];

function emit(kind, file, line, name) {
  nodes.push({ kind, file, line, name });
}

function emitEdge(rel, file, pkind, pline, ckind, cline) {
  edges.push({
    rel,
    parent: { kind: pkind, file, line: pline },
    child: { kind: ckind, file, line: cline },
  });
}

function lineOf(sf, node) {
  return sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
}

function methodKind(container) {
  return container === "namespace" || container === "class" ? "Method" : "Function";
}

// ctx carries the file, the enclosing class/namespace ref (for Methods) and the
// enclosing function ref (for nested Functions).
function defineFunction(node, sf, file, container, ctx, kind, line) {
  if (kind === "Method") {
    if (ctx.typeRef) {
      emitEdge("DEFINES_METHOD", file, ctx.typeRef.kind, ctx.typeRef.line, "Method", line);
    }
  } else {
    const parent = ctx.funcRef || { kind: "Module", line: MODULE_LINE };
    emitEdge("DEFINES", file, parent.kind, parent.line, "Function", line);
  }
}

// container: "module" | "class" | "namespace" | "function"
function walk(node, sf, file, container, ctx) {
  if (ts.isClassDeclaration(node) && node.name) {
    const line = lineOf(sf, node);
    emit("Class", file, line, node.name.text);
    emitEdge("DEFINES", file, "Module", MODULE_LINE, "Class", line);
    const sub = { typeRef: { kind: "Class", line }, funcRef: null };
    node.members.forEach((m) => walk(m, sf, file, "class", sub));
    return;
  }
  if (ts.isInterfaceDeclaration(node) && node.name) {
    const line = lineOf(sf, node);
    emit("Interface", file, line, node.name.text);
    emitEdge("DEFINES", file, "Module", MODULE_LINE, "Interface", line);
    return;
  }
  if (ts.isEnumDeclaration(node) && node.name) {
    const line = lineOf(sf, node);
    emit("Enum", file, line, node.name.text);
    emitEdge("DEFINES", file, "Module", MODULE_LINE, "Enum", line);
    return;
  }
  if (ts.isTypeAliasDeclaration(node) && node.name) {
    const line = lineOf(sf, node);
    emit("Type", file, line, node.name.text);
    emitEdge("DEFINES", file, "Module", MODULE_LINE, "Type", line);
    return;
  }
  if (ts.isModuleDeclaration(node) && node.name) {
    const line = lineOf(sf, node);
    emit("Class", file, line, node.name.text || "");
    emitEdge("DEFINES", file, "Module", MODULE_LINE, "Class", line);
    const sub = { typeRef: { kind: "Class", line }, funcRef: null };
    if (node.body) node.body.forEachChild((c) => walk(c, sf, file, "namespace", sub));
    return;
  }
  if (ts.isFunctionDeclaration(node) && node.name) {
    const kind = methodKind(container);
    const line = lineOf(sf, node);
    emit(kind, file, line, node.name.text);
    defineFunction(node, sf, file, container, ctx, kind, line);
    const sub = { typeRef: null, funcRef: { kind, line } };
    if (node.body) node.body.forEachChild((c) => walk(c, sf, file, "function", sub));
    return;
  }
  if (ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node)) {
    const nm = ts.isConstructorDeclaration(node)
      ? "constructor"
      : node.name && ts.isIdentifier(node.name)
        ? node.name.text
        : node.name && node.name.text;
    // (H) Class members are Methods; object-literal shorthand methods are modelled
    // (H) by cgr as standalone Functions.
    const kind = container === "class" ? "Method" : "Function";
    const line = lineOf(sf, node);
    if (nm) {
      emit(kind, file, line, nm);
      defineFunction(node, sf, file, container, ctx, kind, line);
    }
    const sub = { typeRef: null, funcRef: { kind, line } };
    if (node.body) node.body.forEachChild((c) => walk(c, sf, file, "function", sub));
    return;
  }
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    // (H) cgr captures every arrow/function expression as a Function node (named
    // by its variable when assigned, else anonymous), at the expression's own
    // line. The name is irrelevant to the (kind, file, line) join.
    const kind = methodKind(container);
    const line = lineOf(sf, node);
    emit(kind, file, line, "anonymous");
    defineFunction(node, sf, file, container, ctx, kind, line);
    const sub = { typeRef: null, funcRef: { kind, line } };
    node.forEachChild((c) => walk(c, sf, file, "function", sub));
    return;
  }
  node.forEachChild((c) => walk(c, sf, file, container, ctx));
}

function hasExt(name, exts) {
  return exts.some((e) => name.endsWith(e)) && !name.endsWith(".d.ts");
}

function visitDir(dir, root, exts) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!IGNORED.has(entry.name)) visitDir(p, root, exts);
    } else if (hasExt(entry.name, exts)) {
      const src = fs.readFileSync(p, "utf8");
      const sf = ts.createSourceFile(p, src, ts.ScriptTarget.Latest, true);
      const rel = path.relative(root, p).split(path.sep).join("/");
      const ctx = { typeRef: null, funcRef: null };
      sf.forEachChild((c) => walk(c, sf, rel, "module", ctx));
    }
  }
}

const root = process.argv[2] || ".";
const exts = process.argv.slice(3);
visitDir(root, root, exts.length ? exts : [".ts", ".tsx"]);
process.stdout.write(JSON.stringify({ nodes, edges }));
