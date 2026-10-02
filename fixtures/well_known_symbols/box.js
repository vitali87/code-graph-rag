// Not exported on purpose: `module.exports = Box` makes every public member
// API surface and therefore a liveness root, which would mask the symbol
// allowlist. The require keeps this a CommonJS module rather than a page
// script, whose top-level classes are globals (and roots) too.
const util = require('util')

class Box {
  get [Symbol.toStringTag]() { return 'Box' }   // runtime-invoked → NOT dead
  usedHelper() { return 42 }                     // referenced below → live
  neverCalled() { return 'dead' }                // genuinely unused → SHOULD be reported
}
const b = new Box()
console.log(util.inspect(b.usedHelper()))
