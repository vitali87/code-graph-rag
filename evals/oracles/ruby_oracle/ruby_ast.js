// Authoritative Ruby structure oracle for the cgr eval harness (issue #1190).
//
// Parses every .rb file with Prism, Ruby's official parser, and emits one JSON
// record per definition in cgr's NodeLabel vocabulary:
//
//   DefNode     -> Function (top-level `def`) or Method (inside class/module)
//   ClassNode   -> Class
//   ModuleNode  -> NOT EMITTED as a node; see below.
//
// Ruby modules are deliberately excluded from the emitted node set rather than
// mapped onto Class. cgr has no Module label today, so grading modules against
// one would report a permanent recall miss no implementation could fix. The
// tempting fix — calling a module a Class — makes the ORACLE assert something
// false: modules cannot be instantiated, have no superclass, and join the
// ancestor chain by inclusion rather than inheritance. Ground truth that
// encodes a known-wrong equivalence is worse than one that reports a real gap,
// and it would actively fight a later Module label: correct output would start
// scoring as a regression against this fixture.
//
// So a module contributes no node, and the omission is the honest record of a
// real cgr limitation. Its BODY is still walked, because methods and classes
// nested inside a module are real definitions cgr does emit; they attach to
// whatever namespace encloses the module.
//
// Containment edges: DEFINES from the enclosing class/module (or the file
// module, keyed at line 0) to the definition.
//
// Prism is error tolerant: a file with a syntax error still yields a tree, so a
// malformed file in a corpus degrades to whatever was recovered rather than
// aborting the run.
//
// Run: node ruby_ast.js <dir>

const { loadPrism } = require("@ruby/prism");
const fs = require("fs");
const path = require("path");

const IGNORED = new Set([".git", "node_modules", "vendor"]);
const MODULE_LINE = 0;

const KIND_FUNCTION = "Function";
const KIND_METHOD = "Method";
const KIND_CLASS = "Class";

const REL_DEFINES = "DEFINES";

const nodes = [];
const edges = [];
const calls = [];

// Prism's JS location exposes only `startOffset` and `length` — no line
// numbers (verified against @ruby/prism 1.9.0: the location object's own keys
// are exactly ["startOffset", "length"]). Those offsets are in BYTES. Lines are
// therefore computed from the byte offset against a prefix table built once per
// file, itself indexed in bytes for the same reason. Reading
// `location.startLine` yields undefined, and JSON.stringify silently DROPS an
// undefined value, so the omission surfaces far away as a KeyError on the
// Python side rather than as an error here.
//
// The table is built over the UTF-8 BYTES, not over the JS string. A JS string
// is indexed in UTF-16 code units, so for any source containing a character
// outside ASCII the two disagree and every span below that character drifts by
// the difference — a `def` on line 2 reporting an end_line of 6, or a class in
// a 10-line file ending at line 11. Ruby routinely carries non-ASCII in
// comments and string literals, so this is ordinary input rather than an edge
// case.
//
// `starts` holds the byte offset of each line start, so the 1-based line for an
// offset is the count of line starts at or below it.
function makeLineLookup(source) {
  const bytes = Buffer.from(source, "utf8");
  const starts = [0];
  for (let i = 0; i < bytes.length; i++) {
    // 0x0A is "\n"; a UTF-8 continuation byte can never collide with it,
    // since every byte of a multibyte sequence has the high bit set.
    if (bytes[i] === 0x0a) starts.push(i + 1);
  }
  return (offset) => {
    // Binary search for the last line start <= offset.
    let lo = 0;
    let hi = starts.length - 1;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (starts[mid] <= offset) lo = mid;
      else hi = mid - 1;
    }
    return lo + 1;
  };
}

// A constant path (`class Outer::Inner`) names the trailing constant; that is
// what cgr records as the node's name.
function constantName(node) {
  if (!node) return null;
  if (node.name !== undefined && node.name !== null) return String(node.name);
  // ConstantPathNode carries the tail in `name` on recent Prism, else in
  // `child.name`; fall back so a version bump cannot silently yield null.
  if (node.child && node.child.name) return String(node.child.name);
  return null;
}

