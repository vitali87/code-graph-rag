---
description: "Find duplicated functions and methods by comparing the shape of their syntax trees, so you can apply DRY with evidence instead of guesswork."
---

# Duplicate Code Detection

<style>
.dup-fig {
  --match: #3E7C4F; --match-soft: #E3EEE5;
  --diff: #C4552D; --diff-soft: #F6E5DC;
  margin: 1.6rem 0;
}
[data-md-color-scheme="slate"] .dup-fig {
  --match: #7BC08A; --match-soft: #24332A;
  --diff: #E08658; --diff-soft: #3B2A20;
}
.dup-fig .figframe {
  border: 1px solid var(--md-default-fg-color--lightest);
  border-radius: 10px;
  padding: 1.2rem;
  overflow-x: auto;
}
.dup-fig svg { max-width: 100%; height: auto; display: block; margin: 0 auto; min-width: 540px; }
.dup-fig figcaption {
  font-size: 0.8rem;
  color: var(--md-default-fg-color--light);
  margin-top: 0.7rem;
  text-align: center;
}
</style>

`cgr duplicates` reports groups of functions and methods that are **structural
copies of each other** — code that was copy-pasted, possibly renamed, possibly
lightly edited — so you can decide what to merge into one shared
implementation. Detection is purely structural: it compares the shape of each
function's syntax tree, not its text, so two copies that no longer share a
single word are still found.

The results are **candidates for consolidation, not an automatic merge list**.
Two functions can be structurally identical yet intentionally separate (a
pattern the codebase repeats on purpose, generated code, tiny idioms). Read
each group before refactoring.

## Why text search cannot do this

Consider these two functions:

```python
# billing/cart.py
def total_price(items):
    result = 0
    for item in items:
        result += item.price
    return result
```

```python
# shipping/load.py
def sum_weights(boxes):
    acc = 0
    for box in boxes:
        acc += box.weight
    return acc
```

They share **zero variable names**, so `grep` and text-diff tools see nothing.
But they are the same function: same statements, same loop, same accumulation,
in the same order. Only the labels changed — which is exactly what happens when
someone copies a function and renames things to fit the new file. To find this
pair, you have to compare something deeper than the words: the structure.

## How It Works

### Code is a tree

The graph builder already parses every function into a syntax tree — the same
Tree-sitter trees that power the rest of the knowledge graph. A function
contains a body, the body contains statements, a loop contains what happens
inside it:

<figure class="dup-fig">
<div class="figframe">
<svg viewBox="0 0 640 250" role="img" aria-label="A small Python function on the left, and the same function drawn as a tree of grammar nodes on the right.">
  <defs>
    <marker id="dupArr1" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <polygon points="0,0 8,4 0,8" fill="currentColor"/>
    </marker>
  </defs>
  <g fill="currentColor" font-family="JetBrains Mono, monospace" font-size="11">
    <text x="20" y="82">def total(items):</text>
    <text x="20" y="100">    r = 0</text>
    <text x="20" y="118">    for i in items:</text>
    <text x="20" y="136">        r += i</text>
    <text x="20" y="154">    return r</text>
  </g>
  <line x1="185" y1="115" x2="235" y2="115" stroke="currentColor" stroke-width="1.5" marker-end="url(#dupArr1)"/>
  <text x="210" y="103" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="10" text-anchor="middle">parse</text>
  <g font-family="JetBrains Mono, monospace" font-size="11" text-anchor="middle">
    <rect x="380" y="20" width="120" height="26" rx="6" fill="none" stroke="currentColor"/>
    <text x="440" y="37" fill="currentColor">function def</text>
    <rect x="255" y="90" width="90" height="26" rx="6" fill="none" stroke="currentColor"/>
    <text x="300" y="107" fill="currentColor">params</text>
    <rect x="395" y="90" width="90" height="26" rx="6" fill="none" stroke="currentColor"/>
    <text x="440" y="107" fill="currentColor">body</text>
    <rect x="270" y="165" width="100" height="26" rx="6" fill="none" stroke="currentColor"/>
    <text x="320" y="182" fill="currentColor">r = 0</text>
    <rect x="390" y="165" width="100" height="26" rx="6" fill="none" stroke="currentColor"/>
    <text x="440" y="182" fill="currentColor">for loop</text>
    <rect x="510" y="165" width="100" height="26" rx="6" fill="none" stroke="currentColor"/>
    <text x="560" y="182" fill="currentColor">return r</text>
    <rect x="390" y="212" width="100" height="26" rx="6" fill="none" stroke="currentColor"/>
    <text x="440" y="229" fill="currentColor">r += i</text>
  </g>
  <g stroke="currentColor" stroke-width="1">
    <line x1="420" y1="46" x2="305" y2="90"/>
    <line x1="445" y1="46" x2="440" y2="90"/>
    <line x1="420" y1="116" x2="325" y2="165"/>
    <line x1="440" y1="116" x2="440" y2="165"/>
    <line x1="465" y1="116" x2="555" y2="165"/>
    <line x1="440" y1="191" x2="440" y2="212"/>
  </g>
