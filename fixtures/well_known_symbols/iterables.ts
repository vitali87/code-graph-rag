const local = Symbol('local')

export class Collection<T> {
  private items: T[] = [];

  [Symbol.iterator]() { return this.items[Symbol.iterator]() }             // runtime → NOT dead
  async *[Symbol.asyncIterator]() { for (const i of this.items) yield i }  // runtime → NOT dead
  static [Symbol.hasInstance](x: unknown) { return Array.isArray(x) }      // runtime → NOT dead

  iterator() { return this.items }              // plain name, unused → SHOULD be reported (control)
  [Symbol.for('app.tag')]() { return 'x' }      // registry symbol, not well-known → SHOULD be reported
  [local]() { return 'y' }                      // local symbol, not Symbol.<name> → SHOULD be reported

  add(item: T) { this.items.push(item); return this }  // referenced below → live
}

const c = new Collection<number>()
c.add(1)
