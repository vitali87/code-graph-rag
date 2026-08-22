# ArcadeDB Graph Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ArcadeDB as a selectable graph-storage backend alongside Memgraph, with Memgraph remaining the default and both backends verified by one shared conformance suite.

**Architecture:** Extract the storage layer behind a `GraphIngestor` protocol (which extends the `IngestorProtocol` and `QueryProtocol` that already exist in `codebase_rag/services/__init__.py`) plus a six-member `GraphDialect` holding only what genuinely differs between engines. `MemgraphIngestor` moves under a new `services/graph/` package and keeps working unchanged; `ArcadeDBIngestor` is added beside it, speaking Cypher over Bolt via the `neo4j` driver and SQL over HTTP for schema DDL.

**Tech Stack:** Python 3.12, pydantic-settings, pytest + pytest-xdist (`--dist=loadgroup`), testcontainers, `pymgclient` (Memgraph, hard dep), `neo4j>=5.28,<6` (ArcadeDB, optional extra), Docker Compose profiles.

**Spec:** `docs/superpowers/specs/2026-08-22-arcadedb-graph-backend-design.md`

## Global Constraints

- **Memgraph stays the default.** With `GRAPH_BACKEND` unset, behaviour must be byte-identical to today. Every phase-1 and phase-2 task is a pure refactor: the full existing suite must stay green with no test assertion changed except `patch()` target paths.
- **`MemgraphIngestor` is public API.** `cgr/__init__.py` lists it in `__all__`. `codebase_rag/services/graph_service.py` must keep exporting the name forever.
- **Reuse the existing protocols.** `codebase_rag/services/__init__.py` already defines `IngestorProtocol` (`ensure_node_batch`, `ensure_relationship_batch`, `flush_all`) and `QueryProtocol` (`fetch_all`, `execute_write`), both `@runtime_checkable`, both consumed by `FilteringIngestor` and `resource_cleanup.py`. Do NOT define a parallel `GraphWriter`.
- **Commit messages are single-line.** The `single-line-commit` pre-commit hook rejects anything else, including trailers. Conventional Commit prefix required (`feat:`, `refactor:`, `test:`, `docs:`, `chore:`).
- **Never rename the `MEMGRAPH_*` settings.** Six settings (`MEMGRAPH_HOST`, `MEMGRAPH_PORT`, `MEMGRAPH_HTTP_PORT`, `MEMGRAPH_USERNAME`, `MEMGRAPH_PASSWORD`, `MEMGRAPH_BATCH_SIZE`) are user-facing `.env` keys.
- **No literal strings in code.** This codebase keeps every user-visible string in `codebase_rag/constants/`, `codebase_rag/logs.py`, or `codebase_rag/exceptions.py`. Follow it.
- **Version floors:** `neo4j>=5.28,<6` — the 6.x driver line postdates ArcadeDB's published Bolt certification matrix (protocol 3.0/4.0/4.4/5.0-5.4), so stay inside it. Widen only with conformance evidence. ArcadeDB server `>=26.2.1` (first release with the Bolt plugin); verified against `26.8.1`.
- **Default ports:** ArcadeDB Bolt `7687`, ArcadeDB HTTP `2480`, Memgraph Bolt `7687`. Memgraph and ArcadeDB collide on 7687 and must never run unprofiled together.
- **Run before every commit:** `make lint && make typecheck && $(uv run) pytest -n auto -m "not integration"`.

---

## File Structure

**New package** `codebase_rag/services/graph/`:

| File | Responsibility |
|---|---|
| `__init__.py` | Re-exports `GraphIngestor`, `GraphDialect`, `get_ingestor`, `get_dialect` |
| `protocol.py` | `GraphIngestor(IngestorProtocol, QueryProtocol, Protocol)` — lifecycle + admin surface |
| `dialect.py` | `GraphDialect(Protocol)` — the six diverging members |
| `memgraph.py` | `MemgraphIngestor` (moved verbatim) + `MemgraphDialect` |
| `arcadedb.py` | `ArcadeDBIngestor` + `ArcadeDBDialect` |
| `arcade_http.py` | `ArcadeHttpClient` — SQL over HTTP, DDL only |
| `retry.py` | `retry_on_transient()` — shared, dialect-driven |
| `factory.py` | `get_ingestor()` / `get_dialect()` reading `GRAPH_BACKEND` |

**Modified:**

| File | Change |
|---|---|
| `codebase_rag/services/graph_service.py` | Becomes a re-export shim |
| `codebase_rag/constants/providers.py` | Add `GraphBackend` StrEnum, `MODULE_NEO4J` |
| `codebase_rag/constants/graph.py` | Add ArcadeDB DDL templates |
| `codebase_rag/config.py` | Add `GRAPH_BACKEND` + six `ARCADEDB_*` settings |
| `codebase_rag/utils/dependencies.py` | Add `has_neo4j_driver()` |
| `codebase_rag/prompts.py` | Take `procedure_catalog` from the dialect; neutralise "Memgraph" wording |
| `codebase_rag/services/llm.py` | Take `allowed_proc_prefixes` from the dialect |
| `codebase_rag/stack/` | `constants.py`, `health.py`, `manager.py`, `cli.py` become backend-aware |
| `codebase_rag/tools/health_checker.py` | `check_memgraph_connection` → `check_graph_connection` |
| `codebase_rag/constants/health.py` | Neutralise `HEALTH_CHECK_MEMGRAPH_*` names |
| `codebase_rag/docker-compose.yaml` | Compose profiles + `arcadedb` service |
| `pyproject.toml` | `arcadedb` extra |
| `codebase_rag/tests/integration/conftest.py` | Backend-parametrised fixtures |

**New tests:**

| File | Responsibility |
|---|---|
| `codebase_rag/tests/test_graph_factory.py` | Backend selection, missing-dependency error |
| `codebase_rag/tests/test_arcadedb_dialect.py` | DDL generation, retry classification — no server |
| `codebase_rag/tests/test_arcade_http.py` | HTTP client request shape — no server |
| `codebase_rag/tests/integration/test_graph_backend_conformance.py` | The cross-backend contract |
| `codebase_rag/tests/integration/test_query_corpus.py` | Every shared query parses on both backends |

---

## Phase 1 — Extract the seam (no ArcadeDB code)

### Task 1: Move MemgraphIngestor into services/graph/ behind a shim

**Files:**
- Create: `codebase_rag/services/graph/__init__.py`
- Create: `codebase_rag/services/graph/protocol.py`
- Create: `codebase_rag/services/graph/memgraph.py` (moved from `graph_service.py`)
- Modify: `codebase_rag/services/graph_service.py` (becomes a shim)
- Modify: `codebase_rag/tests/test_graph_service.py:107,120,134` (patch targets)
- Modify: `codebase_rag/tests/test_memgraph_batching.py`, `codebase_rag/tests/test_health_checker.py` (patch targets)

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `codebase_rag.services.graph.protocol.GraphIngestor`, `codebase_rag.services.graph.memgraph.MemgraphIngestor`. `codebase_rag.services.graph_service.MemgraphIngestor` continues to resolve.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/test_graph_protocol.py`:

```python
from codebase_rag.services.graph import GraphIngestor
from codebase_rag.services.graph.memgraph import MemgraphIngestor


def test_memgraph_ingestor_satisfies_graph_ingestor_protocol() -> None:
    assert isinstance(MemgraphIngestor(host="h", port=1), GraphIngestor)


def test_shim_still_exports_memgraph_ingestor() -> None:
    from codebase_rag.services.graph_service import MemgraphIngestor as Shimmed

    assert Shimmed is MemgraphIngestor


def test_cgr_public_api_still_exports_memgraph_ingestor() -> None:
    import cgr

    assert "MemgraphIngestor" in cgr.__all__
    assert cgr.MemgraphIngestor is MemgraphIngestor
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_graph_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codebase_rag.services.graph'`

- [ ] **Step 3: Move the module with git mv (preserves history)**

```bash
mkdir -p codebase_rag/services/graph
git mv codebase_rag/services/graph_service.py codebase_rag/services/graph/memgraph.py
```

Then fix the relative-import depth in `memgraph.py` — it moved one level deeper, so every `..` becomes `...`:

```bash
python3 - <<'PY'
import pathlib
p = pathlib.Path("codebase_rag/services/graph/memgraph.py")
s = p.read_text()
s = s.replace("from .. import exceptions as ex", "from ... import exceptions as ex")
s = s.replace("from .. import logs as ls", "from ... import logs as ls")
s = s.replace("from ..constants import", "from ...constants import")
s = s.replace("from ..cypher_queries import", "from ...cypher_queries import")
s = s.replace("from ..types_defs import", "from ...types_defs import")
s = s.replace("from ..utils.path_utils import", "from ...utils.path_utils import")
s = s.replace("from .resource_cleanup import", "from ..resource_cleanup import")
p.write_text(s)
PY
```

- [ ] **Step 4: Write the protocol**

Create `codebase_rag/services/graph/protocol.py`:

```python
from __future__ import annotations

import types
from typing import Protocol, runtime_checkable

from ...types_defs import GraphData, PropertyValue
from .. import IngestorProtocol, QueryProtocol


@runtime_checkable
class GraphIngestor(IngestorProtocol, QueryProtocol, Protocol):
    """The full storage surface. Extends the node/relationship sink
    (IngestorProtocol) and the read/write query surface (QueryProtocol)
    that already existed, adding lifecycle and admin operations."""

    batch_size: int

    def __enter__(self) -> GraphIngestor: ...

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None: ...

    async def __aenter__(self) -> GraphIngestor: ...

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None: ...

    def ensure_constraints(self) -> None: ...

    def flush_nodes(self) -> None: ...

    def flush_relationships(self) -> None: ...

    def clean_database(self) -> None: ...

    def list_projects(self) -> list[str]: ...

    def list_project_roots(self) -> dict[str, str | None]: ...

    def delete_project(self, project_name: str) -> None: ...

    def export_graph_to_dict(self) -> GraphData: ...
```

Create `codebase_rag/services/graph/__init__.py`:

```python
from .memgraph import MemgraphIngestor
from .protocol import GraphIngestor

__all__ = ["GraphIngestor", "MemgraphIngestor"]
```

- [ ] **Step 5: Write the shim**

Create `codebase_rag/services/graph_service.py`:

```python
"""Backwards-compatible re-export.

`MemgraphIngestor` is public API via `cgr.__all__`, so this import path
must keep resolving even though the implementation moved to
`codebase_rag.services.graph.memgraph`.
"""

from .graph.memgraph import MemgraphIngestor

__all__ = ["MemgraphIngestor"]
```

- [ ] **Step 6: Repoint the mock patch targets**

`patch()` resolves against the module where the name is looked up, so the shim does not help these. Three call sites in `test_graph_service.py` plus the other two files:

```bash
python3 - <<'PY'
import pathlib
for name in (
    "codebase_rag/tests/test_graph_service.py",
    "codebase_rag/tests/test_memgraph_batching.py",
    "codebase_rag/tests/test_health_checker.py",
):
    p = pathlib.Path(name)
    s = p.read_text()
    s = s.replace(
        "codebase_rag.services.graph_service.mgclient",
        "codebase_rag.services.graph.memgraph.mgclient",
    )
    p.write_text(s)
PY
grep -rn "services.graph_service.mgclient" codebase_rag/tests/ || echo "all repointed"
```

- [ ] **Step 6b: Lock the evals stand-in to the protocol**

`evals/cgr_graph.py`'s `_CapturingIngestor` already duck-types the sink surface. Pin that, so a protocol change cannot silently desync the eval harness from the real ingestors. Append to `codebase_rag/tests/test_graph_protocol.py`:

```python
def test_eval_capturing_ingestor_satisfies_the_sink_protocol() -> None:
    from codebase_rag.services import IngestorProtocol
    from evals.cgr_graph import _CapturingIngestor

    assert isinstance(_CapturingIngestor(), IngestorProtocol)
```

- [ ] **Step 7: Run the full unit suite**

Run: `uv run pytest -n auto -m "not integration"`
Expected: PASS. Any failure here means the move was not faithful — fix before continuing, do not adjust assertions.

- [ ] **Step 8: Typecheck and lint**

Run: `make lint && make typecheck`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add codebase_rag/services/graph/ codebase_rag/services/graph_service.py codebase_rag/tests/
git commit -m "refactor: move MemgraphIngestor into services/graph behind a shim"
```

---

### Task 2: Add GraphDialect and route Memgraph behaviour through it

**Files:**
- Create: `codebase_rag/services/graph/dialect.py`
- Create: `codebase_rag/services/graph/retry.py`
- Modify: `codebase_rag/services/graph/memgraph.py` (add `MemgraphDialect`, use it)
- Test: `codebase_rag/tests/test_graph_dialect.py`

**Interfaces:**
- Consumes: `GraphIngestor` from Task 1
- Produces: `GraphDialect` protocol with members `name`, `ensure_schema(ingestor)`, `apply_query_limit(query) -> str`, `procedure_catalog -> str`, `allowed_proc_prefixes -> frozenset[str]`, `is_benign_error(exc) -> bool`, `is_retryable(exc) -> bool`; `MemgraphDialect` implementing it; `retry_on_transient(fn, dialect, attempts, base_delay)`.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/test_graph_dialect.py`:

```python
import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph.dialect import GraphDialect
from codebase_rag.services.graph.memgraph import MemgraphDialect
from codebase_rag.services.graph.retry import retry_on_transient


def test_memgraph_dialect_satisfies_protocol() -> None:
    assert isinstance(MemgraphDialect(), GraphDialect)


def test_memgraph_dialect_name() -> None:
    assert MemgraphDialect().name == GraphBackend.MEMGRAPH


def test_apply_query_limit_appends_suffix() -> None:
    out = MemgraphDialect().apply_query_limit("MATCH (n) RETURN n", 512)
    assert out == "MATCH (n) RETURN n QUERY MEMORY LIMIT 512 MB;"


def test_apply_query_limit_is_idempotent() -> None:
    once = MemgraphDialect().apply_query_limit("MATCH (n) RETURN n", 512)
    assert MemgraphDialect().apply_query_limit(once, 512) == once


def test_apply_query_limit_strips_trailing_semicolon_first() -> None:
    out = MemgraphDialect().apply_query_limit("MATCH (n) RETURN n;", 64)
    assert out == "MATCH (n) RETURN n QUERY MEMORY LIMIT 64 MB;"


def test_memgraph_is_retryable_is_always_false() -> None:
    # The retry loop must stay inert on the default backend.
    assert MemgraphDialect().is_retryable(RuntimeError("anything")) is False


def test_memgraph_is_benign_error_matches_already_exists() -> None:
    d = MemgraphDialect()
    assert d.is_benign_error(RuntimeError("Index already exists")) is True
    assert d.is_benign_error(RuntimeError("syntax error")) is False


def test_retry_on_transient_does_not_retry_when_dialect_says_no() -> None:
    calls = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        retry_on_transient(boom, MemgraphDialect(), attempts=3, base_delay=0.0)
    assert len(calls) == 1


def test_retry_on_transient_retries_then_succeeds() -> None:
    class AlwaysRetryDialect(MemgraphDialect):
        def is_retryable(self, exc: Exception) -> bool:
            return True

    calls = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("conflict")
        return "ok"

    assert retry_on_transient(
        flaky, AlwaysRetryDialect(), attempts=5, base_delay=0.0
    ) == "ok"
    assert len(calls) == 3


def test_retry_on_transient_reraises_after_exhaustion() -> None:
    class AlwaysRetryDialect(MemgraphDialect):
        def is_retryable(self, exc: Exception) -> bool:
            return True

    def always_boom() -> None:
        raise RuntimeError("conflict")

    with pytest.raises(RuntimeError, match="conflict"):
        retry_on_transient(
            always_boom, AlwaysRetryDialect(), attempts=3, base_delay=0.0
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_graph_dialect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codebase_rag.services.graph.dialect'`

- [ ] **Step 3: Add the GraphBackend enum**

Append to `codebase_rag/constants/providers.py`, directly after `VectorStoreBackend`:

```python
class GraphBackend(StrEnum):
    MEMGRAPH = "memgraph"
    ARCADEDB = "arcadedb"


MODULE_NEO4J = "neo4j"
```

Export it from `codebase_rag/constants/__init__.py` alongside `VectorStoreBackend` (follow the existing re-export style in that file).

- [ ] **Step 4: Write the dialect protocol**