</svg>
</div>
<figcaption>Every indexed function already exists as a tree like this — duplicate detection puts that tree to a second use.</figcaption>
</figure>

### Erase the names, keep the skeleton

Duplicate detection walks that tree and **blanks out everything a copy-paster
would rename**: variable names, function names, field names, literal numbers
and strings, comments. What remains is the function's **skeleton** — pure
structure. Apply that to the two "different" functions above and they collapse
into the identical skeleton:

<figure class="dup-fig">
<div class="figframe">
<svg viewBox="0 0 660 240" role="img" aria-label="Two differently-named functions both reduce to the same skeleton tree once names and numbers are blanked out.">
  <defs>
    <marker id="dupArr2" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <polygon points="0,0 8,4 0,8" fill="currentColor"/>
    </marker>
  </defs>
  <g fill="currentColor" font-family="JetBrains Mono, monospace" font-size="10.5">
    <text x="18" y="52">def total_price(items):</text>
    <text x="18" y="68">  result = 0</text>
    <text x="18" y="84">  for item in items:</text>
    <text x="18" y="100">    result += item.price</text>
    <text x="18" y="116">  return result</text>
    <text x="18" y="164">def sum_weights(boxes):</text>
    <text x="18" y="180">  acc = 0</text>
    <text x="18" y="196">  for box in boxes:</text>
    <text x="18" y="212">    acc += box.weight</text>
    <text x="18" y="228">  return acc</text>
  </g>
  <line x1="230" y1="84" x2="300" y2="115" stroke="currentColor" stroke-width="1.5" marker-end="url(#dupArr2)"/>
  <line x1="230" y1="196" x2="300" y2="150" stroke="currentColor" stroke-width="1.5" marker-end="url(#dupArr2)"/>
  <text x="262" y="80" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="10" text-anchor="middle">erase</text>
  <text x="262" y="188" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="10" text-anchor="middle">names</text>
  <g font-family="JetBrains Mono, monospace" font-size="11" text-anchor="middle">
    <rect x="370" y="22" width="130" height="26" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="435" y="39" fill="currentColor">def &#9633;(&#9633;)</text>
    <rect x="370" y="70" width="130" height="26" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="435" y="87" fill="currentColor">&#9633; = NUM</text>
    <rect x="370" y="118" width="130" height="26" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="435" y="135" fill="currentColor">for &#9633; in &#9633;</text>
    <rect x="392" y="166" width="130" height="26" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="457" y="183" fill="currentColor">&#9633; += &#9633;.&#9633;</text>
    <rect x="370" y="214" width="130" height="26" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="435" y="231" fill="currentColor">return &#9633;</text>
  </g>
  <g stroke="var(--match)" stroke-width="1.2">
    <line x1="435" y1="48" x2="435" y2="70"/>
    <line x1="435" y1="96" x2="435" y2="118"/>
    <line x1="435" y1="144" x2="457" y2="166"/>
    <line x1="457" y1="192" x2="435" y2="214"/>
  </g>
  <text x="560" y="128" fill="var(--match)" font-family="Inter, sans-serif" font-size="13" font-weight="700" text-anchor="middle">ONE skeleton,</text>
  <text x="560" y="146" fill="var(--match)" font-family="Inter, sans-serif" font-size="13" font-weight="700" text-anchor="middle">TWO functions</text>
