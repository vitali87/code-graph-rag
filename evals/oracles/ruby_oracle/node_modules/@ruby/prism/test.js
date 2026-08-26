import test from "node:test";
import assert from "node:assert";
import { loadPrism } from "./src/index.js";
import * as nodes from "./src/nodes.js";

const parse = await loadPrism();

function statement(result) {
  return result.value.statements.body[0];
}

test("node", () => {
  const result = parse("foo");
  assert(result.value instanceof nodes.ProgramNode);
});

test("node? present", () => {
  const result = parse("foo.bar");
  assert(statement(result).receiver instanceof nodes.CallNode);
});

test("node? absent", () => {
  const result = parse("foo");
  assert(statement(result).receiver === null);
});

test("node[]", () => {
  const result = parse("foo.bar");
  assert(result.value.statements.body instanceof Array);
});

test("string", () => {
  const result = parse('"foo"');
  const node = statement(result);

  assert(!node.isForcedUtf8Encoding())
  assert(!node.isForcedBinaryEncoding())

  assert(node.unescaped.value === "foo");
  assert(node.unescaped.encoding === "utf-8");
  assert(node.unescaped.validEncoding);
});

test("forced utf-8 string using \\u syntax", () => {
  const result = parse('# encoding: utf-8\n"\\u{9E7F}"');
  const node = statement(result);
  const str = node.unescaped;

  assert(node.isForcedUtf8Encoding());
  assert(!node.isForcedBinaryEncoding());

  assert(str.value === "鹿");
  assert(str.encoding === "utf-8");
  assert(str.validEncoding);
});

test("forced utf-8 string with invalid byte sequence", () => {
  const result = parse('# encoding: utf-8\n"\\xFF\\xFF\\xFF"');
  const node = statement(result);
  const str = node.unescaped;

  assert(node.isForcedUtf8Encoding());
  assert(!node.isForcedBinaryEncoding());

  assert(str.value === "ÿÿÿ");
  assert(str.encoding === "utf-8");
  assert(!str.validEncoding);
});

test("ascii string with embedded utf-8 character", () => {
  // # encoding: ascii\n"鹿"'
  // # encoding: ascii\n"é¹¿"'
  const ascii_str = new Buffer.from([35, 32, 101, 110, 99, 111, 100, 105, 110, 103, 58, 32, 97, 115, 99, 105, 105, 10, 34, 233, 185, 191, 34]);
  const result = parse(ascii_str);
  const node = statement(result);
  const str = node.unescaped;

  assert(!node.isForcedUtf8Encoding());
  assert(node.isForcedBinaryEncoding());

  assert(str.value === "é¹¿");
  assert(str.encoding === "ascii");
  assert(str.validEncoding);
});

test("forced binary string", () => {
  const result = parse('# encoding: ascii\n"\\xFF\\xFF\\xFF"');
  const node = statement(result);
  const str = node.unescaped;

  assert(!node.isForcedUtf8Encoding());
  assert(node.isForcedBinaryEncoding());

  assert(str.value === "ÿÿÿ");
  assert(str.encoding === "ascii");
  assert(str.validEncoding);
});

test("forced binary string with Unicode character", () => {
  // # encoding: us-ascii\n"\\xFFé¹¿\\xFF"
  const ascii_str = Buffer.from([35, 32, 101, 110, 99, 111, 100, 105, 110, 103, 58, 32, 97, 115, 99, 105, 105, 10, 34, 92, 120, 70, 70, 233, 185, 191, 92, 120, 70, 70, 34]);
  const result = parse(ascii_str);
  const node = statement(result);
  const str = node.unescaped;

  assert(!node.isForcedUtf8Encoding());
  assert(node.isForcedBinaryEncoding());

  assert(str.value === "ÿé¹¿ÿ");
  assert(str.encoding === "ascii");
  assert(str.validEncoding);
});

test("constant", () => {
  const result = parse("foo = 1");
  assert(result.value.locals[0] === "foo");
});

test("constant? present", () => {
  const result = parse("def foo(*bar); end");
  assert(statement(result).parameters.rest.name === "bar");
});

test("constant? absent", () => {
  const result = parse("def foo(*); end");
  assert(statement(result).parameters.rest.name === null);
});

test("constant[]", async() => {
  const result = parse("foo = 1");
  assert(result.value.locals instanceof Array);
});

test("location", () => {
  const result = parse("foo = 1");
  assert(typeof result.value.location.startOffset === "number");
});

test("location? present", () => {
  const result = parse("def foo = bar");
  assert(statement(result).equalLoc !== null);
});

test("location? absent", () => {
  const result = parse("def foo; bar; end");
  assert(statement(result).equalLoc === null);
});

test("uint8", () => {
  const result = parse("-> { _3 }");
  assert(statement(result).parameters.maximum === 3);
});

test("uint32", () => {
  const result = parse("foo = 1");
  assert(statement(result).depth === 0);
});

test("flags", () => {
  const result = parse("/foo/mi");
  const regexp = statement(result);

  assert(regexp.isIgnoreCase());
  assert(regexp.isMultiLine());
  assert(!regexp.isExtended());
});

test("integer (decimal)", () => {
  const result = parse("10");
  assert(statement(result).value === 10);
});

test("integer (hex)", () => {
  const result = parse("0xA");
  assert(statement(result).value === 10);
});

test("integer (2 nodes)", () => {
  const result = parse("4294967296");
  assert(statement(result).value === 4294967296n);
});

test("integer (3 nodes)", () => {
  const result = parse("18446744073709552000");
  assert(statement(result).value === 18446744073709552000n);
});

test("double", () => {
  const result = parse("1.0");
  assert(statement(result).value === 1.0);
});

test("scopes", () => {
  let result;

  result = parse("foo");
  assert(statement(result) instanceof nodes.CallNode);

  result = parse("foo", { scopes: [["foo"]] });
  assert(statement(result) instanceof nodes.LocalVariableReadNode);

  result = parse("foo", { scopes: [{ locals: ["foo"] }] });
  assert(statement(result) instanceof nodes.LocalVariableReadNode);

  result = parse("foo(*)");
  assert(result.errors.length > 0);

  result = parse("foo(*)", { scopes: [{ forwarding: ["*"] }] });
  assert(result.errors.length === 0);
});