Create `codebase_rag/services/graph/dialect.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ...constants import GraphBackend

if TYPE_CHECKING:
    from .protocol import GraphIngestor


@runtime_checkable
class GraphDialect(Protocol):
    """Everything that genuinely differs between graph engines.

    Deliberately small: the shared Cypher in cypher_queries.py runs on both
    backends unchanged, so only these five behaviours plus the name vary.
    """

    @property
    def name(self) -> GraphBackend: ...

    def ensure_schema(self, ingestor: GraphIngestor) -> None:
        """Create whatever the engine needs before MERGE is efficient:
        unique constraints and indexes on Memgraph; vertex/edge types,
        property declarations and unique indexes on ArcadeDB."""
        ...

    def apply_query_limit(self, query: str, mb: int) -> str:
        """Bound a read query's resource use, or return it unchanged when
        the engine has no equivalent."""
        ...

    @property
    def procedure_catalog(self) -> str:
        """The prompt fragment listing callable graph-algorithm procedures."""
        ...

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        """Procedure namespaces the read-only guard permits."""
        ...

    def is_benign_error(self, exc: Exception) -> bool:
        """True when the error means 'already done' and should not be logged."""
        ...

    def is_retryable(self, exc: Exception) -> bool:
        """True for transient write conflicts worth retrying."""
        ...
```

- [ ] **Step 5: Write the retry helper**

Create `codebase_rag/services/graph/retry.py`:

```python
from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from loguru import logger

from ... import logs as ls

if TYPE_CHECKING:
    from .dialect import GraphDialect

T = TypeVar("T")

DEFAULT_ATTEMPTS = 5
DEFAULT_BASE_DELAY_S = 0.05


def retry_on_transient(
    fn: Callable[[], T],
    dialect: GraphDialect,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_S,
) -> T:
    """Run `fn`, retrying only errors the dialect calls transient.

    Memgraph's dialect never does, so this is a straight pass-through on
    the default backend. ArcadeDB is MVCC/optimistic and raises on
    concurrent updates to the same vertex, which parallel flush provokes
    whenever many edges converge on one hot node.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not dialect.is_retryable(exc):
                raise
            last = exc
            if attempt == attempts - 1:
                break
            delay = base_delay * (2**attempt) * (0.5 + random.random())  # noqa: S311
            logger.debug(
                ls.GRAPH_RETRY_TRANSIENT.format(
                    attempt=attempt + 1, attempts=attempts, delay=delay, error=exc
                )
            )
            time.sleep(delay)
    assert last is not None
    raise last
```

Add to `codebase_rag/logs.py`:

```python
GRAPH_RETRY_TRANSIENT = (
    "Transient write conflict (attempt {attempt}/{attempts}), "
    "retrying in {delay:.3f}s: {error}"
)
```

- [ ] **Step 6: Write MemgraphDialect and use it from MemgraphIngestor**

Append to `codebase_rag/services/graph/memgraph.py`:

```python
class MemgraphDialect:
    __slots__ = ()

    @property
    def name(self) -> GraphBackend:
        return GraphBackend.MEMGRAPH

    def ensure_schema(self, ingestor: GraphIngestor) -> None:
        for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
            try:
                ingestor.execute_write(build_constraint_query(label, prop))
            except Exception:
                pass
        for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
            try:
                ingestor.execute_write(build_index_query(label, prop))
            except Exception:
                pass

    def apply_query_limit(self, query: str, mb: int) -> str:
        if CYPHER_MEMORY_LIMIT_TOKEN in query.upper():
            return query
        stripped = query.rstrip()
        if stripped.endswith(CYPHER_SEMICOLON):
            stripped = stripped[: -len(CYPHER_SEMICOLON)].rstrip()
        suffix = CYPHER_MEMORY_LIMIT_SUFFIX.format(mb=mb)
        return f"{stripped}{suffix}{CYPHER_SEMICOLON}"

    @property
    def procedure_catalog(self) -> str:
        return MAGE_PROCEDURE_CATALOG

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        return cs.CYPHER_ALLOWED_PROCEDURE_PREFIXES

    def is_benign_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return ERR_SUBSTR_ALREADY_EXISTS in text or ERR_SUBSTR_CONSTRAINT in text

    def is_retryable(self, exc: Exception) -> bool:
        # Memgraph's storage engine does not surface the optimistic-write
        # conflicts ArcadeDB does. Keeping this False makes the shared retry
        # loop inert on the default backend, so behaviour is unchanged.
        return False
```

`MemgraphIngestor.ensure_constraints` must keep calling `_migrate_legacy_path_keys()` BEFORE delegating, or the issue-#897 migration silently stops running:

```python
    def ensure_constraints(self) -> None:
        logger.info(ls.MG_ENSURING_CONSTRAINTS)
        self._migrate_legacy_path_keys()
        self._dialect.ensure_schema(self)
        logger.info(ls.MG_CONSTRAINTS_DONE)
```

`_migrate_legacy_path_keys` stays on `MemgraphIngestor`, not the dialect: it is a one-off data repair for databases written by the superseded key, not a schema concern, and ArcadeDB has no equivalent history.

Replace the module-level `_apply_memory_limit` function with a delegation. In `MemgraphIngestor.__init__`, add `self._dialect = MemgraphDialect()` (and `"_dialect"` to `__slots__`). Change `fetch_all` to:

```python
    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        bounded_query = self._dialect.apply_query_limit(
            query, settings.QUERY_MEMORY_LIMIT_MB
        )
        logger.debug(ls.MG_FETCH_QUERY, query=bounded_query, params=params)
        return self._execute_query(bounded_query, params)
```

`MAGE_PROCEDURE_CATALOG` is the existing section-2b text lifted verbatim out of `prompts.py` — move it to `codebase_rag/constants/graph.py` as a module constant in this step, and have `prompts.py` import it. Do not reword it; that is Task 4's job.

- [ ] **Step 7: Run tests**

Run: `uv run pytest codebase_rag/tests/test_graph_dialect.py -v && uv run pytest -n auto -m "not integration"`
Expected: PASS, including every pre-existing test.

- [ ] **Step 8: Commit**

```bash
git add codebase_rag/services/graph/ codebase_rag/constants/ codebase_rag/logs.py codebase_rag/prompts.py codebase_rag/tests/test_graph_dialect.py
git commit -m "refactor: add GraphDialect and route Memgraph behaviour through it"
```

---

### Task 3: Add backend selection, settings, and the factory

**Files:**
- Create: `codebase_rag/services/graph/factory.py`
- Modify: `codebase_rag/config.py:157-165` (settings block)
- Modify: `codebase_rag/utils/dependencies.py`
- Modify: `codebase_rag/exceptions.py`
- Modify: `codebase_rag/services/graph/__init__.py`
- Test: `codebase_rag/tests/test_graph_factory.py`

**Interfaces:**
- Consumes: `GraphBackend` (Task 2), `MemgraphIngestor`/`MemgraphDialect` (Tasks 1-2)
- Produces: `get_dialect(backend: GraphBackend | None = None) -> GraphDialect`, `get_ingestor(**overrides) -> GraphIngestor`, `has_neo4j_driver() -> bool`, `settings.GRAPH_BACKEND`, six `ARCADEDB_*` settings.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/test_graph_factory.py`:

```python
import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.exceptions import GraphBackendUnavailableError
from codebase_rag.services.graph.factory import get_dialect, get_ingestor
from codebase_rag.services.graph.memgraph import MemgraphDialect, MemgraphIngestor


def test_default_backend_is_memgraph(monkeypatch: pytest.MonkeyPatch) -> None:
    from codebase_rag.config import settings

    assert settings.GRAPH_BACKEND == GraphBackend.MEMGRAPH


def test_get_dialect_returns_memgraph_by_default() -> None:
    assert isinstance(get_dialect(), MemgraphDialect)


def test_get_dialect_honours_explicit_backend() -> None:
    from codebase_rag.services.graph.arcadedb import ArcadeDBDialect

    assert isinstance(get_dialect(GraphBackend.ARCADEDB), ArcadeDBDialect)


def test_get_ingestor_returns_memgraph_ingestor() -> None:
    ingestor = get_ingestor()
    assert isinstance(ingestor, MemgraphIngestor)


def test_get_ingestor_raises_when_driver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A missing graph backend is fatal, unlike a missing vector store which
    # degrades to no semantic search.
    monkeypatch.setattr(
        "codebase_rag.services.graph.factory.has_neo4j_driver", lambda: False
    )
    with pytest.raises(GraphBackendUnavailableError, match=r"code-graph-rag\[arcadedb\]"):
        get_ingestor(backend=GraphBackend.ARCADEDB)


def test_arcadedb_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # ArcadeDB's Bolt listener rejects the `none` auth scheme outright.
    monkeypatch.setattr(
        "codebase_rag.services.graph.factory.has_neo4j_driver", lambda: True
    )
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "ARCADEDB_USERNAME", None)
    monkeypatch.setattr(settings, "ARCADEDB_PASSWORD", None)
    with pytest.raises(ValueError, match="credentials"):
        get_ingestor(backend=GraphBackend.ARCADEDB)
```

Note: two of these reference `ArcadeDBDialect`, which arrives in Task 10. Mark them `@pytest.mark.xfail(reason="ArcadeDBDialect lands in Task 10", strict=False)` now and remove the marker in Task 10.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_graph_factory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codebase_rag.services.graph.factory'`

- [ ] **Step 3: Add the settings**

In `codebase_rag/config.py`, immediately after the `MEMGRAPH_BATCH_SIZE` line in `AppConfig`:

```python
    # Graph backend selection. MEMGRAPH_* above stay authoritative for the
    # default backend; renaming them would break every existing .env.
    GRAPH_BACKEND: cs.GraphBackend = cs.GraphBackend.MEMGRAPH
    ARCADEDB_HOST: str = "localhost"
    ARCADEDB_BOLT_PORT: int = 7687
    ARCADEDB_HTTP_PORT: int = 2480
    ARCADEDB_USERNAME: str | None = None
    ARCADEDB_PASSWORD: str | None = None
    # ArcadeDB is multi-database; Memgraph is not. Required when selected.
    ARCADEDB_DATABASE: str = "codegraph"
```

- [ ] **Step 4: Add the dependency probe**

In `codebase_rag/utils/dependencies.py`, add `MODULE_NEO4J` to the existing `from codebase_rag.constants import (MODULE_AST_GREP, MODULE_PYMILVUS, MODULE_QDRANT_CLIENT, MODULE_TORCH, MODULE_TRANSFORMERS, UNIXCODER_MODEL, EmbeddingProvider, VectorStoreBackend)` block, keeping alphabetical order, and append:

```python
def has_neo4j_driver() -> bool:
    return _check_dependency(MODULE_NEO4J)
```

- [ ] **Step 5: Add the exception**

In `codebase_rag/exceptions.py`:

```python
class GraphBackendUnavailableError(RuntimeError):
    """The selected graph backend's driver is not installed."""


GRAPH_BACKEND_UNAVAILABLE = (
    "GRAPH_BACKEND is '{backend}' but its driver is not installed. "
    "Install it with: pip install 'code-graph-rag[{extra}]'"
)
GRAPH_BACKEND_AUTH_REQUIRED = (
    "Backend '{backend}' requires credentials: set ARCADEDB_USERNAME and "
    "ARCADEDB_PASSWORD. Its Bolt listener rejects unauthenticated connections."
)
```

- [ ] **Step 6: Write the factory**

Create `codebase_rag/services/graph/factory.py`:

```python
from __future__ import annotations

from ...config import settings
from ...constants import GraphBackend
from ... import exceptions as ex
from ...utils.dependencies import has_neo4j_driver
from .dialect import GraphDialect
from .memgraph import MemgraphDialect, MemgraphIngestor
from .protocol import GraphIngestor

_ARCADEDB_EXTRA = "arcadedb"


def get_dialect(backend: GraphBackend | None = None) -> GraphDialect:
    resolved = backend or settings.GRAPH_BACKEND
    if resolved == GraphBackend.ARCADEDB:
        from .arcadedb import ArcadeDBDialect

        return ArcadeDBDialect()
    return MemgraphDialect()


def get_ingestor(
    backend: GraphBackend | None = None,
    batch_size: int | None = None,
) -> GraphIngestor:
    """Build the configured ingestor.

    Unlike `_get_vector_store()`, which returns None and warns when its
    dependency is missing, a missing graph backend is not degradable — it
    is the whole product — so this raises instead.
    """
    resolved = backend or settings.GRAPH_BACKEND
    size = settings.MEMGRAPH_BATCH_SIZE if batch_size is None else batch_size

    if resolved == GraphBackend.ARCADEDB:
        if not has_neo4j_driver():
            raise ex.GraphBackendUnavailableError(
                ex.GRAPH_BACKEND_UNAVAILABLE.format(
                    backend=resolved, extra=_ARCADEDB_EXTRA
                )
            )
        if not settings.ARCADEDB_USERNAME or not settings.ARCADEDB_PASSWORD:
            raise ValueError(
                ex.GRAPH_BACKEND_AUTH_REQUIRED.format(backend=resolved)
            )
        from .arcadedb import ArcadeDBIngestor

        return ArcadeDBIngestor(
            host=settings.ARCADEDB_HOST,
            bolt_port=settings.ARCADEDB_BOLT_PORT,
            http_port=settings.ARCADEDB_HTTP_PORT,
            database=settings.ARCADEDB_DATABASE,
            username=settings.ARCADEDB_USERNAME,
            password=settings.ARCADEDB_PASSWORD,
            batch_size=size,
        )

    return MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=size,
        username=settings.MEMGRAPH_USERNAME,
        password=settings.MEMGRAPH_PASSWORD,
    )
```

Update `codebase_rag/services/graph/__init__.py`:

```python
from .dialect import GraphDialect
from .factory import get_dialect, get_ingestor
from .memgraph import MemgraphDialect, MemgraphIngestor
from .protocol import GraphIngestor

__all__ = [
    "GraphDialect",
    "GraphIngestor",
    "MemgraphDialect",
    "MemgraphIngestor",
    "get_dialect",
    "get_ingestor",
]
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest codebase_rag/tests/test_graph_factory.py -v && uv run pytest -n auto -m "not integration"`
Expected: PASS, with the two ArcadeDB tests xfailing.

- [ ] **Step 8: Commit**

```bash
git add codebase_rag/services/graph/ codebase_rag/config.py codebase_rag/utils/dependencies.py codebase_rag/exceptions.py codebase_rag/tests/test_graph_factory.py
git commit -m "feat: add GRAPH_BACKEND selection and the graph ingestor factory"
```

---

### Task 4: Route the prompt catalog and procedure allowlist through the dialect

**Files:**
- Modify: `codebase_rag/prompts.py:44-80` (section 2b), `prompts.py:88` and `prompts.py:~100` (Memgraph wording)
- Modify: `codebase_rag/services/llm.py:99-108` (`_validate_call_procedures`)
- Test: `codebase_rag/tests/test_prompt_dialect.py`

**Interfaces:**
- Consumes: `get_dialect()` (Task 3), `GraphDialect.procedure_catalog` / `.allowed_proc_prefixes` (Task 2)
- Produces: `build_graph_schema_and_rules(dialect)` and `build_cypher_system_prompt(active_projects, dialect=None)` taking an optional dialect; `_validate_call_procedures(query, dialect=None)`.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/test_prompt_dialect.py`:

```python
import pytest

from codebase_rag.exceptions import LLMGenerationError
from codebase_rag.prompts import build_graph_schema_and_rules
from codebase_rag.services.graph.memgraph import MemgraphDialect
from codebase_rag.services.llm import _validate_call_procedures


class _FakeDialect(MemgraphDialect):
    @property
    def procedure_catalog(self) -> str:
        return "- **PageRank**: `CALL algo.pageRank() YIELD node, score`"

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        return frozenset({"algo."})


def test_prompt_embeds_the_dialect_catalog() -> None:
    out = build_graph_schema_and_rules(_FakeDialect())
    assert "CALL algo.pageRank() YIELD node, score" in out
    assert "nxalg." not in out


def test_prompt_does_not_name_a_specific_engine() -> None:
    out = build_graph_schema_and_rules(MemgraphDialect())
    assert "Memgraph" not in out


def test_validate_call_procedures_allows_dialect_prefix() -> None:
    _validate_call_procedures(
        "MATCH (n) CALL algo.pageRank() YIELD node RETURN node", _FakeDialect()
    )