</svg>
</div>
<figcaption>Blank the parts people rename (&#9633;) and both functions become the same skeleton. That sameness is what gets detected.</figcaption>
</figure>

### One fingerprint per function

Comparing skeletons tree-against-tree for every pair of functions would be
slow, so each skeleton is serialized and hashed into a short **structural
fingerprint**. Same skeleton, same fingerprint, always. The fingerprint is
stamped onto the `Function`/`Method` node in the graph at index time, and
finding clones then costs almost nothing: **group all functions by fingerprint
and see which ones share one.** No pairwise comparison, no thresholds, no
tuning — a group of three functions with the same fingerprint is a clone
family, three places maintaining the same logic.

<figure class="dup-fig">
<div class="figframe">
<svg viewBox="0 0 660 210" role="img" aria-label="Five functions flow through skeleton extraction into fingerprints; three of them land in the same bucket and form a clone group.">
  <defs>
    <marker id="dupArr3" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <polygon points="0,0 8,4 0,8" fill="currentColor"/>
    </marker>
  </defs>
  <g font-family="JetBrains Mono, monospace" font-size="10.5">
    <rect x="15" y="20" width="118" height="24" rx="6" fill="none" stroke="currentColor"/>
    <text x="74" y="36" fill="currentColor" text-anchor="middle">total_price()</text>
    <rect x="15" y="56" width="118" height="24" rx="6" fill="none" stroke="currentColor"/>
    <text x="74" y="72" fill="currentColor" text-anchor="middle">sum_weights()</text>
    <rect x="15" y="92" width="118" height="24" rx="6" fill="none" stroke="currentColor"/>
    <text x="74" y="108" fill="currentColor" text-anchor="middle">count_users()</text>
    <rect x="15" y="128" width="118" height="24" rx="6" fill="none" stroke="currentColor"/>
    <text x="74" y="144" fill="currentColor" text-anchor="middle">parse_config()</text>
    <rect x="15" y="164" width="118" height="24" rx="6" fill="none" stroke="currentColor"/>
    <text x="74" y="180" fill="currentColor" text-anchor="middle">send_email()</text>
  </g>
  <g stroke="currentColor" stroke-width="1.2">
    <line x1="133" y1="32" x2="205" y2="88" marker-end="url(#dupArr3)"/>
    <line x1="133" y1="68" x2="205" y2="96" marker-end="url(#dupArr3)"/>
    <line x1="133" y1="104" x2="205" y2="104" marker-end="url(#dupArr3)"/>
    <line x1="133" y1="140" x2="205" y2="112" marker-end="url(#dupArr3)"/>
    <line x1="133" y1="176" x2="205" y2="120" marker-end="url(#dupArr3)"/>
  </g>
  <rect x="210" y="78" width="150" height="52" rx="8" fill="none" stroke="currentColor" stroke-dasharray="4 3"/>
  <text x="285" y="99" fill="currentColor" font-family="Inter, sans-serif" font-size="12" font-weight="700" text-anchor="middle">skeleton &#8594; hash</text>
  <text x="285" y="116" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="9.5" text-anchor="middle">(at index time)</text>
  <line x1="360" y1="104" x2="420" y2="104" stroke="currentColor" stroke-width="1.5" marker-end="url(#dupArr3)"/>
  <g font-family="JetBrains Mono, monospace" font-size="10.5">
    <rect x="430" y="24" width="210" height="76" rx="8" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="445" y="42" fill="var(--match)" font-weight="600">f3a9c2  &#8592; clone family!</text>
    <text x="445" y="60" fill="currentColor">total_price()</text>
    <text x="445" y="76" fill="currentColor">sum_weights()</text>
    <text x="445" y="92" fill="currentColor">count_users()</text>
    <rect x="430" y="112" width="210" height="34" rx="8" fill="none" stroke="currentColor"/>
    <text x="445" y="133" fill="currentColor">8b11d0  parse_config()</text>
    <rect x="430" y="156" width="210" height="34" rx="8" fill="none" stroke="currentColor"/>
    <text x="445" y="177" fill="currentColor">27ce4f  send_email()</text>
  </g>
</svg>
</div>
<figcaption>Every function gets a fingerprint at index time. Functions that share one are structural copies — found by a simple group-by, even in a huge repo.</figcaption>
</figure>

This catches **exact copies** and **renamed copies** — the two most common
kinds — with certainty: a fingerprint match is not a guess.

### Catching the edited copy

Someone copies a function, renames things, *and changes two lines*. Now the
skeletons differ slightly and the fingerprints differ completely (that is how
hashes work). So each function additionally stores fingerprints of its
individual **branches** — its statements and subtrees. Two functions that
share most of their branch fingerprints are almost certainly a copy with
edits, even though their overall fingerprints no longer match:

<figure class="dup-fig">
<div class="figframe">
<svg viewBox="0 0 660 235" role="img" aria-label="Two skeleton trees share most branches, shown in green; one edited branch differs, shown in orange. A bar shows 5 of 6 branches shared.">
  <g font-family="JetBrains Mono, monospace" font-size="10.5" text-anchor="middle">
    <text x="150" y="20" fill="currentColor" font-family="Inter, sans-serif" font-size="12" font-weight="700">original</text>
    <rect x="95" y="30" width="110" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="150" y="46" fill="currentColor">def &#9633;(&#9633;)</text>
    <rect x="35" y="90" width="105" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="87" y="106" fill="currentColor">&#9633; = NUM</text>
    <rect x="160" y="90" width="105" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="212" y="106" fill="currentColor">for loop</text>
    <rect x="35" y="150" width="105" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="87" y="166" fill="currentColor">&#9633; += &#9633;.&#9633;</text>
    <rect x="160" y="150" width="105" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="212" y="166" fill="currentColor">return &#9633;</text>
  </g>
  <g stroke="var(--match)" stroke-width="1.2">
    <line x1="120" y1="54" x2="87" y2="90"/>
    <line x1="180" y1="54" x2="212" y2="90"/>
    <line x1="87" y1="114" x2="87" y2="150"/>
    <line x1="212" y1="114" x2="212" y2="150"/>
  </g>
  <g font-family="JetBrains Mono, monospace" font-size="10.5" text-anchor="middle">
    <text x="510" y="20" fill="currentColor" font-family="Inter, sans-serif" font-size="12" font-weight="700">copied, then edited</text>
    <rect x="455" y="30" width="110" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="510" y="46" fill="currentColor">def &#9633;(&#9633;)</text>
    <rect x="395" y="90" width="105" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="447" y="106" fill="currentColor">&#9633; = NUM</text>
    <rect x="520" y="90" width="105" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="572" y="106" fill="currentColor">for loop</text>
    <rect x="395" y="150" width="105" height="24" rx="6" fill="var(--diff-soft)" stroke="var(--diff)" stroke-width="1.5"/>
    <text x="447" y="166" fill="var(--diff)">if &#9633; &gt; NUM</text>
    <rect x="520" y="150" width="105" height="24" rx="6" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.5"/>
    <text x="572" y="166" fill="currentColor">return &#9633;</text>
  </g>
  <g stroke="var(--match)" stroke-width="1.2">
    <line x1="480" y1="54" x2="447" y2="90"/>
    <line x1="540" y1="54" x2="572" y2="90"/>
    <line x1="572" y1="114" x2="572" y2="150"/>
  </g>
  <line x1="447" y1="114" x2="447" y2="150" stroke="var(--diff)" stroke-width="1.2"/>
  <rect x="150" y="203" width="360" height="14" rx="7" fill="none" stroke="currentColor"/>
  <rect x="150" y="203" width="300" height="14" rx="7" fill="var(--match)"/>
  <text x="330" y="232" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="10.5" text-anchor="middle">5 of 6 branches shared &#8594; reported with its score</text>
</svg>
</div>
<figcaption>Whole-tree fingerprints miss this pair; branch-by-branch overlap catches it, and the overlap ratio becomes the pair's similarity score.</figcaption>
</figure>

The overlap ratio is the pair's **similarity score**, and pairs at or above
the threshold are reported as near-duplicates. Candidate pairs are found
through the shared branch fingerprints themselves (only functions sharing at
least one branch are ever compared), so the scan stays fast even on large
graphs.

### Where it runs

Fingerprints are computed during the indexing CGR already does, stored on the
graph node like any other property, and read back by the two reporting
surfaces — so running a report is cheap and never re-reads your source code:

<figure class="dup-fig">
<div class="figframe">
<svg viewBox="0 0 660 250" role="img" aria-label="Pipeline: source files are parsed into trees, a fingerprint step stamps each function node in the graph, and two surfaces read the result: the cgr duplicates command and an agent tool.">
  <defs>
    <marker id="dupArr4" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <polygon points="0,0 8,4 0,8" fill="currentColor"/>
    </marker>
  </defs>
  <g font-family="JetBrains Mono, monospace" font-size="10.5" text-anchor="middle">
    <rect x="20" y="85" width="110" height="40" rx="8" fill="none" stroke="currentColor"/>
    <text x="75" y="102" fill="currentColor">your repo</text>
    <text x="75" y="116" fill="currentColor">(14 languages)</text>
    <rect x="180" y="85" width="110" height="40" rx="8" fill="none" stroke="currentColor"/>
    <text x="235" y="102" fill="currentColor">parse to tree</text>
    <text x="235" y="116" fill="currentColor">(Tree-sitter)</text>
    <rect x="340" y="85" width="120" height="40" rx="8" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.8"/>
    <text x="400" y="102" fill="currentColor">fingerprint</text>
    <text x="400" y="116" fill="var(--match)" font-weight="600">skeleton + branches</text>
    <rect x="510" y="78" width="130" height="54" rx="8" fill="none" stroke="currentColor"/>
    <text x="575" y="98" fill="currentColor">graph node</text>
    <text x="575" y="112" fill="currentColor">Function +</text>
    <text x="575" y="126" fill="var(--match)" font-weight="600">fingerprints</text>
    <rect x="250" y="185" width="160" height="44" rx="8" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.8"/>
    <text x="330" y="203" fill="currentColor">cgr duplicates</text>
    <text x="330" y="219" fill="currentColor">DRY report, CI gate</text>
    <rect x="450" y="185" width="190" height="44" rx="8" fill="var(--match-soft)" stroke="var(--match)" stroke-width="1.8"/>
    <text x="545" y="203" fill="currentColor">agent tool</text>
    <text x="545" y="219" fill="currentColor">"does this already exist?"</text>
  </g>
  <g stroke="currentColor" stroke-width="1.5">
    <line x1="130" y1="105" x2="172" y2="105" marker-end="url(#dupArr4)"/>
    <line x1="290" y1="105" x2="332" y2="105" marker-end="url(#dupArr4)"/>
    <line x1="460" y1="105" x2="502" y2="105" marker-end="url(#dupArr4)"/>
    <line x1="545" y1="132" x2="360" y2="180" marker-end="url(#dupArr4)"/>
    <line x1="575" y1="132" x2="555" y2="178" marker-end="url(#dupArr4)"/>
  </g>
  <text x="235" y="70" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="10" text-anchor="middle">during indexing</text>
  <text x="452" y="170" fill="currentColor" font-family="JetBrains Mono, monospace" font-size="10" text-anchor="middle">group by fingerprint</text>
</svg>
</div>
<figcaption>Fingerprints are computed once, during indexing, then queried like any other graph property by the CLI and the agent tool.</figcaption>
</figure>

## Language Coverage

Fingerprints are computed from Tree-sitter syntax trees, so detection covers
the [fully supported languages](../architecture/language-support.md).
Languages served by the structural (ast-grep) tier, such as Ruby, carry no
syntax tree in the graph and are not analyzed; the report prints how many
symbols were skipped for this reason.

Fingerprints are language-specific: a Python function and a TypeScript
function never land in the same group, even if their shapes agree.

## Prerequisites

Index the repository first, so the graph exists in Memgraph:

```bash
cgr daemon up
cgr start --repo-path /path/to/your/repo --update-graph --clean
```

Graphs indexed before duplicate detection existed carry no fingerprints, and
fingerprints are stamped while a file is parsed, so backfilling them means
re-parsing. Which command does that depends on the hash cache:

- **No hash cache for the repo** (never synced from this machine, or the
  cache was cleared): `--update-graph` re-parses everything, because a file
  is skipped only when the cache already holds a matching hash. This
  backfills fingerprints without touching any other project.
- **A populated hash cache**: `--update-graph` skips every unchanged file and
  so backfills nothing. Use `--clean --update-graph` together, which drops
  the cache and re-indexes in one pass.

`--clean` deletes **every** project in the shared graph, not just this one —
it prompts before destroying others, so confirm only when that graph holds
this repository alone. `--clean` on its own wipes without re-indexing and
leaves an empty graph; pair it with `--update-graph`.

## Basic Usage

```bash
cgr duplicates
```

If a single project is indexed it is used automatically. When several are
indexed, name one:

```bash
cgr duplicates --project-name my-project
```

Each reported group lists its members with `file:line` locations, ordered so
the largest wins — the groups with the most copies and the biggest bodies come
first, because that is where applying DRY pays off most.

Every pair inside a group clears the similarity threshold, and a function can
appear in more than one `similar` group: when `A` duplicates both `B` and `C`
but `B` and `C` are not similar to each other, the report shows `{A, B}` and
`{A, C}` rather than lumping all three together or dropping one pair.

Candidate discovery is *exact*: any pair of functions clearing the threshold
is guaranteed to be compared, no matter how common their shared blocks are.
The engine uses prefix filtering — each function is indexed only under its
globally rarest statement blocks, sized so that a qualifying pair always
co-occurs under at least one of them — which keeps the scan far from
all-pairs cost without sacrificing recall. On a pathological codebase (vast
numbers of near-identical bodies) candidate generation stops at a hard
budget and the report says so: the `truncated` flag in the JSON envelope,
the table notice, and the agent tool's warning all fire, so a cut-short
scan can never be mistaken for a complete one.

## Exact Copies vs. Edited Copies

By default the report contains both kinds of finding:

- **Clone groups** — functions with identical structural fingerprints:
  exact copies and renamed copies. These are certain matches.
- **Near-duplicate pairs** — functions whose branch overlap meets the
  similarity threshold: copies that were edited after pasting. These carry a
  score (e.g. `0.87`) so you can judge how close they are.

Tighten or loosen the second kind with `--threshold`:

```bash
# Only report near-duplicates sharing at least 90% of their structure
cgr duplicates --threshold 0.9

# Exact and renamed copies only, skip similarity scoring entirely
cgr duplicates --exact-only
```

## Skipping Trivial Functions

One-line getters, `__repr__` bodies, and trivial delegating wrappers all look
alike by construction — reporting them as "duplicates" is noise. Functions
below the minimum structural size are excluded from the report. Raise the bar
to focus on substantial duplication:

```bash
cgr duplicates --min-size 25
```

## Excluding Paths

Generated code (protobuf stubs, API clients) is duplication by design, and
test suites repeat scaffolding on purpose. Exclude them by file-path glob
rather than raising the threshold:

```bash
cgr duplicates --exact-only --exclude 'tests/*' --exclude '*_generated*'
```

Two rules keep a pattern from silently excluding nothing:

- **Quote the glob.** Unquoted, the shell expands `--exclude src/tests/*`
  into a file listing before `cgr` runs — the first file becomes the pattern
  and the rest are rejected as extra arguments (or, if the glob matches
  nothing, some shells pass it through and others error out). Quotes make
  the shell hand the pattern over untouched.
- **Cover the whole path.** Patterns are matched against the full
  repo-relative file path (`src/billing/cart.py`), and a glob only counts
  when it matches that entire string. A bare directory name like `tests`
  matches nothing; spell the path out from the repo root (`'tests/*'`,
  `'src/tests/*'`) and add `'*/tests/*'` when test directories nest deeper.
  `*` spans `/`, so keep patterns path-scoped: a substring glob like
  `'*tests*'` also excludes production paths such as `contests/entry.py`.

## Jumping to the Code

A report is only useful if you can get from a row to the code. The table
gives you two ways:

**Click a location.** Every `file:line` cell is a terminal hyperlink
(cmd/ctrl-click in iTerm2, Kitty, WezTerm, Windows Terminal, and VS Code's
terminal) that opens the file in your editor at the function's first line.
CGR picks the editor automatically — on macOS, Cursor, Windsurf, and Zed are
detected from the hosting app's bundle identifier; elsewhere, editors that
announce themselves via `TERM_PROGRAM` (Zed, VS Code) are honored, and
anything else falls back to VS Code. VS Code forks inherit
`TERM_PROGRAM=vscode`, so on Linux and Windows set `CGR_EDITOR` to pick a
fork explicitly. Two environment variables override the guess:

```bash
CGR_EDITOR=zed                                    # vscode, cursor, windsurf, zed, idea, textmate, none
CGR_EDITOR_URL_TEMPLATE="myeditor://{path}:{line}" # full control over the URL
```

**Click a group number to compare.** The group cell carries a `diff://`
pair link naming both members; a terminal that understands the scheme
([Croft](https://github.com/vitali87/croft)) opens the first two members
side by side at their lines. Single-file URL schemes cannot express a
pair, so in other terminals the same comparison is a flag:

```bash
cgr duplicates --open 3
```

opens group 3's first two members in your editor's diff view (`code --diff`
and equivalents; `CGR_DIFF_COMMAND="meld {left} {right}"` substitutes any
tool). Groups with more than two members open their first pair — the two
whose paths sort first.

Both features need the graph to record where the project lives on disk;
graphs indexed before this existed fall back to plain text until re-indexed.

## Options

| Option | Description |
|--------|-------------|
| `--project-name`, `-n` | Project to scan. Defaults to the sole indexed project. |
| `--threshold` | Minimum similarity score for a near-duplicate pair, between 0 and 1. Default `0.8`. |
| `--exact-only` | Report only identical-fingerprint clone groups; skip similarity scoring. |
| `--min-size` | Minimum structural size (skeleton nodes) for a function to be considered. Default `15`. |
| `--exclude` | Glob matched against a symbol's whole repo-relative file path to exclude it from the report; quote it. Repeatable. |
| `--format` | Output format: `table` (default) or `json`. |
| `--output`, `-o` | Write the report to this file instead of stdout. |
| `--fail-on-found` | Exit with code 1 when any duplicate is found (useful in CI). |
| `--open` | Open group N's first two members side by side in your editor. |

The JSON report is an envelope carrying the group list and the scan's
coverage, so an artifact never reads as a complete scan when some symbols
could not be analyzed:

```json
{
  "groups": [
    {
      "kind": "exact",
      "similarity": 1.0,
      "node_count": 24,
      "members": [
        {
          "label": "Function",
          "qualified_name": "myproj.billing.total_price",
          "name": "total_price",
          "path": "billing/cart.py",
          "start_line": 5,
          "end_line": 12
        }
      ]
    }
  ],
  "skipped_symbols": 0,
  "truncated": false
}
```

`skipped_symbols` counts functions and methods with no structural
fingerprint: pattern-tier languages and bodiless declarations. `truncated`
is `true` when similar-group enumeration stopped at its internal cap —
qualifying groups may be missing, and narrowing the scan with a higher
`--threshold` or `--min-size` brings the report back under the cap. The
table output prints the same facts as notices after the report.

## Use in CI

Fail a build when duplication appears, writing a JSON report for the job
artifacts:

```bash
cgr duplicates --format json --output duplicates.json --fail-on-found \
  --exclude '*_generated*'
```

The exclude globs follow the same rules as everywhere else: quoted, and
covering the whole repo-relative path.

## Asking the Agent

Duplicate detection is also exposed as an agent tool, so inside `cgr start`
you can simply ask:

```text
> Where are we violating DRY? Show me the biggest duplicated functions.
```

The agent runs the same analysis and can go one step further than the CLI:
having found a clone group, it can read the members, propose the shared
implementation, and rewrite the call sites — turning a report into an actual
DRY refactor.

## What It Catches, and What It Does Not

| Situation | Found? |
|-----------|--------|
| Exact copy-paste | Yes — same fingerprint |
| Copy-paste, then renamed variables/functions | Yes — names are erased before hashing |
| Copy-paste, then changed literals or strings | Yes — literals are erased before hashing |
| Copy-paste, then edited a few statements | Yes — near-duplicate pair with a score |
| Same *behavior* written with genuinely different structure | No — this is semantic equivalence, out of scope |
| Copies across two different languages | No — fingerprints are per-language |

The last two are deliberate. Structural comparison is exact about what it
claims: it finds code that *was copied*, not code that merely does the same
thing. That keeps every reported group trustworthy enough to act on.
