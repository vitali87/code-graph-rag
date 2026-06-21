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
// Containment edges (matching how cgr models PHP containment):
//
//   DEFINES        : the file module -> every named type and top-level function
//   DEFINES_METHOD : the enclosing named type -> Method
//
// cgr keeps type containment flat (the file module DEFINES every named type,
// keyed at line 0); a Method binds to its enclosing class/interface/trait/enum;
// a Function/closure binds to its nearest enclosing function, else the module.
// An anonymous-class member is a Function (no DEFINES_METHOD). Output is a
// {nodes, edges} payload joining cgr on (kind, file, line).
//
// Run: node php_ast.js <dir>

const phpParser = require("php-parser");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor"]);
const MODULE_LINE = 0;
const nodes = [];
const edges = [];
const nameEdges = [];

function emit(kind, file, line) {
  nodes.push({ kind, file, line, name: "decl" });
}

function emitEdge(rel, file, pkind, pline, ckind, cline) {
  edges.push({
    rel,
    parent: { kind: pkind, file, line: pline },
    child: { kind: ckind, file, line: cline },
  });
}

function emitNameEdge(rel, file, skind, sline, targetName) {
  nameEdges.push({
    rel,
    source: { kind: skind, file, line: sline },
    target_name: targetName,
  });
}

// (H) Simple name of a php-parser Name ref: its last namespace segment, matching
// (H) how cgr resolves bases by simple name (e.g. \App\Base -> Base).
function phpSimpleName(ref) {
  const n = ref && ref.name ? ref.name : "";
  return n.split("\\").pop();
}

function asList(refs) {
  if (!refs) return [];
  return Array.isArray(refs) ? refs : [refs];
}

// (H) class extends -> INHERITS, implements -> IMPLEMENTS; interface extends
// (H) (an array) -> INHERITS (cgr models superinterfaces as inheritance).
function emitInheritance(node, file, kind, line) {
  const extendsRel = "INHERITS";
  for (const ref of asList(node.extends)) {
    emitNameEdge(extendsRel, file, kind, line, phpSimpleName(ref));
  }
  for (const ref of asList(node.implements)) {
    emitNameEdge("IMPLEMENTS", file, kind, line, phpSimpleName(ref));
  }
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

function walkChildren(node, file, ctx) {
  for (const k of Object.keys(node)) {
    if (k === "loc") continue;
    walk(node[k], file, ctx);
  }
}

// ctx: { container, typeRef, funcRef }
//   container: "module" | "class" | "anon" | "function"
//   typeRef:   enclosing named type {kind,line} (DEFINES_METHOD parent)
//   funcRef:   enclosing function {kind,line} (DEFINES parent for nested fns)
function defineFunctionEdge(file, ctx, kind, line) {
  if (kind === "Method") {
    if (ctx.typeRef) {
      emitEdge("DEFINES_METHOD", file, ctx.typeRef.kind, ctx.typeRef.line, "Method", line);
    }
  } else {
    const parent = ctx.funcRef || { kind: "Module", line: MODULE_LINE };
    emitEdge("DEFINES", file, parent.kind, parent.line, "Function", line);
  }
}

function walk(node, file, ctx) {
  if (node === null || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const c of node) walk(c, file, ctx);
    return;
  }
  switch (node.kind) {
    case "class": {
      if (isAnonymous(node)) {
        // (H) Anonymous class: no node; its methods are Functions bound to the
        // (H) enclosing function/module, so keep funcRef and mark the container.
        walkChildren(node, file, { container: "anon", typeRef: null, funcRef: ctx.funcRef });
      } else {
        const line = declLine(node);
        emit("Class", file, line);
        emitEdge("DEFINES", file, "Module", MODULE_LINE, "Class", line);
        emitInheritance(node, file, "Class", line);
        walkChildren(node, file, { container: "class", typeRef: { kind: "Class", line }, funcRef: null });
      }
      return;
    }
    case "interface": {
      const line = declLine(node);
      emit("Interface", file, line);
      emitEdge("DEFINES", file, "Module", MODULE_LINE, "Interface", line);
      emitInheritance(node, file, "Interface", line);
      walkChildren(node, file, { container: "class", typeRef: { kind: "Interface", line }, funcRef: null });
      return;
    }
    case "trait": {
      const line = declLine(node);
      emit("Class", file, line);
      emitEdge("DEFINES", file, "Module", MODULE_LINE, "Class", line);
      walkChildren(node, file, { container: "class", typeRef: { kind: "Class", line }, funcRef: null });
      return;
    }
    case "enum": {
      const line = declLine(node);
      emit("Enum", file, line);
      emitEdge("DEFINES", file, "Module", MODULE_LINE, "Enum", line);
      emitInheritance(node, file, "Enum", line);
      walkChildren(node, file, { container: "class", typeRef: { kind: "Enum", line }, funcRef: null });
      return;
    }
    case "method": {
      const kind = ctx.container === "anon" ? "Function" : "Method";
      const line = declLine(node);
      emit(kind, file, line);
      defineFunctionEdge(file, ctx, kind, line);
      walkChildren(node, file, { container: "function", typeRef: null, funcRef: { kind, line } });
      return;
    }
    case "function": {
      const line = declLine(node);
      emit("Function", file, line);
      defineFunctionEdge(file, ctx, "Function", line);
      walkChildren(node, file, { container: "function", typeRef: null, funcRef: { kind: "Function", line } });
      return;
    }
    case "closure":
    case "arrowfunc": {
      const line = node.loc.start.line;
      emit("Function", file, line);
      defineFunctionEdge(file, ctx, "Function", line);
      walkChildren(node, file, { container: "function", typeRef: null, funcRef: { kind: "Function", line } });
      return;
    }
    default:
      walkChildren(node, file, ctx);
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
        walk(ast, rel, { container: "module", typeRef: null, funcRef: null });
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
process.stdout.write(JSON.stringify({ nodes, edges, name_edges: nameEdges }));
