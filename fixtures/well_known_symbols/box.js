class Box {
  get [Symbol.toStringTag]() { return 'Box' }   // runtime-invoked → NOT dead
  usedHelper() { return 42 }                     // referenced below → live
  neverCalled() { return 'dead' }                // genuinely unused → SHOULD be reported
}
const b = new Box()
console.log(b.usedHelper())
module.exports = Box
