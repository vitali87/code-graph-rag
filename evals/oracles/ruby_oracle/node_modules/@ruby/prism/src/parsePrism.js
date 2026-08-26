import { ParseResult, deserialize } from "./deserialize.js";

/**
 * Parse the given source code.
 *
 * @typedef {{
 *   locals?: string[],
 *   forwarding?: string[]
 * }} Scope
 *
 * @typedef {{
 *   filepath?: string,
 *   line?: number,
 *   encoding?: string | false,
 *   frozen_string_literal?: boolean,
 *   command_line?: string,
 *   version?: string,
 *   main_script?: boolean,
 *   partial_script?: boolean,
 *   scopes?: (string[] | Scope)[]
 * }} Options<C>
 *
 * @param {WebAssembly.Exports} prism
 * @param {string} source
 * @param {Options} options
 * @returns {ParseResult}
 */
export function parsePrism(prism, source, options = {}) {
  const sourceArray = new TextEncoder().encode(source);
  const sourcePointer = prism.calloc(1, sourceArray.length);

  const packedOptions = dumpOptions(options);
  const optionsPointer = prism.calloc(1, packedOptions.length);

  const bufferPointer = prism.calloc(prism.pm_buffer_sizeof(), 1);
  prism.pm_buffer_init(bufferPointer);

  const sourceView = new Uint8Array(prism.memory.buffer, sourcePointer, sourceArray.length);
  sourceView.set(sourceArray);

  const optionsView = new Uint8Array(prism.memory.buffer, optionsPointer, packedOptions.length);
  optionsView.set(packedOptions);

  prism.pm_serialize_parse(bufferPointer, sourcePointer, sourceArray.length, optionsPointer);
  const serializedView = new Uint8Array(prism.memory.buffer, prism.pm_buffer_value(bufferPointer), prism.pm_buffer_length(bufferPointer));
  const result = deserialize(sourceArray, serializedView);

  prism.pm_buffer_free(bufferPointer);
  prism.free(sourcePointer);
  prism.free(bufferPointer);
  prism.free(optionsPointer);
  return result;
}

/**
 * Dump the command line options into a serialized format.
 *
 * @param {Options} options
 * @returns {number}
 */
function dumpCommandLineOptions(options) {
  if (options.command_line === undefined) {
    return 0;
  }

  const commandLine = options.command_line;
  if (typeof commandLine !== "string") {
    throw new Error("command_line must be a string");
  }

  let value = 0;
  for (let index = 0; index < commandLine.length; index++) {
    switch (commandLine[index]) {
      case "a": value |= 1; break;
      case "e": value |= 2; break;
      case "l": value |= 4; break;
      case "n": value |= 8; break;
      case "p": value |= 16; break;
      case "x": value |= 32; break;
      default: throw new Error(`Unsupported command line flag '${commandLine[index]}'`);
    }
  }

  return value;
}

/**
 * Convert a boolean value into a serialized byte.
 *
 * @param {boolean} value
 * @returns {number}
 */
function dumpBooleanOption(value) {
  return (value === undefined || value === false || value === null) ? 0 : 1;
}

/** 
 * Converts the given options into a serialized options string.
 *
 * @param {Options} options
 * @returns {Uint8Array}
 */
