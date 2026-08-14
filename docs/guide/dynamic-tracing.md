# Dynamic Call Tracing

Static analysis cannot see every call: dispatch through registries, `getattr`
lookups, callbacks routed by frameworks, and monkey-patched targets only exist
at runtime. Dynamic tracing runs your code (typically the test suite), records
which functions actually called which, and merges those observations into the
graph alongside the statically derived `CALLS` edges.

Currently supported for **Python** codebases (Python 3.12+ at runtime, via
`sys.monitoring`) and **Java/Scala** codebases (via a zero-dependency
`java.lang.instrument` agent, JDK 24+). Other language runtimes are tracked
in [issue #1244](https://github.com/vitali87/code-graph-rag/issues/1244).

## Recording a trace

The `cgr` package ships a pytest plugin. It is inert unless enabled:

```bash
cd /path/to/your-repo
pytest --cgr-trace
```

This writes `cgr-trace.jsonl` (override with `--cgr-trace-output PATH`). Each
test's node id is attached to the calls it triggered, so an edge in the graph
can tell you *which tests* exercise it. Tracing is scoped to files under the
pytest root; pass `--cgr-trace-repo PATH` if your repository root differs.

Under `pytest-xdist` each worker traces its own interpreter and writes its own
file with the worker id in the name (`cgr-trace-gw0.jsonl`, ...); ingest each
file to cover the whole run. Within one process, workload attribution is
best-effort for multi-threaded code: calls made by background threads are
attributed to the test the main thread was running.

Any other workload can be traced programmatically:

```python
from pathlib import Path
from codebase_rag.trace.tracer import CallGraphTracer

tracer = CallGraphTracer(Path("/path/to/your-repo"))
tracer.start()
try:
    run_your_workload()
finally:
    tracer.stop()
tracer.write(Path("cgr-trace.jsonl"))
```

## Recording a JVM trace (Java, Scala)

Build the agent once (requires a JDK with the `java.lang.classfile` API,
i.e. JDK 24+; the agent itself has no dependencies):

```bash
make jvm-agent   # produces build/cgr-jvm-agent.jar
```

Attach it to any JVM workload, most usefully a test run:

```bash
java -javaagent:build/cgr-jvm-agent.jar="include=com.example;repo=/path/to/your-repo" ...
# Maven:  MAVEN_OPTS='-javaagent:...' mvn test
# Gradle: add the same -javaagent flag to test { jvmArgs ... }
```

Agent arguments are semicolon-separated `key=value` pairs:

| Argument | Meaning |
|---|---|
| `include=com.example,org.acme` | Package prefixes to instrument (required). Both endpoints of an edge must match; the JDK and third-party code are never instrumented. |
| `output=cgr-trace.jsonl` | Trace file path (written on JVM exit). |
| `repo=/abs/path` | Repository root recorded in the trace header. |
| `workload=label` | Workload label for the run. Tests can refine it per case with `cgr.trace.TraceRecorder.setWorkload(...)`, which labels the calling thread and threads it spawns afterwards, so concurrent runners keep separate provenance. |

The agent instruments method entry and recovers the caller by walking the
stack to the nearest project frame, seeing through JDK internals,
lambda-metafactory classes, and generated proxies. That is deliberate: an
edge like `list.forEach(this::handle)` or a call through a DI proxy is
attributed to the code that initiated it, which is exactly the relationship
static analysis cannot see. Concrete receiver classes are sampled on
virtual and interface calls, so the graph records which implementation
actually handled a dispatch.

## Ingesting a trace

Parse the repository into the graph first (`cgr start --repo-path ... --update-graph`),
then ingest the trace against the same repository:

```bash
cgr trace ingest cgr-trace.jsonl --repo-path /path/to/your-repo
```

The ingest step resolves each recorded frame to the graph's `Function`,
`Method`, or `Module` nodes and writes `CALLS` edges with dynamic-provenance
properties:

| Property | Meaning |
|---|---|
| `dynamic: true` | This edge was observed at runtime. |
| `dynamic_call_count` | Total observed invocations in the trace. |
| `dynamic_workloads` | Test ids that exercised the edge (capped list). |
| `dynamic_workload_count` | Uncapped number of distinct workloads. |
| `dynamic_receiver_types` | Concrete receiver types observed for method calls. |
| `static_missed: true` | No matching static `CALLS` edge existed in the graph at ingest time. Dynamic dispatch, reflection, and registries are the common causes; a stale or incomplete static graph produces the same flag. |

An edge with `dynamic: true` and `static_missed: false` is a static edge
confirmed at runtime. Re-ingesting a trace is idempotent: properties are set,
not accumulated.

The command reports resolution quality: frames outside the repository,
synthetic code objects (lambdas, generator expressions), and names the graph
does not know are counted per reason instead of being silently dropped.

## Caveats

- **Coverage honesty.** The dynamic view only reflects the workload that was
  traced. The absence of a dynamic edge never means dead code; it means the
  traced workload did not exercise that path.
- **Staleness.** Dynamic properties describe the commit that was traced.
  After significant edits, re-run the trace and ingest again; a full graph
  rebuild with `--clean` discards dynamic edges entirely.
- **Threading.** Counts are aggregated without locks; heavily threaded
  workloads may undercount, though edge presence is unaffected.
- **Overhead.** `sys.monitoring` keeps Python tracing cheap enough for test
  suites, but expect measurable slowdown on call-heavy code. Receiver types
  are sampled only for a pair's first few calls to bound the cost.
- **JVM overhead.** The agent walks the stack on every instrumented method
  entry, costing roughly a microsecond per call (measured: 6M instrumented
  calls added ~7s on a JIT-friendly loop that runs in milliseconds
  untraced). Test suites dominated by I/O see far less relative impact, but
  keep `include=` scoped to your own packages and avoid tracing
  compute-heavy inner loops.
- **JVM resolution.** A lambda body has no static node of its own, so its
  frame resolves to the enclosing method by line span. An anonymous-class
  method resolves to its own node — the innermost source span containing the
  frame line, threaded under the enclosing method — rather than to the
  enclosing method itself. Frames the static graph cannot account for
  (implicit constructors, static initializers) are counted as unresolved
  rather than guessed. Scala name mangling (`Util$`, `$anonfun$`) is
  normalized, but Scala static parsing is still in development, so expect
  lower resolution rates than Java.