def test_validate_call_procedures_rejects_outside_dialect_prefix() -> None:
    with pytest.raises(LLMGenerationError, match="pagerank.get"):
        _validate_call_procedures(
            "MATCH (n) CALL pagerank.get() YIELD node RETURN node", _FakeDialect()
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_prompt_dialect.py -v`
Expected: FAIL — `build_graph_schema_and_rules()` currently takes no arguments.

- [ ] **Step 3: Thread the dialect through prompts.py**

In `codebase_rag/prompts.py`, split the existing `CYPHER_QUERY_RULES` constant into two module-private strings at the section boundaries, changing no wording except where noted:

- `_CYPHER_RULES_BODY` — everything from the first bullet (`**ALWAYS Return Specific Properties...**`) down to and including the `**NEVER use unbounded variable-length paths**` bullet. Change only its final clause `use a MAGE procedure (see Section 2b)` to `use a graph-algorithm procedure (see Section 2b)`.
- `_CYPHER_RULES_FALLBACK` — the whole `**2c. When Cypher Can't Answer**` section, with its two MAGE-specific examples replaced by dialect-neutral wording: `"longest call chain" -> call a strongly-connected-components procedure from Section 2b and post-process the result` and `"find a deeply-nested call site" -> use a bounded depth such as [:CALLS*1..10] with ORDER BY ... LIMIT 1`.

Section 2b between them is what the dialect now supplies. Then make the two entry points accept an optional dialect defaulting to `get_dialect()`:

```python
def build_cypher_query_rules(procedure_catalog: str) -> str:
    return f"""**2. Critical Cypher Query Rules**
{_CYPHER_RULES_BODY}

**2b. Graph Algorithm Procedures**

For algorithmic questions (longest/shortest paths, cycles, recursion clusters,
centrality, communities, reachability), prefer calling a procedure over writing
variable-length Cypher. Cypher path patterns enumerate all matches with no
memoization, so they OOM on cyclic graphs; these procedures run real graph
algorithms in bounded memory.

Call them with `CALL <procedure>(...) YIELD ... RETURN ...`:

{procedure_catalog}

{_CYPHER_RULES_FALLBACK}"""


def build_graph_schema_and_rules(dialect: GraphDialect | None = None) -> str:
    resolved = dialect or get_dialect()
    return f"""You are an expert AI assistant for analyzing codebases using a **hybrid retrieval system**: a **knowledge graph** for structural queries and a **semantic code search engine** for intent-based discovery.

**1. Graph Schema Definition**
The database contains information about a codebase, structured with the following nodes and relationships.

{GRAPH_SCHEMA_DEFINITION}

{build_cypher_query_rules(resolved.procedure_catalog)}
"""
```

Delete the module-level `GRAPH_SCHEMA_AND_RULES = build_graph_schema_and_rules()` constant and have every reader call the function; a module-level constant would freeze one backend's catalog at import time.

Neutralise the two engine mentions in `_format_active_projects_block`: `"This Memgraph database may contain multiple indexed projects"` becomes `"This knowledge graph may contain multiple indexed projects"`.

- [ ] **Step 4: Thread the dialect through llm.py**

```python
def _validate_call_procedures(query: str, dialect: GraphDialect | None = None) -> None:
    resolved = dialect or get_dialect()
    for match in _PROCEDURE_CALL_PATTERN.finditer(query):
        name = match.group(1)
        if not any(name.startswith(p) for p in resolved.allowed_proc_prefixes):
            raise ex.LLMGenerationError(
                ex.LLM_DISALLOWED_PROCEDURE.format(name=name, query=query)
            )
```

In `CypherGenerator.__init__`, store `self._dialect = get_dialect()` (add to `__slots__`) and pass it at the `generate()` call site: `_validate_call_procedures(query, self._dialect)`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest codebase_rag/tests/test_prompt_dialect.py -v && uv run pytest -n auto -m "not integration"`
Expected: PASS. If a pre-existing prompt test asserts the literal string "Memgraph", update that assertion — the wording change is intentional and covered by `test_prompt_does_not_name_a_specific_engine`.

- [ ] **Step 6: Commit**

```bash
git add codebase_rag/prompts.py codebase_rag/services/llm.py codebase_rag/tests/test_prompt_dialect.py
git commit -m "refactor: source the prompt procedure catalog from the graph dialect"
```

---
## Phase 2 — Conformance suite and corpus gate (still no ArcadeDB)

### Task 5: Make the integration fixtures backend-parametrised

**Files:**
- Modify: `codebase_rag/tests/integration/conftest.py`
- Modify: 30 files under `codebase_rag/tests/integration/` using the `memgraph_ingestor` fixture
- Delete: the dead `memgraph_connection` fixture in `codebase_rag/tests/integration/conftest.py` (no test requests it — see the correction note in Task 16)

**Interfaces:**
- Consumes: `get_ingestor` (Task 3), `GraphBackend` (Task 2)
- Produces: session fixture `graph_container(request) -> GraphContainer`, function fixture `graph_ingestor -> GraphIngestor`. `GraphContainer` is a `TypedDict` with keys `backend: GraphBackend`, `host: str`, `bolt_port: int`, `http_port: int | None`, `username: str | None`, `password: str | None`.

At this task the backend parameter list contains only `GraphBackend.MEMGRAPH`. Task 14 adds ArcadeDB to the list, and every test written against `graph_ingestor` then runs on both for free.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/integration/test_fixture_contract.py`:

```python
from __future__ import annotations

import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph import GraphIngestor

pytestmark = [pytest.mark.integration]


def test_graph_ingestor_fixture_satisfies_protocol(
    graph_ingestor: GraphIngestor,
) -> None:
    assert isinstance(graph_ingestor, GraphIngestor)


def test_graph_container_reports_its_backend(
    graph_container: dict[str, object],
) -> None:
    assert graph_container["backend"] in set(GraphBackend)


def test_fixture_starts_empty(graph_ingestor: GraphIngestor) -> None:
    rows = graph_ingestor.fetch_all("MATCH (n) RETURN count(n) AS c")
    assert rows[0]["c"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/integration/test_fixture_contract.py -v`
Expected: FAIL with `fixture 'graph_ingestor' not found`

- [ ] **Step 3: Rewrite conftest.py with parametrised fixtures**

Replace `codebase_rag/tests/integration/conftest.py` with:

```python
from __future__ import annotations

import socket
import time
from collections.abc import Generator
from pathlib import Path
from typing import TypedDict

import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph import GraphIngestor
from codebase_rag.services.graph.memgraph import MemgraphIngestor

_INTEGRATION_DIR = Path(__file__).parent

# Backends the integration suite runs against. Task 14 appends ARCADEDB.
BACKENDS: tuple[GraphBackend, ...] = (GraphBackend.MEMGRAPH,)

MEMGRAPH_IMAGE = "memgraph/memgraph:3.3.0"
MEMGRAPH_READY_LOG = "You are running Memgraph"


class GraphContainer(TypedDict):
    backend: GraphBackend
    host: str
    bolt_port: int
    http_port: int | None
    username: str | None
    password: str | None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Every integration test wipes the whole database, so tests sharing a
    # container must not run concurrently. Grouping PER BACKEND (rather than
    # one group for the whole directory) keeps each backend serialised while
    # letting the two backends run on different xdist workers, so wall clock
    # stays near 1x instead of 2x.
    for item in items:
        if not (item.path and _INTEGRATION_DIR in item.path.parents):
            continue
        backend = _backend_of(item)
        item.add_marker(pytest.mark.xdist_group(f"graph-integration-{backend}"))


def _backend_of(item: pytest.Item) -> str:
    # Parametrised items carry the backend in their id as [memgraph] etc.
    for backend in BACKENDS:
        if f"[{backend}" in item.name or f"-{backend}]" in item.name:
            return str(backend)
    return str(BACKENDS[0])


def _wait_for_port(host: str, port: int, attempts: int = 30) -> None:
    for attempt in range(attempts):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect((host, port))
            sock.close()
            return
        except (TimeoutError, ConnectionRefusedError, OSError):
            if attempt == attempts - 1:
                pytest.fail(f"port {port} on {host} not ready after {attempts} tries")
            time.sleep(0.5)


def _start_memgraph() -> tuple[object, GraphContainer]:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    # Same engine line the packaged stack pins (issue #1257): integration
    # tests must exercise the syntax the shipped Memgraph actually accepts.
    container = DockerContainer(MEMGRAPH_IMAGE)
    container.with_exposed_ports(7687)
    container.waiting_for(LogMessageWaitStrategy(MEMGRAPH_READY_LOG))
    container.start()

    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(7687))
    _wait_for_port(host, port)
    return container, GraphContainer(
        backend=GraphBackend.MEMGRAPH,
        host=host,
        bolt_port=port,
        http_port=None,
        username=None,
        password=None,
    )


@pytest.fixture(scope="session", params=BACKENDS, ids=[str(b) for b in BACKENDS])
def graph_container(request: pytest.FixtureRequest) -> Generator[GraphContainer, None, None]:
    pytest.importorskip("testcontainers")
    backend: GraphBackend = request.param
    if backend == GraphBackend.MEMGRAPH:
        container, info = _start_memgraph()
    else:
        container, info = _start_arcadedb()
    try:
        yield info
    finally:
        container.stop()


def _build_ingestor(info: GraphContainer) -> GraphIngestor:
    if info["backend"] == GraphBackend.MEMGRAPH:
        return MemgraphIngestor(host=info["host"], port=info["bolt_port"])
    from codebase_rag.services.graph.arcadedb import ArcadeDBIngestor

    return ArcadeDBIngestor(
        host=info["host"],
        bolt_port=info["bolt_port"],
        http_port=info["http_port"] or 2480,
        database=ARCADEDB_TEST_DB,
        username=info["username"] or "",
        password=info["password"] or "",
    )


@pytest.fixture(scope="function")
def graph_ingestor(
    graph_container: GraphContainer,
) -> Generator[GraphIngestor, None, None]:
    ingestor: GraphIngestor | None = None
    for attempt in range(10):
        try:
            ingestor = _build_ingestor(graph_container)
            ingestor.__enter__()
            ingestor.execute_write("MATCH (n) DETACH DELETE n")
            break
        except Exception as e:
            if attempt == 9:
                pytest.fail(f"Failed to connect after 10 attempts: {e}")
            time.sleep(0.5)

    assert ingestor is not None
    yield ingestor

    ingestor.execute_write("MATCH (n) DETACH DELETE n")
    ingestor.__exit__(None, None, None)


@pytest.fixture(scope="function")
def memgraph_ingestor(graph_ingestor: GraphIngestor) -> GraphIngestor:
    """Deprecated alias. Retained so the 30 existing test modules keep
    working while they migrate; delete once none reference it."""
    return graph_ingestor
```

`_start_arcadedb` and `ARCADEDB_TEST_DB` land in Task 14. Until then the `else` branch is unreachable because `BACKENDS` holds only Memgraph — add a `raise NotImplementedError` placeholder body and remove it in Task 14.

The `memgraph_connection` fixture (raw `mgclient.Connection`) stays as-is for now. NOTE: Task 5 established it has no consumers at all — Task 16 deletes it.

- [ ] **Step 4: Run the fixture-contract test**

Run: `uv run pytest codebase_rag/tests/integration/test_fixture_contract.py -v`
Expected: PASS, three tests, all with `[memgraph]` in their ids.

- [ ] **Step 5: Run the whole integration suite unchanged**

Run: `uv run pytest -m integration -v`
Expected: PASS. The `memgraph_ingestor` alias means the 30 existing modules need no edit yet.

- [ ] **Step 6: Commit**

```bash
git add codebase_rag/tests/integration/
git commit -m "test: parametrise the integration graph fixtures over backends"
```

---

### Task 6: Write the cross-backend conformance suite

**Files:**
- Create: `codebase_rag/tests/integration/test_graph_backend_conformance.py`

**Interfaces:**
- Consumes: `graph_ingestor` fixture (Task 5), `NODE_UNIQUE_CONSTRAINTS`, `MERGE_KEY_PROPS_BY_REL`, `RelationshipType`, `NodeLabel` from `codebase_rag.constants`
- Produces: the executable contract every backend must satisfy. Nothing imports it.

This is the spec's acceptance surface. Every assertion must hold on both engines without a backend conditional; a test that needs `if backend == ...` is describing a leak in the abstraction, not a legitimate difference.

- [ ] **Step 1: Write the suite**

Create `codebase_rag/tests/integration/test_graph_backend_conformance.py`:

```python
"""The contract every graph backend must satisfy.

Deliberately free of backend conditionals: if an assertion here needs to
branch on the engine, the abstraction leaks and the dialect should absorb
the difference instead.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.services.graph import GraphIngestor

pytestmark = [pytest.mark.integration]

_FN = NodeLabel.FUNCTION.value
_MOD = NodeLabel.MODULE.value
_QN = "qualified_name"


class TestNodeIdentity:
    def test_merge_is_idempotent(self, graph_ingestor: GraphIngestor) -> None:
        for _ in range(2):
            graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f", "name": "f"})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (n:{_FN} {{{_QN}: 'p.m.f'}}) RETURN count(n) AS c"
        )
        assert rows[0]["c"] == 1

    def test_merge_updates_properties_on_second_write(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f", "start_line": 1})
        graph_ingestor.flush_all()
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f", "start_line": 42})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (n:{_FN} {{{_QN}: 'p.m.f'}}) RETURN n.start_line AS line"
        )
        assert len(rows) == 1
        assert rows[0]["line"] == 42

    def test_distinct_keys_make_distinct_nodes(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(f"MATCH (n:{_FN}) RETURN count(n) AS c")
        assert rows[0]["c"] == 2

    def test_node_id_is_an_integer(self, graph_ingestor: GraphIngestor) -> None:
        # vector_store.py keys Qdrant/Milvus payloads on this value, so a
        # string RID would silently break every stored embedding.
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f"})
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(f"MATCH (n:{_FN}) RETURN id(n) AS node_id")
        assert isinstance(rows[0]["node_id"], int)


class TestRelationships:
    def test_relationship_properties_round_trip(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_nodes()
        graph_ingestor.ensure_relationship_batch(
            (_FN, _QN, "p.m.a"),
            RelationshipType.CALLS.value,
            (_FN, _QN, "p.m.b"),
            {"line_number": 7},
        )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->(:{_FN}) "
            "RETURN r.line_number AS line"
        )
        assert [r["line"] for r in rows] == [7]

    def test_merge_does_not_duplicate_the_same_edge(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.a"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.b"})
        graph_ingestor.flush_nodes()
        for _ in range(2):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.a"),
                RelationshipType.CALLS.value,
                (_FN, _QN, "p.m.b"),
            )
            graph_ingestor.flush_relationships()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->(:{_FN}) "
            "RETURN count(r) AS c"
        )
        assert rows[0]["c"] == 1

    def test_flows_to_parallel_edges_survive_merge(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Regression guard for issue #722. MERGE_KEY_PROPS_BY_REL puts
        # (via, kind) into the MERGE pattern so two provenance edges between
        # the same pair stay distinct instead of collapsing into one.
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.src"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.dst"})
        graph_ingestor.flush_nodes()
        for via, kind in (("arg", "direct"), ("ret", "direct")):
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, "p.m.src"),
                RelationshipType.FLOWS_TO.value,
                (_FN, _QN, "p.m.dst"),
                {"via": via, "kind": kind},
            )
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.FLOWS_TO.value}]->(:{_FN}) "
            "RETURN r.via AS via ORDER BY via"
        )
        assert [r["via"] for r in rows] == ["arg", "ret"]


class TestConcurrency:
    def test_parallel_flush_into_one_hot_target(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Many CALLS edges converging on one vertex is the exact shape that
        # provokes optimistic-write conflicts on MVCC engines. This is the
        # test that exercises the retry path.
        target = "p.m.hot"
        callers = [f"p.m.c{i}" for i in range(60)]
        graph_ingestor.ensure_node_batch(_FN, {_QN: target})
        for qn in callers:
            graph_ingestor.ensure_node_batch(_FN, {_QN: qn})
        graph_ingestor.flush_nodes()

        def write(qn: str) -> None:
            graph_ingestor.ensure_relationship_batch(
                (_FN, _QN, qn), RelationshipType.CALLS.value, (_FN, _QN, target)
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, callers))
        graph_ingestor.flush_all()

        rows = graph_ingestor.fetch_all(
            f"MATCH (:{_FN})-[r:{RelationshipType.CALLS.value}]->"
            f"(:{_FN} {{{_QN}: '{target}'}}) RETURN count(r) AS c"
        )
        assert rows[0]["c"] == len(callers)


class TestAdminOperations:
    def _seed_project(self, ingestor: GraphIngestor, name: str) -> None:
        ingestor.ensure_node_batch(
            NodeLabel.PROJECT.value, {"name": name, "root_path": f"/tmp/{name}"}
        )
        ingestor.ensure_node_batch(_MOD, {_QN: f"{name}.mod"})
        ingestor.ensure_node_batch(_FN, {_QN: f"{name}.mod.fn"})
        ingestor.flush_nodes()
        ingestor.ensure_relationship_batch(
            (NodeLabel.PROJECT.value, "name", name),
            RelationshipType.CONTAINS_MODULE.value,
            (_MOD, _QN, f"{name}.mod"),
        )
        ingestor.ensure_relationship_batch(
            (_MOD, _QN, f"{name}.mod"),
            RelationshipType.DEFINES.value,
            (_FN, _QN, f"{name}.mod.fn"),
        )
        ingestor.flush_all()

    def test_list_projects(self, graph_ingestor: GraphIngestor) -> None:
        self._seed_project(graph_ingestor, "alpha")
        self._seed_project(graph_ingestor, "beta")
        assert graph_ingestor.list_projects() == ["alpha", "beta"]

    def test_list_project_roots(self, graph_ingestor: GraphIngestor) -> None:
        self._seed_project(graph_ingestor, "alpha")
        assert graph_ingestor.list_project_roots() == {"alpha": "/tmp/alpha"}

    def test_delete_project_removes_its_subtree(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        self._seed_project(graph_ingestor, "alpha")
        self._seed_project(graph_ingestor, "beta")
        graph_ingestor.delete_project("alpha")

        rows = graph_ingestor.fetch_all(
            f"MATCH (n) WHERE n.{_QN} STARTS WITH 'alpha.' RETURN count(n) AS c"
        )
        assert rows[0]["c"] == 0

    def test_delete_project_leaves_siblings_intact(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        self._seed_project(graph_ingestor, "alpha")
        self._seed_project(graph_ingestor, "beta")
        graph_ingestor.delete_project("alpha")

        assert graph_ingestor.list_projects() == ["beta"]
        rows = graph_ingestor.fetch_all(
            f"MATCH (n) WHERE n.{_QN} STARTS WITH 'beta.' RETURN count(n) AS c"
        )
        assert rows[0]["c"] == 2

    def test_clean_database_empties_the_graph(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        self._seed_project(graph_ingestor, "alpha")
        graph_ingestor.clean_database()

        rows = graph_ingestor.fetch_all("MATCH (n) RETURN count(n) AS c")
        assert rows[0]["c"] == 0

    def test_export_graph_to_dict(self, graph_ingestor: GraphIngestor) -> None:
        self._seed_project(graph_ingestor, "alpha")
        data = graph_ingestor.export_graph_to_dict()

        assert data["metadata"]["total_nodes"] == 3
        assert data["metadata"]["total_relationships"] == 2
        assert all(isinstance(n["node_id"], int) for n in data["nodes"])


class TestSchemaBootstrap:
    def test_ensure_constraints_is_idempotent(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        graph_ingestor.ensure_constraints()
        graph_ingestor.ensure_constraints()

        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f"})
        graph_ingestor.ensure_node_batch(_FN, {_QN: "p.m.f"})
        graph_ingestor.flush_all()
        rows = graph_ingestor.fetch_all(f"MATCH (n:{_FN}) RETURN count(n) AS c")
        assert rows[0]["c"] == 1


class TestResourcePruning:
    def test_unanchored_resources_are_pruned_on_delete_project(
        self, graph_ingestor: GraphIngestor
    ) -> None:
        # Resource nodes carry no project prefix, so delete_project only
        # strips their edges; the prune pass must then remove the ones this
        # project alone anchored.
        graph_ingestor.ensure_node_batch(
            NodeLabel.PROJECT.value, {"name": "alpha", "root_path": "/tmp/alpha"}
        )
        graph_ingestor.ensure_node_batch(_MOD, {_QN: "alpha.mod"})
        graph_ingestor.ensure_node_batch(
            NodeLabel.RESOURCE.value, {_QN: "res1", "kind": "ENDPOINT"}
        )
        graph_ingestor.flush_nodes()
        graph_ingestor.ensure_relationship_batch(
            (NodeLabel.PROJECT.value, "name", "alpha"),
            RelationshipType.CONTAINS_MODULE.value,
            (_MOD, _QN, "alpha.mod"),
        )
        graph_ingestor.ensure_relationship_batch(
            (_MOD, _QN, "alpha.mod"),
            RelationshipType.EXPOSES.value,
            (NodeLabel.RESOURCE.value, _QN, "res1"),
        )
        graph_ingestor.flush_all()

        graph_ingestor.delete_project("alpha")

        rows = graph_ingestor.fetch_all(
            f"MATCH (r:{NodeLabel.RESOURCE.value}) RETURN count(r) AS c"
        )
        assert rows[0]["c"] == 0
```

- [ ] **Step 2: Run the suite against Memgraph**

Run: `uv run pytest codebase_rag/tests/integration/test_graph_backend_conformance.py -v`
Expected: PASS, every id suffixed `[memgraph]`.

If any test fails here, it has found a real Memgraph behaviour the plan mis-stated. Fix the test to match observed behaviour and note the correction — do not weaken an assertion to make it pass.

- [ ] **Step 3: Commit**

```bash
git add codebase_rag/tests/integration/test_graph_backend_conformance.py
git commit -m "test: add the cross-backend graph conformance suite"
```

---

### Task 7: Add the query corpus gate

**Files:**
- Create: `codebase_rag/tests/integration/test_query_corpus.py`

**Interfaces:**
- Consumes: `graph_ingestor` (Task 5); every `CYPHER_*` constant in `codebase_rag/cypher_queries.py` and `codebase_rag/constants/graph.py`
- Produces: nothing importable — it is a gate.

Nothing today checks that the shipped Cypher actually parses: `evals/` runs against `_CapturingIngestor` and never opens a connection. This gate closes that hole for both backends at once.

- [ ] **Step 1: Write the gate**

Create `codebase_rag/tests/integration/test_query_corpus.py`:

```python
"""Every shipped Cypher string must parse and return its declared columns.

The eval harness runs against an in-memory capturing ingestor and never
touches a database, so without this gate a query that no longer parses
reaches users unnoticed — on either backend.
"""

from __future__ import annotations

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.services.graph import GraphIngestor

pytestmark = [pytest.mark.integration]

_FN = NodeLabel.FUNCTION.value
_MOD = NodeLabel.MODULE.value
_QN = "qualified_name"

# (query, params, expected column names). Params use values the seeded
# fixture graph actually contains so a shape assertion is meaningful
# rather than trivially empty.
CORPUS: list[tuple[str, dict[str, object], list[str]]] = [
    (cq.CYPHER_LIST_PROJECTS, {}, ["name", "root_path"]),
    (cq.CYPHER_AUDIT_ORPHANS, {}, ["label", "orphans"]),
    (cq.CYPHER_AUDIT_LABELS, {}, ["label"]),
    (cq.CYPHER_AUDIT_REL_TRIPLES, {}, ["src", "rel", "dst"]),
    (cq.CYPHER_AUDIT_LABEL_PROPS, {}, ["label", "key"]),
    (cq.CYPHER_EXPORT_NODES, {}, ["node_id", "labels", "properties"]),
    (
        cq.CYPHER_EXPORT_RELATIONSHIPS,
        {},
        ["from_id", "to_id", "type", "properties"],
    ),
    (cq.CYPHER_STATS_NODE_COUNTS, {}, ["labels", "count"]),
    (cq.CYPHER_STATS_RELATIONSHIP_COUNTS, {}, ["type", "count"]),
    (cq.CYPHER_ANY_SHARED_STRUCTURE, {}, ["damaged"]),
    (cq.CYPHER_ANY_KEYLESS_STRUCTURE, {}, ["damaged"]),
    (
        cq.CYPHER_FIND_BY_QUALIFIED_NAME,
        {"qn": "alpha.mod.fn"},
        ["name", "start", "end", "path", "absolute_path", "docstring"],
    ),
    (
        cq.CYPHER_DEAD_CODE_NODES,
        {"project_prefix": "alpha."},
        [
            "label",
            "qualified_name",
            "name",
            "path",
            "start_line",
            "end_line",
            "decorators",
            "is_exported",
            "overrides_external",
            "rust_cfg_test_mods",
            "rust_ungated_mods",
        ],
    ),
    (
        cq.CYPHER_DEAD_CODE_RELS,
        {"project_prefix": "alpha."},
        ["from_label", "from_qn", "rel_type", "to_label", "to_qn"],
    ),
    (cq.CYPHER_EXAMPLE_DECORATED_FUNCTIONS, {}, ["name", "qualified_name", "type"]),
    (cq.CYPHER_EXAMPLE_CONTENT_BY_PATH, {}, ["name", "path", "type"]),
    (cq.CYPHER_EXAMPLE_KEYWORD_SEARCH, {}, ["name", "qualified_name", "type"]),
    (cq.CYPHER_EXAMPLE_FIND_FILE, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_README, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_PYTHON_FILES, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_TASKS, {}, ["qualified_name", "name", "type"]),
    (cq.CYPHER_EXAMPLE_FILES_IN_FOLDER, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_LIMIT_ONE, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_PROJECT_SCOPED, {}, ["name", "qualified_name", "type"]),
    (
        cq.CYPHER_EXAMPLE_CLASS_METHODS,
        {},
        ["className", "methodName", "qualified_name", "type"],
    ),
    (cq.CYPHER_EXAMPLE_FIND_PATTERN, {}, ["path", "pattern", "line", "message"]),
    (cq.CYPHER_EXAMPLE_SECURITY_ISSUES, {}, ["path", "rule", "line", "message"]),
    (cq.CYPHER_EXAMPLE_CODE_SMELLS, {}, ["path", "smell", "line", "message"]),
]


@pytest.fixture
def seeded(graph_ingestor: GraphIngestor) -> GraphIngestor:
    graph_ingestor.ensure_node_batch(
        NodeLabel.PROJECT.value, {"name": "alpha", "root_path": "/tmp/alpha"}
    )
    graph_ingestor.ensure_node_batch(
        _MOD, {_QN: "alpha.mod", "path": "alpha/mod.py"}
    )
    graph_ingestor.ensure_node_batch(
        _FN,
        {
            _QN: "alpha.mod.fn",
            "name": "fn",
            "path": "alpha/mod.py",
            "start_line": 1,
            "end_line": 5,
            "decorators": ["task"],
        },
    )
    graph_ingestor.ensure_node_batch(
        NodeLabel.FILE.value,
        {
            "absolute_path": "/tmp/alpha/README.md",
            "path": "README.md",
            "name": "README.md",
            "extension": ".md",
        },
    )
    graph_ingestor.flush_nodes()
    graph_ingestor.ensure_relationship_batch(
        (NodeLabel.PROJECT.value, "name", "alpha"),
        RelationshipType.CONTAINS_MODULE.value,
        (_MOD, _QN, "alpha.mod"),
    )
    graph_ingestor.ensure_relationship_batch(
        (_MOD, _QN, "alpha.mod"),
        RelationshipType.DEFINES.value,
        (_FN, _QN, "alpha.mod.fn"),
    )
    graph_ingestor.flush_all()
    return graph_ingestor


@pytest.mark.parametrize(
    ("query", "params", "columns"),
    CORPUS,
    ids=[q.strip().splitlines()[0][:48] for q, _, _ in CORPUS],
)
def test_shipped_query_parses_and_returns_declared_columns(
    seeded: GraphIngestor,
    query: str,
    params: dict[str, object],
    columns: list[str],
) -> None:
    rows = seeded.fetch_all(query, params)  # must not raise
    if rows:
        assert set(rows[0].keys()) == set(columns)
```

- [ ] **Step 2: Run the gate against Memgraph**

Run: `uv run pytest codebase_rag/tests/integration/test_query_corpus.py -v`
Expected: PASS.

If a query fails to parse, that is a genuine pre-existing bug this gate just found. Fix the query, do not delete the corpus entry.

- [ ] **Step 3: Commit**

```bash
git add codebase_rag/tests/integration/test_query_corpus.py
git commit -m "test: gate every shipped Cypher query on parsing against a live backend"
```

---
## Phase 3 — ArcadeDB dialect, HTTP client, schema bootstrap

### Task 8: Write the ArcadeDB HTTP client

**Files:**
- Create: `codebase_rag/services/graph/arcade_http.py`
- Modify: `codebase_rag/constants/graph.py` (endpoint + payload keys)
- Modify: `codebase_rag/exceptions.py`
- Test: `codebase_rag/tests/test_arcade_http.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ArcadeHttpClient(host: str, port: int, database: str, username: str, password: str)` with one public method `sql(command: str) -> list[dict[str, object]]`, raising `ArcadeHttpError` on non-2xx.

The client exists solely because ArcadeDB's Bolt listener accepts Cypher and rejects SQL, while index creation requires SQL. It has no role in the hot path and must not grow one.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/test_arcade_http.py`:

```python
from __future__ import annotations

import json
from typing import Any

import pytest

from codebase_rag.exceptions import ArcadeHttpError
from codebase_rag.services.graph.arcade_http import ArcadeHttpClient


class _FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_sql_posts_to_the_command_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse(200, {"result": [{"ok": 1}]})

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="db", port=2480, database="cg", username="root", password="pw"
    )
    rows = client.sql("CREATE VERTEX TYPE Function IF NOT EXISTS")

    assert captured["url"] == "http://db:2480/api/v1/command/cg"
    assert captured["body"] == {
        "language": "sql",
        "command": "CREATE VERTEX TYPE Function IF NOT EXISTS",
    }
    assert captured["auth"].startswith("Basic ")
    assert rows == [{"ok": 1}]


def test_sql_raises_on_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        raise urllib.error.HTTPError(
            req.full_url, 500, "Server Error", {}, None  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="db", port=2480, database="cg", username="root", password="pw"
    )
    with pytest.raises(ArcadeHttpError, match="500"):
        client.sql("CREATE VERTEX TYPE Bad")


def test_sql_returns_empty_list_when_result_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(200, {})

    monkeypatch.setattr(
        "codebase_rag.services.graph.arcade_http.urllib.request.urlopen", fake_urlopen
    )
    client = ArcadeHttpClient(
        host="db", port=2480, database="cg", username="root", password="pw"
    )
    assert client.sql("CREATE VERTEX TYPE X IF NOT EXISTS") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_arcade_http.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codebase_rag.services.graph.arcade_http'`

- [ ] **Step 3: Add the constants**

In `codebase_rag/constants/graph.py`:

```python
# ArcadeDB HTTP: Bolt accepts Cypher only, so schema DDL (which is SQL)
# goes over the REST endpoint instead.
ARCADE_HTTP_SCHEME = "http"
ARCADE_COMMAND_PATH = "/api/v1/command/{database}"
ARCADE_LANG_SQL = "sql"
ARCADE_KEY_LANGUAGE = "language"
ARCADE_KEY_COMMAND = "command"
ARCADE_KEY_RESULT = "result"
ARCADE_HTTP_TIMEOUT_S = 30.0
```

In `codebase_rag/exceptions.py`:

```python
class ArcadeHttpError(RuntimeError):
    """A non-2xx response from ArcadeDB's HTTP command endpoint."""


ARCADE_HTTP_FAILED = "ArcadeDB HTTP command failed ({status}): {detail}\nCommand: {command}"
```

- [ ] **Step 4: Write the client**

Create `codebase_rag/services/graph/arcade_http.py`:

```python
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from ... import exceptions as ex
from ...constants import (
    ARCADE_COMMAND_PATH,
    ARCADE_HTTP_SCHEME,
    ARCADE_HTTP_TIMEOUT_S,
    ARCADE_KEY_COMMAND,
    ARCADE_KEY_LANGUAGE,
    ARCADE_KEY_RESULT,
    ARCADE_LANG_SQL,
)


class ArcadeHttpClient:
    """SQL over ArcadeDB's REST endpoint, used only for schema DDL.

    ArcadeDB's Bolt listener accepts Cypher and rejects SQL, but creating an
    index requires SQL (and requires the property to be declared first). This
    client covers exactly that gap; it is never on the ingestion hot path.
    """

    __slots__ = ("_base_url", "_auth_header")

    def __init__(
        self, host: str, port: int, database: str, username: str, password: str
    ) -> None:
        path = ARCADE_COMMAND_PATH.format(database=database)
        self._base_url = f"{ARCADE_HTTP_SCHEME}://{host}:{port}{path}"
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"

    def sql(self, command: str) -> list[dict[str, Any]]:
        payload = json.dumps(
            {ARCADE_KEY_LANGUAGE: ARCADE_LANG_SQL, ARCADE_KEY_COMMAND: command}
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - fixed http scheme
            self._base_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._auth_header,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed http scheme
                request, timeout=ARCADE_HTTP_TIMEOUT_S
            ) as response:
                body = json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise ex.ArcadeHttpError(
                ex.ARCADE_HTTP_FAILED.format(
                    status=e.code, detail=e.reason, command=command
                )
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ex.ArcadeHttpError(
                ex.ARCADE_HTTP_FAILED.format(status="n/a", detail=e, command=command)
            ) from e
        result = body.get(ARCADE_KEY_RESULT, [])
        return list(result) if isinstance(result, list) else []
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest codebase_rag/tests/test_arcade_http.py -v && make lint`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add codebase_rag/services/graph/arcade_http.py codebase_rag/constants/graph.py codebase_rag/exceptions.py codebase_rag/tests/test_arcade_http.py
git commit -m "feat: add the ArcadeDB HTTP client for SQL schema DDL"
```

---

### Task 9: Generate the ArcadeDB schema DDL

**Files:**
- Create: `codebase_rag/services/graph/arcadedb.py` (dialect half only)
- Modify: `codebase_rag/constants/graph.py` (DDL templates)
- Test: `codebase_rag/tests/test_arcadedb_dialect.py`

**Interfaces:**
- Consumes: `NodeLabel`, `RelationshipType`, `NODE_UNIQUE_CONSTRAINTS` from `codebase_rag.constants`; `ArcadeHttpClient` (Task 8)
- Produces: `build_arcade_schema_statements() -> list[str]` — the ordered, idempotent DDL, derived entirely from the existing constants with no second schema list to maintain.

ArcadeDB refuses to index an undeclared property, so ordering matters: vertex type, then property, then index. All statements use `IF NOT EXISTS` so re-running is free.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/test_arcadedb_dialect.py`:

```python
from __future__ import annotations

from codebase_rag.constants import (
    NODE_UNIQUE_CONSTRAINTS,
    NodeLabel,
    RelationshipType,
)
from codebase_rag.services.graph.arcadedb import build_arcade_schema_statements


def test_generates_one_vertex_type_per_node_label() -> None:
    stmts = build_arcade_schema_statements()
    for label in NodeLabel:
        assert f"CREATE VERTEX TYPE {label.value} IF NOT EXISTS" in stmts


def test_generates_one_edge_type_per_relationship_type() -> None:
    stmts = build_arcade_schema_statements()
    for rel in RelationshipType:
        assert f"CREATE EDGE TYPE {rel.value} IF NOT EXISTS" in stmts


def test_declares_the_unique_key_property_before_indexing_it() -> None:
    # ArcadeDB rejects CREATE INDEX on an undeclared property, so the
    # property statement must come first.
    stmts = build_arcade_schema_statements()
    prop = "CREATE PROPERTY Function.qualified_name IF NOT EXISTS STRING"
    index = "CREATE INDEX IF NOT EXISTS ON Function (qualified_name) UNIQUE"
    assert stmts.index(prop) < stmts.index(index)


def test_creates_the_vertex_type_before_its_property() -> None:
    stmts = build_arcade_schema_statements()
    assert stmts.index("CREATE VERTEX TYPE Function IF NOT EXISTS") < stmts.index(
        "CREATE PROPERTY Function.qualified_name IF NOT EXISTS STRING"
    )


def test_generates_a_unique_index_for_every_constrained_label() -> None:
    stmts = build_arcade_schema_statements()
    for label, key in NODE_UNIQUE_CONSTRAINTS.items():
        assert f"CREATE INDEX IF NOT EXISTS ON {label} ({key}) UNIQUE" in stmts


def test_statement_count_matches_the_constant_tables() -> None:
    # 20 vertex types + 20 properties + 20 unique indexes + 25 edge types.
    stmts = build_arcade_schema_statements()
    expected = (
        len(NodeLabel)
        + len(NODE_UNIQUE_CONSTRAINTS) * 2
        + len(RelationshipType)
    )
    assert len(stmts) == expected


def test_every_statement_is_idempotent() -> None:
    for stmt in build_arcade_schema_statements():
        assert "IF NOT EXISTS" in stmt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_dialect.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codebase_rag.services.graph.arcadedb'`

- [ ] **Step 3: Add the DDL templates**

In `codebase_rag/constants/graph.py`:

```python
ARCADE_DDL_VERTEX_TYPE = "CREATE VERTEX TYPE {label} IF NOT EXISTS"
ARCADE_DDL_EDGE_TYPE = "CREATE EDGE TYPE {rel_type} IF NOT EXISTS"
ARCADE_DDL_PROPERTY = "CREATE PROPERTY {label}.{prop} IF NOT EXISTS STRING"
ARCADE_DDL_UNIQUE_INDEX = "CREATE INDEX IF NOT EXISTS ON {label} ({prop}) UNIQUE"
```

Every unique key in `_NODE_LABEL_UNIQUE_KEYS` is a string (`name`, `qualified_name`, `absolute_path`), so `STRING` is correct for all 20 — no per-label type table is needed.

- [ ] **Step 4: Write the generator**

Create `codebase_rag/services/graph/arcadedb.py`:

```python
from __future__ import annotations

from ...constants import (
    ARCADE_DDL_EDGE_TYPE,
    ARCADE_DDL_PROPERTY,
    ARCADE_DDL_UNIQUE_INDEX,
    ARCADE_DDL_VERTEX_TYPE,
    NODE_UNIQUE_CONSTRAINTS,
    NodeLabel,
    RelationshipType,
)


def build_arcade_schema_statements() -> list[str]:
    """The full DDL bootstrap, ordered so each statement's dependencies exist.

    Derived entirely from the existing constant tables: the guard in
    constants/graph.py that rejects any NodeLabel without a unique key keeps
    this in sync with the schema for free, so there is no second list to
    maintain.
    """
    statements: list[str] = []

    for label in NodeLabel:
        statements.append(ARCADE_DDL_VERTEX_TYPE.format(label=label.value))

    # ArcadeDB refuses CREATE INDEX on an undeclared property, so every
    # property declaration precedes every index.
    for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
        statements.append(ARCADE_DDL_PROPERTY.format(label=label, prop=prop))
    for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
        statements.append(ARCADE_DDL_UNIQUE_INDEX.format(label=label, prop=prop))

    for rel_type in RelationshipType:
        statements.append(ARCADE_DDL_EDGE_TYPE.format(rel_type=rel_type.value))

    return statements
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_dialect.py -v`
Expected: PASS, seven tests.

- [ ] **Step 6: Commit**

```bash
git add codebase_rag/services/graph/arcadedb.py codebase_rag/constants/graph.py codebase_rag/tests/test_arcadedb_dialect.py
git commit -m "feat: generate ArcadeDB schema DDL from the existing constant tables"
```

---

### Task 10: Complete ArcadeDBDialect

**Files:**
- Modify: `codebase_rag/services/graph/arcadedb.py`
- Modify: `codebase_rag/constants/graph.py` (error substrings, placeholder catalog)
- Modify: `codebase_rag/tests/test_arcadedb_dialect.py`
- Modify: `codebase_rag/tests/test_graph_factory.py` (drop the two xfail markers)

**Interfaces:**
- Consumes: `GraphDialect` (Task 2), `build_arcade_schema_statements` (Task 9), `ArcadeHttpClient` (Task 8)
- Produces: `ArcadeDBDialect(http: ArcadeHttpClient | None = None)` implementing all six `GraphDialect` members.

- [ ] **Step 1: Write the failing test**

Append to `codebase_rag/tests/test_arcadedb_dialect.py`:

```python
import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph.dialect import GraphDialect
from codebase_rag.services.graph.arcadedb import ArcadeDBDialect


def test_satisfies_the_dialect_protocol() -> None:
    assert isinstance(ArcadeDBDialect(), GraphDialect)


def test_name() -> None:
    assert ArcadeDBDialect().name == GraphBackend.ARCADEDB


def test_apply_query_limit_is_identity() -> None:
    # ArcadeDB has no QUERY MEMORY LIMIT equivalent; the read path bounds
    # wall clock with a transaction timeout instead.
    query = "MATCH (n) RETURN n"
    assert ArcadeDBDialect().apply_query_limit(query, 512) == query


def test_allowed_prefixes_narrow_to_algo() -> None:
    # `algo.` is already in the shared allowlist; the dialect narrows to it
    # rather than extending, because MAGE namespaces do not resolve here.
    assert ArcadeDBDialect().allowed_proc_prefixes == frozenset({"algo."})


@pytest.mark.parametrize(
    "message",
    [
        "Concurrent modification of record #12:3",
        "ConcurrentModificationException: cannot update",
        "TransientError: please retry",
    ],
)
def test_is_retryable_matches_write_conflicts(message: str) -> None:
    assert ArcadeDBDialect().is_retryable(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    ["Syntax error at line 1", "Unknown procedure/function: algo.nope"],
)
def test_is_retryable_rejects_permanent_errors(message: str) -> None:
    assert ArcadeDBDialect().is_retryable(RuntimeError(message)) is False


def test_is_benign_error_matches_already_exists() -> None:
    d = ArcadeDBDialect()
    assert d.is_benign_error(RuntimeError("Type 'Function' already exists")) is True
    assert d.is_benign_error(RuntimeError("Syntax error")) is False


def test_ensure_schema_sends_every_statement_over_http() -> None:
    sent: list[str] = []

    class _FakeHttp:
        def sql(self, command: str) -> list[dict[str, object]]:
            sent.append(command)
            return []

    ArcadeDBDialect(http=_FakeHttp()).ensure_schema(ingestor=None)  # type: ignore[arg-type]
    assert sent == build_arcade_schema_statements()


def test_procedure_catalog_mentions_only_algo_procedures() -> None:
    catalog = ArcadeDBDialect().procedure_catalog
    assert "algo." in catalog
    for absent in ("nxalg.", "pagerank.get", "graph_util.", "path.expand"):
        assert absent not in catalog
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_dialect.py -v`
Expected: FAIL with `ImportError: cannot import name 'ArcadeDBDialect'`

- [ ] **Step 3: Add the constants**

In `codebase_rag/constants/graph.py`:

```python
# ArcadeDB is MVCC/optimistic: parallel MERGE into a shared vertex raises
# these, and they are worth retrying. Memgraph's engine does not produce them.
ARCADE_RETRYABLE_SUBSTRINGS: tuple[str, ...] = (
    "concurrent modification",
    "concurrentmodification",
    "transient",
    "neo.transienterror",
)
ARCADE_BENIGN_SUBSTRINGS: tuple[str, ...] = ("already exists",)
ARCADE_ALLOWED_PROCEDURE_PREFIXES: frozenset[str] = frozenset({"algo."})

# Placeholder catalog. Task 15 replaces this with the enumerated result of
# probing a live server; ArcadeDB does not document its Cypher CALL surface,
# so it cannot be written from the docs.
ARCADE_PROCEDURE_CATALOG = """- **PageRank**: `CALL algo.pageRank() YIELD node, score`
- **Strongly connected components**: `CALL algo.scc() YIELD node, componentId`
- **Weakly connected components**: `CALL algo.wcc() YIELD node, componentId`
- **Communities**: `CALL algo.louvain() YIELD node, communityId`

Important: these procedures yield `node` as a **record-id string** such as
`"#46:0"`, not a node you can read properties from. To get properties, match
the node separately by its stored key rather than writing `node.name`."""
```

- [ ] **Step 4: Write the dialect**

Append to `codebase_rag/services/graph/arcadedb.py`:

```python
class ArcadeDBDialect:
    __slots__ = ("_http",)

    def __init__(self, http: ArcadeHttpClient | None = None) -> None:
        self._http = http

    @property
    def name(self) -> GraphBackend:
        return GraphBackend.ARCADEDB

    def ensure_schema(self, ingestor: GraphIngestor) -> None:
        if self._http is None:
            raise ex.ArcadeHttpError(ex.ARCADE_NO_HTTP_CLIENT)
        for statement in build_arcade_schema_statements():
            try:
                self._http.sql(statement)
            except Exception as exc:
                if not self.is_benign_error(exc):
                    raise

    def apply_query_limit(self, query: str, mb: int) -> str:
        # No per-query memory cap exists here. ArcadeDBIngestor bounds reads
        # with a transaction timeout instead; see settings.QUERY_TIMEOUT_S.
        return query

    @property
    def procedure_catalog(self) -> str:
        return ARCADE_PROCEDURE_CATALOG

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        return ARCADE_ALLOWED_PROCEDURE_PREFIXES

    def is_benign_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(s in text for s in ARCADE_BENIGN_SUBSTRINGS)

    def is_retryable(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(s in text for s in ARCADE_RETRYABLE_SUBSTRINGS)
```

Add to `codebase_rag/exceptions.py`:

```python
ARCADE_NO_HTTP_CLIENT = (
    "ArcadeDBDialect needs an HTTP client to run schema DDL; construct it "
    "with ArcadeDBDialect(http=ArcadeHttpClient(...))."
)
```

**CORRECTION (found in Task 10):** the plan previously said to add `QUERY_TIMEOUT_S: float = 600.0`. That setting ALREADY EXISTS at `60.0` and is consumed by `codebase_rag/tools/codebase_query.py` as the asyncio wall-clock ceiling on every LLM-generated graph query, for BOTH backends. Changing it would be a 10x behaviour change for existing Memgraph users, violating the identical-behaviour constraint. Leave `QUERY_TIMEOUT_S` alone.

Instead add a separate, ArcadeDB-only setting to `AppConfig`:

```python
    # Server-side transaction ceiling for ArcadeDB Bolt queries. Distinct from
    # QUERY_TIMEOUT_S, which is the agent-side wall-clock bound applied to any
    # backend in tools/codebase_query.py. This one substitutes for Memgraph's
    # QUERY MEMORY LIMIT, which ArcadeDB cannot parse.
    ARCADEDB_TX_TIMEOUT_S: float = Field(default=600.0, gt=0)
```

- [ ] **Step 5: Remove the xfail markers from Task 3's tests**

```bash
python3 - <<'PY'
import pathlib, re
p = pathlib.Path("codebase_rag/tests/test_graph_factory.py")
s = p.read_text()
s = re.sub(r'@pytest\.mark\.xfail\(reason="ArcadeDBDialect lands in Task 10", strict=False\)\n', "", s)
p.write_text(s)
PY
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_dialect.py codebase_rag/tests/test_graph_factory.py -v && uv run pytest -n auto -m "not integration"`
Expected: PASS, no xfails remaining.

- [ ] **Step 7: Commit**

```bash
git add codebase_rag/services/graph/arcadedb.py codebase_rag/constants/graph.py codebase_rag/config.py codebase_rag/exceptions.py codebase_rag/tests/
git commit -m "feat: complete the ArcadeDB graph dialect"
```

---
## Phase 4 — The ArcadeDB ingestor

### Task 11: ArcadeDBIngestor connection and query surface

**Files:**
- Modify: `codebase_rag/services/graph/arcadedb.py`
- Modify: `pyproject.toml` (`arcadedb` extra)
- Test: `codebase_rag/tests/test_arcadedb_ingestor.py`

**Interfaces:**
- Consumes: `ArcadeDBDialect` (Task 10), `ArcadeHttpClient` (Task 8), `GraphIngestor` (Task 1)
- Produces: `ArcadeDBIngestor(host, bolt_port, http_port, database, username, password, batch_size=1000, use_merge=True)` with `__enter__`/`__exit__`/`__aenter__`/`__aexit__`, `fetch_all`, `execute_write`, `ensure_constraints`.

- [ ] **Step 1: Add the optional dependency**

In `pyproject.toml` under `[project.optional-dependencies]`:

```toml
arcadedb = [
    "neo4j>=5.28,<6",
]
```

Then: `uv sync --extra arcadedb --extra test`

- [ ] **Step 2: Write the failing test**

Create `codebase_rag/tests/test_arcadedb_ingestor.py`:

```python
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.services.graph import GraphIngestor
from codebase_rag.services.graph.arcadedb import ArcadeDBIngestor


def _ingestor(**kw: Any) -> ArcadeDBIngestor:
    defaults = dict(
        host="db",
        bolt_port=7687,
        http_port=2480,
        database="cg",
        username="root",
        password="pw",
    )
    return ArcadeDBIngestor(**{**defaults, **kw})


def test_satisfies_the_graph_ingestor_protocol() -> None:
    assert isinstance(_ingestor(), GraphIngestor)


def test_requires_credentials() -> None:
    # ArcadeDB's Bolt listener rejects the `none` auth scheme.
    with pytest.raises(ValueError, match="credentials"):
        _ingestor(username="", password="")


def test_rejects_batch_size_below_one() -> None:
    with pytest.raises(ValueError):
        _ingestor(batch_size=0)


def test_enter_opens_a_driver_with_basic_auth() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        ingestor = _ingestor()
        ingestor.__enter__()
        gdb.driver.assert_called_once_with("bolt://db:7687", auth=("root", "pw"))
        ingestor.__exit__(None, None, None)


def test_exit_closes_the_driver() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        driver = MagicMock()
        gdb.driver.return_value = driver
        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.__exit__(None, None, None)
        driver.close.assert_called_once()


def test_fetch_all_maps_records_to_dicts() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([{"a": 1, "b": 2}])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        assert ingestor.fetch_all("MATCH (n) RETURN n.a AS a, n.b AS b") == [
            {"a": 1, "b": 2}
        ]
        ingestor.__exit__(None, None, None)


def test_fetch_all_does_not_append_a_memory_limit() -> None:
    # The dialect's apply_query_limit is identity here; appending Memgraph's
    # suffix would be a parse error on ArcadeDB.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.fetch_all("MATCH (n) RETURN n")
        sent = session.run.call_args[0][0]
        assert "QUERY MEMORY LIMIT" not in str(sent)
        ingestor.__exit__(None, None, None)


def test_fetch_all_carries_the_timeout_on_the_query_object() -> None:
    # Session.run(query, parameters=None, **kwargs) means a bare timeout=
    # kwarg becomes a Cypher parameter and bounds nothing. This timeout is
    # the only guard left after QUERY MEMORY LIMIT, so pin the vehicle.
    from neo4j import Query

    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.fetch_all("MATCH (n) RETURN n")
        sent = session.run.call_args[0][0]
        assert isinstance(sent, Query)
        assert sent.timeout is not None
        assert "timeout" not in session.run.call_args.kwargs
        ingestor.__exit__(None, None, None)


def test_fetch_all_passes_parameters_through() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.fetch_all("MATCH (n {qn: $qn}) RETURN n", {"qn": "a.b"})
        assert session.run.call_args.kwargs["qn"] == "a.b"
        ingestor.__exit__(None, None, None)


def test_ensure_constraints_runs_the_schema_ddl() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase"):
        ingestor = _ingestor()
        ingestor.__enter__()
        with patch.object(ingestor._dialect, "ensure_schema") as ensure:
            ingestor.ensure_constraints()
            ensure.assert_called_once_with(ingestor)
        ingestor.__exit__(None, None, None)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_ingestor.py -v`
Expected: FAIL with `ImportError: cannot import name 'ArcadeDBIngestor'`

- [ ] **Step 4: Write the connection and query surface**

Append to `codebase_rag/services/graph/arcadedb.py`:

```python
class ArcadeDBIngestor:
    """Cypher over Bolt for data; SQL over HTTP for schema DDL.

    ArcadeDB's Bolt listener accepts Cypher only, so the two transports are
    not a choice — index creation is SQL and has nowhere else to go.
    """

    __slots__ = (
        "_bolt_port",
        "_database",
        "_dialect",
        "_driver",
        "_executor",
        "_host",
        "_http",
        "_http_port",
        "_password",
        "_rel_count",
        "_rel_groups",
        "_username",
        "_use_merge",
        "batch_size",
        "node_buffer",
    )

    def __init__(
        self,
        host: str,
        bolt_port: int,
        http_port: int,
        database: str,
        username: str,
        password: str,
        batch_size: int = 1000,
        use_merge: bool = True,
    ) -> None:
        if not username or not password:
            raise ValueError(ex.ARCADE_CREDENTIALS_REQUIRED)
        if batch_size < 1:
            raise ValueError(ex.BATCH_SIZE)
        self._host = host
        self._bolt_port = bolt_port
        self._http_port = http_port
        self._database = database
        self._username = username
        self._password = password
        self.batch_size = batch_size
        self._use_merge = use_merge
        self._driver: Any | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._http = ArcadeHttpClient(
            host=host,
            port=http_port,
            database=database,
            username=username,
            password=password,
        )
        self._dialect = ArcadeDBDialect(http=self._http)
        self.node_buffer: list[tuple[str, dict[str, PropertyValue]]] = []
        self._rel_count = 0
        self._rel_groups: defaultdict[
            tuple[str, str, str, str, str], list[RelBatchRow]
        ] = defaultdict(list)

    @property
    def _bolt_uri(self) -> str:
        return f"{ARCADE_BOLT_SCHEME}://{self._host}:{self._bolt_port}"

    def __enter__(self) -> ArcadeDBIngestor:
        logger.info(ls.ARCADE_CONNECTING.format(uri=self._bolt_uri))
        self._driver = GraphDatabase.driver(
            self._bolt_uri, auth=(self._username, self._password)
        )
        self._executor = ThreadPoolExecutor(max_workers=settings.FLUSH_THREAD_POOL_SIZE)
        logger.info(ls.ARCADE_CONNECTED)
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        try:
            if exc_type:
                logger.exception(ls.ARCADE_EXCEPTION.format(error=exc_val))
                try:
                    self.flush_all()
                except Exception as flush_err:
                    logger.error(ls.ARCADE_FLUSH_ERROR.format(error=flush_err))
            else:
                self.flush_all()
        finally:
            if self._executor:
                self._executor.shutdown(wait=True)
                self._executor = None
            if self._driver:
                self._driver.close()
                self._driver = None
                logger.info(ls.ARCADE_DISCONNECTED)

    async def __aenter__(self) -> ArcadeDBIngestor:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)

    @contextmanager
    def _session(self) -> Generator[Any, None, None]:
        # neo4j.Driver is thread-safe and pools internally, so unlike the
        # Memgraph path there is no hand-rolled per-thread connection.
        if self._driver is None:
            raise ConnectionError(ex.CONN)
        with self._driver.session(database=self._database) as session:
            yield session

    def _run(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[ResultRow]:
        # The timeout MUST ride on a Query object. Session.run's signature is
        # run(query, parameters=None, **kwargs), so a bare `timeout=` kwarg
        # would be sent as a Cypher parameter named "timeout" and silently
        # apply no bound at all — and this timeout is the only guard left on
        # runaway LLM-generated queries once QUERY MEMORY LIMIT is gone.
        with self._session() as session:
            result = session.run(
                Query(query, timeout=settings.ARCADEDB_TX_TIMEOUT_S), **(params or {})
            )
            return [dict(record) for record in result]

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        bounded = self._dialect.apply_query_limit(
            query, settings.QUERY_MEMORY_LIMIT_MB
        )
        logger.debug(ls.ARCADE_FETCH_QUERY, query=bounded, params=params)
        return self._run(bounded, params)

    def execute_write(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> None:
        logger.debug(ls.ARCADE_WRITE_QUERY, query=query, params=params)
        self._run(query, params)

    def ensure_constraints(self) -> None:
        logger.info(ls.ARCADE_ENSURING_SCHEMA)
        self._dialect.ensure_schema(self)
        logger.info(ls.ARCADE_SCHEMA_DONE)
```

Module header imports for `arcadedb.py`:

```python
from __future__ import annotations

import types
from collections import defaultdict
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from loguru import logger
from neo4j import GraphDatabase, Query

from ... import exceptions as ex
from ... import logs as ls
from ...config import settings
from ...constants import (
    ARCADE_ALLOWED_PROCEDURE_PREFIXES,
    ARCADE_BENIGN_SUBSTRINGS,
    ARCADE_BOLT_SCHEME,
    ARCADE_PROCEDURE_CATALOG,
    ARCADE_RETRYABLE_SUBSTRINGS,
    KEY_NAME,
    KEY_PROJECT_NAME,
    MERGE_KEY_PROPS_BY_REL,
    NODE_UNIQUE_CONSTRAINTS,
    GraphBackend,
)
from ...cypher_queries import (
    CYPHER_DELETE_ALL,
    CYPHER_DELETE_PROJECT,
    CYPHER_EXPORT_NODES,
    CYPHER_EXPORT_RELATIONSHIPS,
    CYPHER_LIST_PROJECTS,
    build_create_node_query,
    build_create_relationship_query,
    build_merge_node_query,
    build_merge_relationship_query,
    wrap_with_unwind,
)
from ...types_defs import (
    BatchParams,
    GraphData,
    GraphMetadata,
    NodeBatchRow,
    PropertyValue,
    RelBatchRow,
    ResultRow,
)
from ...utils.path_utils import project_roots_from_rows
from ..resource_cleanup import prune_unanchored_resources
from .retry import retry_on_transient
from .arcade_http import ArcadeHttpClient
from .protocol import GraphIngestor
```

Add `ARCADE_BOLT_SCHEME = "bolt"` to `constants/graph.py` and to `exceptions.py`:

```python
ARCADE_CREDENTIALS_REQUIRED = (
    "ArcadeDB requires a username and password: its Bolt listener rejects "
    "the 'none' auth scheme."
)
```

Add these to `codebase_rag/logs.py`:

```python
ARCADE_CONNECTING = "Connecting to ArcadeDB at {uri}..."
ARCADE_CONNECTED = "ArcadeDB connection established."
ARCADE_DISCONNECTED = "ArcadeDB connection closed."
ARCADE_EXCEPTION = "Exception during ArcadeDB session: {error}"
ARCADE_FLUSH_ERROR = "Failed to flush buffers during error handling: {error}"
ARCADE_FETCH_QUERY = "Executing ArcadeDB read query"
ARCADE_WRITE_QUERY = "Executing ArcadeDB write query"
ARCADE_ENSURING_SCHEMA = "Ensuring ArcadeDB schema (types, properties, indexes)..."
ARCADE_SCHEMA_DONE = "ArcadeDB schema ready."
ARCADE_BATCH_ERROR = "ArcadeDB batch execution failed: {error}"
ARCADE_CYPHER_QUERY = "Query: {query}"
ARCADE_CLEANING_DB = "Cleaning ArcadeDB database..."
ARCADE_DB_CLEANED = "ArcadeDB database cleaned."
ARCADE_DELETING_PROJECT = "Deleting project '{project_name}' from ArcadeDB..."
ARCADE_PROJECT_DELETED = "Project '{project_name}' deleted."
ARCADE_NODES_FLUSHED = "Flushed {flushed}/{total} nodes."
ARCADE_RELS_FLUSHED = "Flushed {total} relationships ({success} ok, {failed} failed)."
ARCADE_NO_CONSTRAINT = "No unique key registered for label {label}; skipping."
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_ingestor.py -v`
Expected: PASS, nine tests.

- [ ] **Step 6: Commit**

```bash
git add codebase_rag/services/graph/arcadedb.py codebase_rag/constants/ codebase_rag/exceptions.py codebase_rag/logs.py pyproject.toml codebase_rag/tests/test_arcadedb_ingestor.py
git commit -m "feat: add the ArcadeDB ingestor connection and query surface"
```

---

### Task 12: ArcadeDB batching, parallel flush, and retry

**Files:**
- Modify: `codebase_rag/services/graph/arcadedb.py`
- Modify: `codebase_rag/tests/test_arcadedb_ingestor.py`

**Interfaces:**
- Consumes: `retry_on_transient` (Task 2), `build_merge_node_query` / `build_merge_relationship_query` / `build_create_node_query` / `build_create_relationship_query` / `wrap_with_unwind` from `codebase_rag.cypher_queries`, `MERGE_KEY_PROPS_BY_REL`
- Produces: `ensure_node_batch`, `ensure_relationship_batch`, `flush_nodes`, `flush_relationships`, `flush_all` on `ArcadeDBIngestor`.

The batching logic is identical to Memgraph's — same query builders, same grouping, same `MERGE_KEY_PROPS_BY_REL` splitting for issue #722. Only the execution call differs, and it is wrapped in the retry.

- [ ] **Step 1: Write the failing test**

Append to `codebase_rag/tests/test_arcadedb_ingestor.py`:

```python
def test_node_buffer_flushes_at_batch_size() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase"):
        ingestor = _ingestor(batch_size=2)
        ingestor.__enter__()
        with patch.object(ingestor, "flush_nodes") as flush:
            ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
            flush.assert_not_called()
            ingestor.ensure_node_batch("Function", {"qualified_name": "b"})
            flush.assert_called_once()
        ingestor.__exit__(None, None, None)


def test_flush_nodes_sends_an_unwound_merge() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a", "name": "a"})
        ingestor.flush_nodes()

        query = session.run.call_args[0][0]
        assert query.startswith("UNWIND $batch AS row")
        assert "MERGE (n:Function {qualified_name: row.id})" in query
        assert session.run.call_args.kwargs["batch"] == [
            {"id": "a", "props": {"name": "a"}}
        ]
        ingestor.__exit__(None, None, None)


def test_flush_relationships_splits_by_merge_key_signature() -> None:
    # Issue #722: rows carrying different distinguishing props must not
    # share a MERGE key, or parallel FLOWS_TO edges collapse into one.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([{"created": 1}])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        for via in ("arg", "ret"):
            ingestor.ensure_relationship_batch(
                ("Function", "qualified_name", "a"),
                "FLOWS_TO",
                ("Function", "qualified_name", "b"),
                {"via": via, "kind": "direct"},
            )
        ingestor.flush_relationships()

        merged = [c[0][0] for c in session.run.call_args_list]
        assert any("via: row.props.via" in q for q in merged)
        ingestor.__exit__(None, None, None)


def test_flush_retries_a_concurrent_modification_error() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.side_effect = [
            RuntimeError("Concurrent modification of record #1:0"),
            iter([]),
        ]
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
        ingestor.flush_nodes()  # must not raise

        assert session.run.call_count == 2
        ingestor.__exit__(None, None, None)


def test_flush_reraises_a_permanent_error() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.side_effect = RuntimeError("Syntax error at line 1")
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
        with pytest.raises(RuntimeError, match="Syntax error"):
            ingestor.flush_nodes()
        ingestor.__exit__(None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_ingestor.py -k "flush or buffer" -v`
Expected: FAIL with `AttributeError: 'ArcadeDBIngestor' object has no attribute 'ensure_node_batch'`

- [ ] **Step 3: Implement batching and flush**

Append to `ArcadeDBIngestor`:

```python
    def ensure_node_batch(
        self, label: str, properties: dict[str, PropertyValue]
    ) -> None:
        self.node_buffer.append((label, properties))
        if len(self.node_buffer) >= self.batch_size:
            self.flush_nodes()

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: dict[str, PropertyValue] | None = None,
    ) -> None:
        from_label, from_key, from_val = from_spec
        to_label, to_key, to_val = to_spec
        pattern = (from_label, from_key, rel_type, to_label, to_key)
        self._rel_groups[pattern].append(
            RelBatchRow(from_val=from_val, to_val=to_val, props=properties or {})
        )
        self._rel_count += 1
        if self._rel_count >= self.batch_size:
            self.flush_nodes()
            self.flush_relationships()

    def _execute_batch(
        self, query: str, rows: Sequence[BatchParams]
    ) -> list[ResultRow]:
        if not rows:
            return []

        def run() -> list[ResultRow]:
            with self._session() as session:
                result = session.run(
                    Query(wrap_with_unwind(query), timeout=settings.ARCADEDB_TX_TIMEOUT_S),
                    batch=list(rows),
                )
                return [dict(record) for record in result]

        try:
            return retry_on_transient(run, self._dialect)
        except Exception as e:
            if not self._dialect.is_benign_error(e):
                logger.error(ls.ARCADE_BATCH_ERROR.format(error=e))
                logger.error(ls.ARCADE_CYPHER_QUERY.format(query=query))
            raise
```

```python
    def _flush_node_label_group(
        self, label: str, props_list: list[dict[str, PropertyValue]]
    ) -> tuple[int, int]:
        id_key = NODE_UNIQUE_CONSTRAINTS.get(label)
        if not id_key:
            logger.warning(ls.ARCADE_NO_CONSTRAINT.format(label=label))
            return 0, len(props_list)

        rows: list[NodeBatchRow] = []
        skipped = 0
        for props in props_list:
            if id_key not in props:
                skipped += 1
                continue
            rows.append(
                NodeBatchRow(
                    id=props[id_key],
                    props={k: v for k, v in props.items() if k != id_key},
                )
            )
        if not rows:
            return 0, skipped

        build = build_merge_node_query if self._use_merge else build_create_node_query
        self._execute_batch(build(label, id_key), rows)
        return len(rows), skipped

    def _flush_rel_pattern_group(
        self,
        pattern: tuple[str, str, str, str, str],
        rows: list[RelBatchRow],
    ) -> tuple[int, int]:
        from_label, from_key, rel_type, to_label, to_key = pattern

        if not self._use_merge:
            query = build_create_relationship_query(
                from_label,
                from_key,
                rel_type,
                to_label,
                to_key,
                any(r["props"] for r in rows),
            )
            results = self._execute_batch(query, rows)
            return len(rows), sum(int(r.get("created", 0) or 0) for r in results)

        # Issue #722: rows for the same endpoints may carry different
        # distinguishing props. Flushing each merge-key signature separately
        # stops a prop absent from one row being dropped from the key for the
        # rest, which would re-collapse parallel provenance edges.
        candidate = MERGE_KEY_PROPS_BY_REL.get(rel_type, ())
        by_keys: defaultdict[tuple[str, ...], list[RelBatchRow]] = defaultdict(list)
        for row in rows:
            props = row["props"] or {}
            by_keys[tuple(p for p in candidate if p in props)].append(row)

        attempted = 0
        created = 0
        for merge_key_props, group in by_keys.items():
            query = build_merge_relationship_query(
                from_label,
                from_key,
                rel_type,
                to_label,
                to_key,
                any(r["props"] for r in group),
                merge_key_props=merge_key_props,
            )
            results = self._execute_batch(query, group)
            attempted += len(group)
            created += sum(int(r.get("created", 0) or 0) for r in results)
        return attempted, created

    def flush_nodes(self) -> None:
        if not self.node_buffer:
            return
        by_label: defaultdict[str, list[dict[str, PropertyValue]]] = defaultdict(list)
        for label, props in self.node_buffer:
            by_label[label].append(props)

        total = len(self.node_buffer)
        flushed = 0
        first_error: Exception | None = None

        # neo4j.Driver pools sessions internally, so unlike the Memgraph path
        # there is no per-group connection to create and close — each worker
        # just calls _execute_batch, which takes a session from the pool.
        if self._executor and len(by_label) > 1:
            futures = {
                self._executor.submit(self._flush_node_label_group, label, props): label
                for label, props in by_label.items()
            }
            for future in as_completed(futures):
                try:
                    count, _ = future.result()
                    flushed += count
                except Exception as e:
                    logger.error(ls.ARCADE_BATCH_ERROR.format(error=e))
                    first_error = first_error or e
        else:
            for label, props in by_label.items():
                try:
                    count, _ = self._flush_node_label_group(label, props)
                    flushed += count
                except Exception as e:
                    logger.error(ls.ARCADE_BATCH_ERROR.format(error=e))
                    first_error = first_error or e

        logger.info(ls.ARCADE_NODES_FLUSHED.format(flushed=flushed, total=total))
        self.node_buffer.clear()
        if first_error is not None:
            raise first_error

    def flush_relationships(self) -> None:
        if not self._rel_count:
            return
        total = self._rel_count
        attempted = 0
        created = 0
        first_error: Exception | None = None

        if self._executor and len(self._rel_groups) > 1:
            futures = {
                self._executor.submit(
                    self._flush_rel_pattern_group, pattern, rows
                ): pattern
                for pattern, rows in self._rel_groups.items()
            }
            for future in as_completed(futures):
                try:
                    a, c = future.result()
                    attempted += a
                    created += c
                except Exception as e:
                    logger.error(ls.ARCADE_BATCH_ERROR.format(error=e))
                    first_error = first_error or e
        else:
            for pattern, rows in self._rel_groups.items():
                try:
                    a, c = self._flush_rel_pattern_group(pattern, rows)
                    attempted += a
                    created += c
                except Exception as e:
                    logger.error(ls.ARCADE_BATCH_ERROR.format(error=e))
                    first_error = first_error or e

        logger.info(
            ls.ARCADE_RELS_FLUSHED.format(
                total=total, success=created, failed=attempted - created
            )
        )
        self._rel_count = 0
        self._rel_groups.clear()
        if first_error is not None:
            raise first_error

    def flush_all(self) -> None:
        self.flush_nodes()
        self.flush_relationships()
```

Add to the module imports: `from concurrent.futures import ThreadPoolExecutor, as_completed` and `from collections.abc import Generator, Sequence`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_ingestor.py -v && uv run pytest -n auto -m "not integration"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add codebase_rag/services/graph/arcadedb.py codebase_rag/tests/test_arcadedb_ingestor.py
git commit -m "feat: add ArcadeDB batching, parallel flush, and transient-conflict retry"
```

---

### Task 13: ArcadeDB admin operations

**Files:**
- Modify: `codebase_rag/services/graph/arcadedb.py`
- Modify: `codebase_rag/tests/test_arcadedb_ingestor.py`

**Interfaces:**
- Consumes: `CYPHER_DELETE_ALL`, `CYPHER_LIST_PROJECTS`, `CYPHER_DELETE_PROJECT`, `CYPHER_EXPORT_NODES`, `CYPHER_EXPORT_RELATIONSHIPS`, `CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES` from the shared query modules; `prune_unanchored_resources` from `services/resource_cleanup.py`; `project_roots_from_rows` from `utils/path_utils.py`
- Produces: `clean_database`, `list_projects`, `list_project_roots`, `delete_project`, `export_graph_to_dict` on `ArcadeDBIngestor`.

Every one of these runs the *shared* Cypher unchanged. `delete_project`'s `OPTIONAL MATCH` over variable-length paths with a multi-variable `DETACH DELETE` was verified working on ArcadeDB 26.8.1.

- [ ] **Step 1: Write the failing test**

Append to `codebase_rag/tests/test_arcadedb_ingestor.py`:

```python
def test_admin_operations_use_the_shared_cypher() -> None:
    from codebase_rag.cypher_queries import CYPHER_DELETE_ALL, CYPHER_LIST_PROJECTS

    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.clean_database()
        assert session.run.call_args[0][0] == CYPHER_DELETE_ALL

        session.run.return_value = iter([])
        ingestor.list_projects()
        assert CYPHER_LIST_PROJECTS in session.run.call_args[0][0]
        ingestor.__exit__(None, None, None)


def test_delete_project_also_prunes_shared_nodes() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.delete_project("alpha")

        sent = [c[0][0] for c in session.run.call_args_list]
        assert any("Resource" in q for q in sent)
        assert any("ExternalModule" in q for q in sent)
        ingestor.__exit__(None, None, None)


def test_export_graph_to_dict_reports_counts() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.side_effect = [
            iter([{"node_id": 1, "labels": ["Function"], "properties": {}}]),
            iter([]),
        ]
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        data = ingestor.export_graph_to_dict()
        assert data["metadata"]["total_nodes"] == 1
        assert data["metadata"]["total_relationships"] == 0
        ingestor.__exit__(None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_ingestor.py -k admin -v`
Expected: FAIL with `AttributeError: 'ArcadeDBIngestor' object has no attribute 'clean_database'`

- [ ] **Step 3: Implement the admin operations**

Append to `ArcadeDBIngestor` — bodies identical to `MemgraphIngestor`'s, since they only call `fetch_all`/`execute_write`:

```python
    def clean_database(self) -> None:
        logger.info(ls.ARCADE_CLEANING_DB)
        # DETACH DELETE clears records but leaves ArcadeDB's type definitions
        # behind. That is fine: ensure_schema is idempotent and reuses them.
        self.execute_write(CYPHER_DELETE_ALL)
        logger.info(ls.ARCADE_DB_CLEANED)

    def list_projects(self) -> list[str]:
        return [str(r[KEY_NAME]) for r in self.fetch_all(CYPHER_LIST_PROJECTS)]

    def list_project_roots(self) -> dict[str, str | None]:
        return project_roots_from_rows(self.fetch_all(CYPHER_LIST_PROJECTS))

    def delete_project(self, project_name: str) -> None:
        logger.info(ls.ARCADE_DELETING_PROJECT.format(project_name=project_name))
        self.execute_write(CYPHER_DELETE_PROJECT, {KEY_PROJECT_NAME: project_name})
        # Shared prefix-less nodes (Resources, ExternalModules) only lose
        # their edges above; drop the ones this project alone anchored.
        prune_unanchored_resources(self)
        self.execute_write(CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
        logger.info(ls.ARCADE_PROJECT_DELETED.format(project_name=project_name))

    def export_graph_to_dict(self) -> GraphData:
        nodes_data = self.fetch_all(CYPHER_EXPORT_NODES)
        relationships_data = self.fetch_all(CYPHER_EXPORT_RELATIONSHIPS)
        return GraphData(
            nodes=nodes_data,
            relationships=relationships_data,
            metadata=GraphMetadata(
                total_nodes=len(nodes_data),
                total_relationships=len(relationships_data),
                exported_at=datetime.now(UTC).isoformat(),
            ),
        )
```

`_migrate_legacy_path_keys` has no ArcadeDB counterpart. It exists to retire Memgraph constraints written by the superseded issue-#897 key, and a fresh ArcadeDB database has none. Do not port it.

- [ ] **Step 4: Run tests**

Run: `uv run pytest codebase_rag/tests/test_arcadedb_ingestor.py -v && make lint && make typecheck`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add codebase_rag/services/graph/arcadedb.py codebase_rag/tests/test_arcadedb_ingestor.py
git commit -m "feat: add ArcadeDB admin operations on the shared Cypher"
```

---

### Task 14: Turn on ArcadeDB in the conformance suite

**Files:**
- Modify: `codebase_rag/tests/integration/conftest.py` (`BACKENDS`, `_start_arcadedb`)
- Modify: `codebase_rag/services/graph/__init__.py`

**Interfaces:**
- Consumes: `ArcadeDBIngestor` (Tasks 11-13)
- Produces: `BACKENDS = (GraphBackend.MEMGRAPH, GraphBackend.ARCADEDB)`; `_start_arcadedb() -> tuple[object, GraphContainer]`; `ARCADEDB_TEST_DB`.

This is the phase's acceptance gate: the conformance suite and corpus gate written in phase 2 now run unchanged against ArcadeDB.

- [ ] **Step 1: Add the ArcadeDB container fixture**

In `codebase_rag/tests/integration/conftest.py`:

```python
ARCADEDB_IMAGE = "arcadedata/arcadedb:26.8.1"
ARCADEDB_TEST_DB = "cgrtest"
ARCADEDB_ROOT_PASSWORD = "cgrtestpassword1!"  # noqa: S105 - throwaway container
ARCADEDB_READY_LOG = "ArcadeDB Server started"
ARCADEDB_BOLT_PLUGIN = "Bolt:com.arcadedb.bolt.BoltProtocolPlugin"


def _start_arcadedb() -> tuple[object, GraphContainer]:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = DockerContainer(ARCADEDB_IMAGE)
    container.with_exposed_ports(7687, 2480)
    container.with_env("ARCADEDB_ROOT_PASSWORD", ARCADEDB_ROOT_PASSWORD)
    # The Bolt listener is a plugin and is off unless explicitly enabled.
    container.with_env(
        "JAVA_OPTS",
        f"-Darcadedb.server.plugins={ARCADEDB_BOLT_PLUGIN} "
        f"-Darcadedb.server.defaultDatabases={ARCADEDB_TEST_DB}[root:{ARCADEDB_ROOT_PASSWORD}]",
    )
    container.waiting_for(LogMessageWaitStrategy(ARCADEDB_READY_LOG))
    container.start()

    host = container.get_container_host_ip()
    bolt_port = int(container.get_exposed_port(7687))
    http_port = int(container.get_exposed_port(2480))
    _wait_for_port(host, bolt_port)
    _wait_for_port(host, http_port)
    return container, GraphContainer(
        backend=GraphBackend.ARCADEDB,
        host=host,
        bolt_port=bolt_port,
        http_port=http_port,
        username="root",
        password=ARCADEDB_ROOT_PASSWORD,
    )
```

Change `BACKENDS` to `(GraphBackend.MEMGRAPH, GraphBackend.ARCADEDB)` and remove the `NotImplementedError` placeholder from `graph_container`.

The `graph_ingestor` fixture must call `ensure_constraints()` after `__enter__` on ArcadeDB — unlike Memgraph, MERGE without the unique index is a full type scan and the conformance suite's idempotency assertions would silently pass by luck on tiny data while ingestion of a real repo degrades to quadratic. Add it for both backends; it is idempotent and cheap.

- [ ] **Step 2: Run the conformance suite on both backends**

Run: `uv run pytest codebase_rag/tests/integration/test_graph_backend_conformance.py -v`
Expected: every test id appears twice, `[memgraph]` and `[arcadedb]`, all passing.

Failures here are the real findings of this project. Fix the ArcadeDB implementation, not the assertion — the suite is the contract. The one exception: if a conformance test encodes a Memgraph-specific assumption the spec did not intend, correct the test and record why in the commit message.

- [ ] **Step 3: Run the corpus gate on both backends**

Run: `uv run pytest codebase_rag/tests/integration/test_query_corpus.py -v`
Expected: every query passes on both.

A query that parses on Memgraph but not ArcadeDB belongs in the dialect. Do not fork the corpus.

- [ ] **Step 4: Commit**

```bash
git add codebase_rag/tests/integration/conftest.py codebase_rag/services/graph/__init__.py
git commit -m "test: run the conformance suite and corpus gate against ArcadeDB"
```

---

## Phase 5 — The real procedure catalog

### Task 15: Enumerate ArcadeDB's Cypher procedure surface and write the catalog

**Files:**
- Create: `scripts/probe_arcade_procedures.py`
- Modify: `codebase_rag/constants/graph.py` (`ARCADE_PROCEDURE_CATALOG`)
- Modify: `codebase_rag/tests/test_arcadedb_dialect.py`

**Interfaces:**
- Consumes: a running ArcadeDB server (the integration container, or a local one)
- Produces: a verified `ARCADE_PROCEDURE_CATALOG` string.

ArcadeDB documents only the Java `GraphAlgorithms` API; the Cypher `CALL` surface is undocumented, so it cannot be written from the docs. Probing established that `algo.pageRank`, `algo.scc`, `algo.wcc` and `algo.louvain` resolve while `algo.betweennessCentrality` does not — the marketing algorithm list is not the callable list.

- [ ] **Step 1: Write the probe script**

Create `scripts/probe_arcade_procedures.py`:

```python
"""Enumerate which algo.* procedures ArcadeDB actually exposes over Cypher.

ArcadeDB publishes an algorithm list but does not document the Cypher CALL
surface, and the two do not match. Run this against a live server and paste
the surviving names into ARCADE_PROCEDURE_CATALOG.

Usage:
    uv run python scripts/probe_arcade_procedures.py \
        --uri bolt://localhost:7687 --user root --password pw --database cgrtest
"""

from __future__ import annotations

import argparse

from neo4j import GraphDatabase, Query

CANDIDATES = [
    "algo.pageRank",
    "algo.articleRank",
    "algo.personalizedPageRank",
    "algo.betweenness",
    "algo.betweennessCentrality",
    "algo.closeness",
    "algo.closenessCentrality",
    "algo.harmonicCentrality",
    "algo.eigenvectorCentrality",
    "algo.degreeCentrality",
    "algo.katzCentrality",
    "algo.hits",
    "algo.eccentricity",
    "algo.wcc",
    "algo.scc",
    "algo.louvain",
    "algo.leiden",
    "algo.labelPropagation",
    "algo.slpa",
    "algo.triangleCount",
    "algo.localClusteringCoefficient",
    "algo.shortestPath",
    "algo.allShortestPaths",
    "algo.dijkstra",
    "algo.astar",
    "algo.bellmanFord",
    "algo.yens",
    "algo.kShortestPaths",
    "algo.allSimplePaths",
    "algo.floydWarshall",
    "algo.longestPath",
    "algo.bfs",
    "algo.topologicalSort",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    available: list[str] = []
    with driver.session(database=args.database) as session:
        for name in CANDIDATES:
            try:
                result = session.run(f"CALL {name}() YIELD node RETURN node LIMIT 1")
                list(result)
                available.append(name)
                print(f"OK      {name}")
            except Exception as e:
                head = str(e).splitlines()[0][:90]
                print(f"MISSING {name}  ({head})")
    driver.close()

    print("\nAvailable:")
    for name in available:
        print(f"  {name}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Start a server and run the probe**

```bash
docker run -d --name cgr-arcade-probe \
  -p 2480:2480 -p 7687:7687 \
  -e ARCADEDB_ROOT_PASSWORD='cgrtestpassword1!' \
  -e JAVA_OPTS='-Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin -Darcadedb.server.defaultDatabases=cgrtest[root:cgrtestpassword1!]' \
  arcadedata/arcadedb:26.8.1

sleep 20
uv run python scripts/probe_arcade_procedures.py \
  --uri bolt://localhost:7687 --user root --password 'cgrtestpassword1!' --database cgrtest
```

Some procedures need a non-empty graph or arguments and will report MISSING for that reason rather than absence. For each MISSING whose message is *not* "Unknown procedure/function", seed two connected vertices and retry that one before concluding it is unavailable.

- [ ] **Step 3: Determine each available procedure's YIELD columns**

For every name the probe accepted, discover its real output columns:

```bash
uv run python - <<'PY'
from neo4j import GraphDatabase, Query
driver = GraphDatabase.driver("bolt://localhost:7687", auth=("root", "cgrtestpassword1!"))
with driver.session(database="cgrtest") as s:
    s.run("CREATE (a:Probe {k:'a'})-[:LINK]->(b:Probe {k:'b'})")
    for name in ["algo.pageRank", "algo.scc", "algo.wcc", "algo.louvain"]:
        try:
            rows = list(s.run(f"CALL {name}()"))
            print(name, "->", list(rows[0].keys()) if rows else "[] (no rows)")
        except Exception as e:
            print(name, "ERR", str(e).splitlines()[0][:80])
    s.run("MATCH (n:Probe) DETACH DELETE n")
driver.close()
PY
```

- [ ] **Step 4: Write the verified catalog**

Replace `ARCADE_PROCEDURE_CATALOG` in `codebase_rag/constants/graph.py` with one bullet per *confirmed* procedure, using its *observed* YIELD columns. Do not list anything the probe did not accept. Keep the RID warning paragraph — it was verified: `algo.pageRank` yields `node` as the string `"#46:0"`, not a node object.

- [ ] **Step 5: Pin the findings in a test**

Replace the placeholder assertions in `codebase_rag/tests/test_arcadedb_dialect.py`:

```python
def test_procedure_catalog_lists_only_verified_procedures() -> None:
    catalog = ArcadeDBDialect().procedure_catalog
    # Every procedure named here was accepted by scripts/probe_arcade_procedures.py
    # against ArcadeDB 26.8.1. Re-run that script before adding to this list.
    for confirmed in VERIFIED_ARCADE_PROCEDURES:
        assert confirmed in catalog


def test_procedure_catalog_excludes_mage_namespaces() -> None:
    catalog = ArcadeDBDialect().procedure_catalog
    for absent in ("nxalg.", "pagerank.get", "graph_util.", "path.expand", "wcc.get"):
        assert absent not in catalog


def test_procedure_catalog_warns_that_node_is_a_rid_string() -> None:
    # Without this the model writes node.qualified_name and gets nothing.
    assert "record-id string" in ArcadeDBDialect().procedure_catalog
```

Add `VERIFIED_ARCADE_PROCEDURES: tuple[str, ...]` to `constants/graph.py` holding the confirmed names, and build the catalog text from it so the two cannot drift.

- [ ] **Step 6: Clean up and commit**

```bash
docker rm -f cgr-arcade-probe
uv run pytest codebase_rag/tests/test_arcadedb_dialect.py -v
git add scripts/probe_arcade_procedures.py codebase_rag/constants/graph.py codebase_rag/tests/test_arcadedb_dialect.py
git commit -m "feat: write the ArcadeDB procedure catalog from a live server probe"
```

---
## Phase 6 — Parametrise the suite, ship the ops surface

### Task 16: Migrate the 30 integration modules onto the backend-parametrised fixture

**Files:**
- Modify: 30 files under `codebase_rag/tests/integration/` (fixture rename)
- Modify: `codebase_rag/tests/integration/conftest.py` (drop the alias)

**Interfaces:**
- Consumes: `graph_ingestor` (Task 5), both backends live (Task 14)
- Produces: the whole integration suite running on both engines.

The `memgraph_ingestor` alias added in Task 5 already makes these modules run against both backends — the fixture it delegates to is parametrised. This task removes the misleading name.

- [ ] **Step 1: Confirm the suite already runs on both backends**

Run: `uv run pytest -m integration -v --collect-only | grep -c "arcadedb"`
Expected: a non-zero count. If it is zero, `BACKENDS` was not updated in Task 14 — stop and fix that first.

- [ ] **Step 2: Rename the fixture across the suite**

```bash
grep -rl "memgraph_ingestor" codebase_rag/tests/integration/ | while read -r f; do
  python3 - "$f" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
s = s.replace("memgraph_ingestor", "graph_ingestor")
s = s.replace(
    "from codebase_rag.services.graph_service import MemgraphIngestor",
    "from codebase_rag.services.graph import GraphIngestor",
)
s = s.replace("MemgraphIngestor", "GraphIngestor")
p.write_text(s)
PY
done
grep -rn "memgraph_ingestor\|MemgraphIngestor" codebase_rag/tests/integration/ || echo "all migrated"
```

**Correction found during Task 5:** the plan previously claimed `test_flow_edges_e2e.py` consumes the raw `memgraph_connection` fixture. It does not — no test in the repo requests it. The fixture is defined in `conftest.py` and never used; the original grep that produced this claim counted the definition itself. Delete the dead `memgraph_connection` fixture in this task rather than preserving it, and confirm with `grep -rn "memgraph_connection" codebase_rag/` that the only remaining hits are the unrelated `check_memgraph_connection` method in `tools/health_checker.py` and its test.

- [ ] **Step 3: Delete the deprecated alias**

Remove the `memgraph_ingestor` fixture from `conftest.py`.

- [ ] **Step 4: Run the full integration suite on both backends**

Run: `uv run pytest -m integration -n auto -v`
Expected: PASS. Each test appears twice.

Expect real failures here — this is the first time the parser end-to-end paths (flow edges, endpoint linking, cross-project folder identity) run against ArcadeDB. Each is a genuine finding. Fix the backend, not the test.

- [ ] **Step 5: Verify the xdist grouping actually parallelises**

```bash
time uv run pytest -m integration -n 4 -q
```

Confirm the two backend groups land on different workers — wall clock should be materially below the sum of two serial runs. If both groups land on one worker, `_backend_of` is not matching the parametrised test ids; fix the id parsing before accepting the runtime cost.

- [ ] **Step 6: Commit**

```bash
git add codebase_rag/tests/integration/
git commit -m "test: run the full integration suite against both graph backends"
```

---

### Task 17: Compose profiles, stack manager, and health checks

**Files:**
- Modify: `codebase_rag/docker-compose.yaml`
- Modify: `codebase_rag/stack/constants.py`, `stack/health.py`, `stack/manager.py`, `stack/cli.py`
- Modify: `codebase_rag/tools/health_checker.py`, `codebase_rag/constants/health.py`
- Test: `codebase_rag/tests/test_stack_backend.py`

**Interfaces:**
- Consumes: `GraphBackend`, `settings.GRAPH_BACKEND`, `has_neo4j_driver` (Task 3)
- Produces: `wait_for_graph(backend, host, bolt_port, http_port) -> bool`; `StackStatus.graph_reachable` / `.graph_endpoint`; `HealthChecker.check_graph_connection()`; compose profiles `memgraph` (default) and `arcadedb`.

- [ ] **Step 1: Write the failing test**

Create `codebase_rag/tests/test_stack_backend.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.stack import constants as sc
from codebase_rag.stack.health import wait_for_graph


def test_compose_defines_both_engines_under_profiles() -> None:
    compose = (Path("codebase_rag") / "docker-compose.yaml").read_text()
    assert "arcadedata/arcadedb" in compose
    assert "BoltProtocolPlugin" in compose
    # Both default to 7687, so neither may start unprofiled.
    assert compose.count("profiles:") >= 2


def test_service_names_cover_both_backends() -> None:
    assert sc.SERVICE_MEMGRAPH == "memgraph"
    assert sc.SERVICE_ARCADEDB == "arcadedb"


def test_wait_for_graph_probes_bolt_for_memgraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "codebase_rag.stack.health._bolt_reachable",
        lambda h, p: seen.append((h, p)) or True,
    )
    assert wait_for_graph(GraphBackend.MEMGRAPH, "h", 7687, None, timeout=1.0)
    assert seen == [("h", 7687)]


def test_wait_for_graph_requires_both_ports_for_arcadedb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live Bolt listener with a dead HTTP endpoint would pass startup and
    # then fail at ensure_constraints(), so both must be probed.
    monkeypatch.setattr(
        "codebase_rag.stack.health._arcade_bolt_reachable", lambda h, p: True
    )
    monkeypatch.setattr("codebase_rag.stack.health._http_reachable", lambda url: False)
    assert not wait_for_graph(GraphBackend.ARCADEDB, "h", 7687, 2480, timeout=1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest codebase_rag/tests/test_stack_backend.py -v`
Expected: FAIL with `ImportError: cannot import name 'wait_for_graph'`

- [ ] **Step 3: Add the compose profiles**

In `codebase_rag/docker-compose.yaml`, add `profiles: ["memgraph"]` to the existing `memgraph` and `lab` services and append:

```yaml
  arcadedb:
    image: arcadedata/arcadedb
    profiles: ["arcadedb"]
    environment:
      ARCADEDB_ROOT_PASSWORD: ${ARCADEDB_PASSWORD}
      JAVA_OPTS: >-
        -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin
        -Darcadedb.server.defaultDatabases=${ARCADEDB_DATABASE:-codegraph}[root:${ARCADEDB_PASSWORD}]
    ports:
      - "${CGR_STACK_BIND_HOST:-127.0.0.1}:${ARCADEDB_BOLT_PORT:-7687}:7687"
      - "${CGR_STACK_BIND_HOST:-127.0.0.1}:${ARCADEDB_HTTP_PORT:-2480}:2480"
    volumes:
      - arcadedb_data:/home/arcadedb/databases
```

Add `arcadedb_data:` to the `volumes:` block. Keep the `CGR_STACK_BIND_HOST` loopback default — issue #1012 exists because these services are unauthenticated on the network, and ArcadeDB's data is exactly as sensitive.

`docker compose --profile memgraph up` must remain what `cgr daemon up` runs by default, so existing users see no change.

- [ ] **Step 4: Make the stack backend-aware**

In `stack/constants.py` add `SERVICE_ARCADEDB = "arcadedb"` and change `MSG_STACK_HEALTHY` from `"Stack is healthy ({memgraph}, {qdrant})."` to `"Stack is healthy ({graph}, {qdrant})."`.

In `stack/health.py`:

```python
def _arcade_bolt_reachable(host: str, port: int) -> bool:
    from neo4j import GraphDatabase, Query
    from codebase_rag.config import settings

    try:
        driver = GraphDatabase.driver(
            f"bolt://{host}:{port}",
            auth=(settings.ARCADEDB_USERNAME or "", settings.ARCADEDB_PASSWORD or ""),
        )
        try:
            driver.verify_connectivity()
        finally:
            driver.close()
        return True
    except Exception:
        return False


def wait_for_graph(
    backend: GraphBackend,
    host: str,
    bolt_port: int,
    http_port: int | None,
    timeout: float = cs.DEFAULT_HEALTH_TIMEOUT_S,
    interval: float = cs.DEFAULT_HEALTH_INTERVAL_S,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend == GraphBackend.ARCADEDB:
            # Both transports must answer: schema DDL goes over HTTP, so a
            # live Bolt listener alone would pass startup then fail at
            # ensure_constraints().
            if _arcade_bolt_reachable(host, bolt_port) and _http_reachable(
                f"http://{cs.LOOPBACK_HOST}:{http_port}/api/v1/ready"
            ):
                return True
        elif _bolt_reachable(host, bolt_port):
            return True
        time.sleep(interval)
    return False
```

Keep `wait_for_memgraph` as a thin wrapper delegating to `wait_for_graph(GraphBackend.MEMGRAPH, ...)` so `stack/manager.py` can migrate in one edit.

Rename `StackStatus.memgraph_reachable` → `graph_reachable` and `.memgraph_endpoint` → `graph_endpoint` in `stack/manager.py`, and update the one `stack/cli.py` echo line. These are internal names with no `.env` exposure.

- [ ] **Step 5: Make the health checker backend-aware**

In `constants/health.py`, rename `HEALTH_CHECK_MEMGRAPH_*` to `HEALTH_CHECK_GRAPH_*` and change the literals to name the backend by substitution: `HEALTH_CHECK_GRAPH_SUCCESSFUL = "{backend} connection successful"`.

In `tools/health_checker.py`, replace `check_memgraph_connection` with `check_graph_connection`, which builds the ingestor via `get_ingestor()` and runs `HEALTH_CHECK_GRAPH_QUERY` (`"RETURN 1 AS test;"` — valid on both engines) through `fetch_all`, rather than importing `mgclient` directly. Update `codebase_rag/tests/test_health_checker.py` to patch `codebase_rag.services.graph.memgraph.mgclient` (already done in Task 1) and to exercise the new name.

- [ ] **Step 6: Run tests**

Run: `uv run pytest codebase_rag/tests/test_stack_backend.py codebase_rag/tests/test_health_checker.py codebase_rag/tests/test_stack_manager.py -v && uv run pytest -n auto -m "not integration"`
Expected: PASS.

- [ ] **Step 7: Verify both stacks actually start**

```bash
docker compose -f codebase_rag/docker-compose.yaml --profile memgraph up -d
uv run cgr daemon status
docker compose -f codebase_rag/docker-compose.yaml --profile memgraph down

ARCADEDB_PASSWORD='cgrtestpassword1!' docker compose -f codebase_rag/docker-compose.yaml --profile arcadedb up -d
GRAPH_BACKEND=arcadedb ARCADEDB_PASSWORD='cgrtestpassword1!' uv run cgr daemon status
docker compose -f codebase_rag/docker-compose.yaml --profile arcadedb down
```

Expected: each reports its own engine healthy. Confirm the two are never up simultaneously — they share port 7687.

- [ ] **Step 8: Commit**

```bash
git add codebase_rag/docker-compose.yaml codebase_rag/stack/ codebase_rag/tools/health_checker.py codebase_rag/constants/health.py codebase_rag/tests/
git commit -m "feat: add compose profiles and backend-aware stack health checks"
```

---

### Task 18: End-to-end verification and documentation

**Files:**
- Modify: `.env.example`
- Modify: `README.md`, `PYPI_README.md`, `docs/getting-started/`, `docs/architecture/`
- Modify: `NEWS.md`
- Modify: `Makefile`

**Interfaces:**
- Consumes: everything
- Produces: the shipped, documented feature.

- [ ] **Step 1: Index a real repository on ArcadeDB**

This is the spec's acceptance criterion that no test covers.

```bash
ARCADEDB_PASSWORD='cgrtestpassword1!' docker compose -f codebase_rag/docker-compose.yaml --profile arcadedb up -d
sleep 20

export GRAPH_BACKEND=arcadedb
export ARCADEDB_PASSWORD='cgrtestpassword1!'
export ARCADEDB_USERNAME=root
export ARCADEDB_DATABASE=codegraph

uv run cgr index . --clean
uv run cgr stats
uv run cgr dead-code --project code-graph-rag | head -30
```

Expected: node and relationship counts within a few percent of the same commands run against Memgraph. Record both numbers in the commit message. A large discrepancy means edges are being dropped — investigate before shipping, do not document around it.

- [ ] **Step 2: Compare against Memgraph on the same commit**

```bash
docker compose -f codebase_rag/docker-compose.yaml --profile arcadedb down
docker compose -f codebase_rag/docker-compose.yaml --profile memgraph up -d
sleep 15
unset GRAPH_BACKEND
uv run cgr index . --clean && uv run cgr stats
```

Diff the two `cgr stats` outputs. Any label or relationship type whose count differs is a real behavioural divergence and needs a conformance test before this ships.

- [ ] **Step 3: Confirm the default path is untouched**

```bash
unset GRAPH_BACKEND ARCADEDB_PASSWORD ARCADEDB_USERNAME ARCADEDB_DATABASE
uv run pytest -n auto
git stash list  # must be empty; no local config should be needed
```

Expected: the full suite green with no ArcadeDB environment set at all.

- [ ] **Step 4: Document the settings**

In `.env.example`, after the `MEMGRAPH_*` block:

```bash
# --- Graph backend -----------------------------------------------------
# Which graph engine stores the code graph: "memgraph" (default) or "arcadedb".
GRAPH_BACKEND=memgraph

# ArcadeDB settings, used only when GRAPH_BACKEND=arcadedb.
# Credentials are REQUIRED: ArcadeDB's Bolt listener rejects unauthenticated
# connections. Install the driver with: pip install 'code-graph-rag[arcadedb]'
# ARCADEDB_HOST=localhost
# ARCADEDB_BOLT_PORT=7687
# ARCADEDB_HTTP_PORT=2480
# ARCADEDB_USERNAME=root
# ARCADEDB_PASSWORD=
# ARCADEDB_DATABASE=codegraph
```

- [ ] **Step 5: Add the Makefile targets**

```makefile
test-integration-memgraph: ## Run integration tests against Memgraph only
	$(PYTHON) pytest -m "integration" -k memgraph -v

test-integration-arcadedb: ## Run integration tests against ArcadeDB only
	$(PYTHON) pytest -m "integration" -k arcadedb -v
```

Add both to the `.PHONY` line.

- [ ] **Step 6: Update the prose docs**

`README.md` line 1 of the description currently reads "builds a knowledge graph of its structure in Memgraph". Change to "builds a knowledge graph of its structure in Memgraph or ArcadeDB". Mirror in `PYPI_README.md`.

Add a "Choosing a graph backend" section to `docs/getting-started/` covering: the `GRAPH_BACKEND` setting, the `arcadedb` extra, the mandatory credentials, the compose profile, and the port-7687 collision. State plainly that Memgraph is the default and the better-tested path.

In `docs/architecture/`, document the `GraphIngestor` / `GraphDialect` seam and the six dialect members, so the next backend has a map.

Add to `NEWS.md` under a new heading:

```markdown
- **ArcadeDB Backend**: The code graph can now be stored in ArcadeDB as well as Memgraph, selected with `GRAPH_BACKEND=arcadedb`. Both engines run the same conformance suite on every change. Memgraph remains the default.
```

Then regenerate the README sections the pre-commit hook owns: `make readme`.

- [ ] **Step 7: Full verification**

Run: `make lint && make typecheck && uv run pytest -n auto`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
docker compose -f codebase_rag/docker-compose.yaml --profile memgraph down
git add .env.example README.md PYPI_README.md NEWS.md Makefile docs/
git commit -m "docs: document the ArcadeDB graph backend and its configuration"
```

---

## Acceptance Checklist

Run before declaring the feature done:

- [ ] `uv run pytest -n auto` green with no graph environment variables set
- [ ] `uv run pytest -m integration -v` shows every test twice, `[memgraph]` and `[arcadedb]`, all passing
- [ ] `test_graph_backend_conformance.py` green on both backends with zero backend conditionals in the file
- [ ] `test_query_corpus.py` green on both backends with a single shared corpus
- [ ] `cgr index` on a real repository produces matching `cgr stats` counts on both backends
- [ ] `cgr.MemgraphIngestor` still imports and is still in `cgr.__all__`
- [ ] `.env` files with only `MEMGRAPH_*` keys work unchanged
- [ ] `make lint && make typecheck` clean
- [ ] `ARCADE_PROCEDURE_CATALOG` contains only procedures the probe script confirmed

## Deferred

Named in the spec as out of scope; do not let them creep into this work.

- Consolidating `vector_store.py` onto ArcadeDB's native vector index
- Flipping the default backend to ArcadeDB
- Removing the Memgraph path
- Behavioural parity for `algo.*` algorithms the prompt does not reference
- Natural-language-to-Cypher quality measurement in CI (opt-in script only)