function spanOf(node, lineAt) {
  const start = node.location.startOffset;
  return {
    line: lineAt(start),
    end_line: lineAt(start + node.location.length),
  };
}

function record(kind, name, file, node, parentRef, lineAt) {
  const span = spanOf(node, lineAt);
  nodes.push({
    kind,
    name,
    file,
    line: span.line,
    end_line: span.end_line,
  });
  edges.push({
    rel: REL_DEFINES,
    parent: parentRef,
    child: { kind, name, file, line: span.line },
  });
  return span;
}

function walk(node, file, parentRef, insideNamespace, lineAt) {
  if (node === null || typeof node !== "object") return;

  const type = node.constructor && node.constructor.name;

  if (type === "DefNode") {
    const name = String(node.name);
    // A `def` inside a class or module is a Method; at file scope it is a
    // Function. `def self.build` is still a Method of its class.
    const kind = insideNamespace ? KIND_METHOD : KIND_FUNCTION;
    const span = record(kind, name, file, node, parentRef, lineAt);
    // A nested `def` inside a `def` is rare but legal; its parent is the outer
    // definition, and it stays a Method/Function by the same namespace rule.
    const selfRef = { kind, name, file, line: span.line };
    for (const child of node.compactChildNodes()) {
      walk(child, file, selfRef, insideNamespace, lineAt);
    }
    return;
  }

  if (type === "ClassNode") {
    const name = constantName(node.constantPath);
    if (name !== null) {
      const span = record(KIND_CLASS, name, file, node, parentRef, lineAt);
      const selfRef = {
        kind: KIND_CLASS,
        name,
        file,
        line: span.line,
      };
      for (const child of node.compactChildNodes()) {
        walk(child, file, selfRef, true, lineAt);
      }
      return;
    }
  }

  if (type === "ModuleNode") {
    // No node emitted (see the header): cgr has no Module label, and calling a
    // module a Class would put a falsehood in ground truth. The body still
    // walks so nested classes and methods are captured, and `insideNamespace`
    // becomes true so a `def` directly inside a module is a Method rather than
    // a top-level Function — which is what it is, regardless of whether the
    // module itself is representable.
    for (const child of node.compactChildNodes()) {
      walk(child, file, parentRef, true, lineAt);
    }
    return;
  }

  for (const child of node.compactChildNodes()) {
    walk(child, file, parentRef, insideNamespace, lineAt);
  }
}

function collect(dir, out) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (IGNORED.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) collect(full, out);
    else if (entry.name.endsWith(".rb")) out.push(full);
  }
  return out;
}

async function main() {
  const root = process.argv[2];
  if (!root) {
    process.stderr.write("usage: node ruby_ast.js <dir>\n");
    process.exit(2);
  }

  const parse = await loadPrism();

  for (const full of collect(root, [])) {
    const rel = path.relative(root, full).split(path.sep).join("/");
    let source;
    try {
      source = fs.readFileSync(full, "utf8");
    } catch {
      continue;
    }
    let result;
    try {
      result = parse(source);
    } catch (err) {
      // Prism recovers from syntax errors internally; a throw here means the
      // file could not be parsed at all. Skip it rather than aborting the
      // whole corpus, and say so on stderr so it is not silent.
      process.stderr.write(`ruby_oracle: skipped ${rel}: ${err}\n`);
      continue;
    }
    const fileRef = { kind: "Module", name: rel, file: rel, line: MODULE_LINE };
    walk(result.value, rel, fileRef, false, makeLineLookup(source));
  }

  process.stdout.write(JSON.stringify({ nodes, edges, calls }));
}

main().catch((err) => {
  process.stderr.write(`ruby_oracle: ${err}\n`);
  process.exit(1);
});