function dumpOptions(options) {
  /** @type {PackTemplate} */
  const template = [];

  /** @type {PackValues} */
  const values = [];

  /** @type {TextEncoder} */
  const encoder = new TextEncoder();

  template.push("L")
  if (options.filepath) {
    const filepath = encoder.encode(options.filepath);
    values.push(filepath.length);
    values.push(filepath);
    template.push("A");
  } else {
    values.push(0);
  }

  template.push("l");
  values.push(options.line || 1);

  template.push("L");
  if (options.encoding) {
    const encoding = encoder.encode(options.encoding);
    values.push(encoding.length);
    values.push(encoding);
    template.push("A");
  } else {
    values.push(0);
  }

  template.push("C");
  values.push(dumpBooleanOption(options.frozen_string_literal));

  template.push("C");
  values.push(dumpCommandLineOptions(options));

  template.push("C");
  if (!options.version || options.version === "latest") {
    values.push(0); // Handled in pm_parser_init
  } else if (options.version.match(/^3\.3(\.\d+)?$/)) {
    values.push(1);
  } else if (options.version.match(/^3\.4(\.\d+)?$/)) {
    values.push(2);
  } else if (options.version.match(/^3\.5(\.\d+)?$/) || options.version.match(/^4\.0(\.\d+)?$/)) {
    values.push(3);
  } else if (options.version.match(/^4\.1(\.\d+)?$/)) {
    values.push(4);
  } else {
    throw new Error(`Unsupported version '${options.version}' in compiler options`);
  }

  template.push("C");
  values.push(options.encoding === false ? 1 : 0);

  template.push("C");
  values.push(dumpBooleanOption(options.main_script));

  template.push("C");
  values.push(dumpBooleanOption(options.partial_script));

  template.push("C");
  values.push(0);

  template.push("L");
  if (options.scopes) {
    const scopes = options.scopes;
    values.push(scopes.length);

    for (const scope of scopes) {
      let locals;
      let forwarding = 0;

      if (Array.isArray(scope)) {
        locals = scope;
      } else {
        locals = scope.locals || [];

        for (const forward of (scope.forwarding || [])) {
          switch (forward) {
            case "*": forwarding |= 0x1; break;
            case "**": forwarding |= 0x2; break;
            case "&": forwarding |= 0x4; break;
            case "...": forwarding |= 0x8; break;
            default: throw new Error(`invalid forwarding value: ${forward}`);
          }
        }
      }

      template.push("L");
      values.push(locals.length);

      template.push("C");
      values.push(forwarding);

      for (const local of locals) {
        template.push("L");
        values.push(local.length);

        template.push("A")
        values.push(encoder.encode(local));
      }
    }
  } else {
    values.push(0);
  }

  return pack(values, template);
}

/**
 * Pack the given values using the given template. This function matches a
 * subset of the functionality from Ruby's Array#pack method.
 *
 * @typedef {(number | string)[]} PackValues
 * @typedef {string[]} PackTemplate
 *
 * @param {PackValues} values 
 * @param {PackTemplate} template 
 * @returns {Uint8Array}
 */
function pack(values, template) {
  const littleEndian = endianness() === "LE";
  const buffer = new ArrayBuffer(totalSizeOf(values, template));
  const data_view = new DataView(buffer);
  let offset = 0;

  for (let i = 0; i < values.length; i ++) {
    switch (template[i]) {
      case "A":
        for (let c = 0; c < values[i].length; c ++) {
          data_view.setUint8(offset + c, values[i][c]);
        }

        break;

      case "l":
        data_view.setInt32(offset, values[i], littleEndian);
        break;

      case "L":
        data_view.setUint32(offset, values[i], littleEndian);
        break;

      case "C":
        data_view.setUint8(offset, values[i], littleEndian);
        break;
    }

    offset += sizeOf(values, template, i);
  }

  return new Uint8Array(buffer);
}

/**
 * Returns the total size of the given values in bytes.
 *
 * @param {PackValues} values
 * @param {PackTemplate} template
 * @returns {number}
 */
function totalSizeOf(values, template) {
  let size = 0;

  for (let i = 0; i < values.length; i ++) {
    size += sizeOf(values, template, i);
  }

  return size;
}

/**
 * Returns the size of the given value inside the list of values at the
 * specified index in bytes.
 * 
 * @param {PackValues} values 
 * @param {PackTemplate} template 
 * @param {number} index 
 * @returns {number}
 */
function sizeOf(values, template, index) {
  switch (template[index]) {
    // arbitrary binary string
    case "A":
      return values[index].length;

    // l: signed 32-bit integer, L: unsigned 32-bit integer
    case "l":
    case "L":
      return 4;

    // 8-bit unsigned integer
    case "C":
      return 1;
  }
}

/**
 * Platform-agnostic implementation of Node's os.endianness().
 *
 * @returns {"BE" | "LE"}
 */
function endianness() {
  const arr = new Uint8Array(4);
  new Uint32Array(arr.buffer)[0] = 0xffcc0011;
  return arr[0] === 0xff ? "BE" : "LE";
}
