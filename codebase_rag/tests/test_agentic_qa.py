from pathlib import Path

from evals.agentic_qa import (
    build_cases,
    build_multihop_cases,
    extract_answer_files,
    grade_answer_files,
    latency_percentile,
)

# helper() is called from two files and defined in one; the definition file
# also calls it, so it belongs to the expected answer set.
_CORE = """\
def long_helper_name():
    return 1


def caller():
    return long_helper_name()
"""

_USER = """\
from core import long_helper_name


def run():
    return long_helper_name()
"""

_IDLE = """\
VALUE = 42
"""


def _write_fixture(root: Path) -> None:
    (root / "core.py").write_text(_CORE)
    (root / "user.py").write_text(_USER)
    (root / "idle.py").write_text(_IDLE)


def test_build_cases_derives_ground_truth_from_oracle(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    cases = build_cases(tmp_path, sample=10)
    assert [c.name for c in cases] == ["long_helper_name"]
    (case,) = cases
    assert case.expected_files == frozenset({"core.py", "user.py"})
    assert "`long_helper_name`" in case.question


def test_build_cases_is_deterministic(tmp_path: Path) -> None:
    # More candidates than the sample size, so rng.sample actually runs and
    # the assertion exercises the seeded-sampling path, not just dict order.
    for i in range(4):
        (tmp_path / f"mod{i}.py").write_text(
            f"def unique_callable_{i}():\n    return {i}\n"
        )
        (tmp_path / f"caller_a{i}.py").write_text(
            f"from mod{i} import unique_callable_{i}\n\n"
            f"def go():\n    return unique_callable_{i}()\n"
        )
        (tmp_path / f"caller_b{i}.py").write_text(
            f"from mod{i} import unique_callable_{i}\n\n"
            f"def run():\n    return unique_callable_{i}()\n"
        )
    first = build_cases(tmp_path, sample=2, seed=7)
    second = build_cases(tmp_path, sample=2, seed=7)
    assert len(first) == 2
    assert first == second


def test_build_multihop_cases_walks_one_extra_hop(tmp_path: Path) -> None:
    # core.py and user.py call long_helper_name directly; second.py calls
    # wrapper_function (unique, defined in core.py, calls long_helper_name),
    # so the depth-2 answer adds second.py to the direct caller files.
    (tmp_path / "core.py").write_text(
        "def long_helper_name():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def wrapper_function():\n"
        "    return long_helper_name()\n"
    )
    (tmp_path / "user.py").write_text(
        "from core import long_helper_name\n"
        "\n"
        "\n"
        "def run_once():\n"
        "    return long_helper_name()\n"
    )
    (tmp_path / "second.py").write_text(
        "from core import wrapper_function\n\nprint(wrapper_function())\n"
    )
    cases = build_multihop_cases(tmp_path, sample=10)
    assert [c.name for c in cases] == ["long_helper_name"]
    (case,) = cases
    assert case.expected_files == frozenset({"core.py", "user.py", "second.py"})
    assert "directly calls `long_helper_name`" in case.question


def test_build_multihop_cases_drops_ambiguous_intermediates(tmp_path: Path) -> None:
    # The enclosing function `handle_event` is defined twice, so "callers of
    # the intermediate" is ambiguous under name matching → no case emitted.
    (tmp_path / "core.py").write_text(
        "def long_helper_name():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def handle_event():\n"
        "    return long_helper_name()\n"
    )
    (tmp_path / "user.py").write_text(
        "from core import long_helper_name\n"
        "\n"
        "\n"
        "def handle_event():\n"
        "    return long_helper_name()\n"
        "\n"
        "\n"
        "def other_entrypoint():\n"
        "    return handle_event()\n"
    )
    assert build_multihop_cases(tmp_path, sample=10) == []


def test_extract_answer_files_normalizes_paths() -> None:
    answer = (
        "The callers are:\n- ./db/models/query.py\n- django/apps/config.py\nplus prose."
    )
    files = extract_answer_files(answer, expected_root="django")
    assert files == frozenset({"db/models/query.py", "apps/config.py"})


def test_grade_scores_partial_and_exact() -> None:
    expected = frozenset({"a.py", "b.py"})
    partial = grade_answer_files(frozenset({"a.py", "c.py"}), expected)
    assert partial["f1"] == 0.5
    assert not partial["exact"]
    perfect = grade_answer_files(expected, expected)
    assert perfect["f1"] == 1.0
    assert perfect["exact"]
    empty = grade_answer_files(frozenset(), expected)
    assert empty["f1"] == 0.0


def test_percentile_bounds() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert latency_percentile(values, 50) == 3.0
    assert latency_percentile(values, 95) == 4.0
    assert latency_percentile([], 95) == 0.0


def test_summarize_aggregates_metrics() -> None:
    from evals.agentic_qa import summarize_qa_records
    from evals.types_defs import QARecord

    def rec(f1: float, exact: bool, seconds: float, err: str = "") -> QARecord:
        return QARecord(
            condition="grep",
            name="x",
            f1=f1,
            exact=exact,
            seconds=seconds,
            input_tokens=100,
            output_tokens=10,
            answered=[],
            expected=[],
            error=err,
        )

    summary = summarize_qa_records([rec(1.0, True, 2.0), rec(0.5, False, 4.0, "boom")])
    assert summary["questions"] == 2
    assert summary["mean_f1"] == 0.75
    assert summary["exact_rate"] == 0.5
    assert summary["errors"] == 1
    assert summary["mean_seconds"] == 3.0
    assert summary["mean_input_tokens"] == 100
    assert summarize_qa_records([]) == {}


def _record(condition: str, name: str, f1: float = 1.0, error: str = "") -> dict:
    return {
        "condition": condition,
        "name": name,
        "f1": f1,
        "exact": f1 == 1.0,
        "seconds": 1.0,
        "input_tokens": 10,
        "output_tokens": 5,
        "answered": [],
        "expected": [],
        "error": error,
    }


class TestRecordsFileLifecycle:
    # The JSONL records file starts with a fingerprint header so --resume can
    # never merge records from a different run configuration, a fresh run
    # truncates instead of appending, and loading keeps only the LAST record
    # per (condition, name) (Greptile P1 + CodeRabbit review on PR #1388).
    FP = {
        "corpus": "django",
        "commit": "abc123",
        "qtype": "calls",
        "sample": 2,
        "seed": 0,
        "model": "m1",
    }

    def test_fresh_run_truncates_stale_records(self, tmp_path: Path) -> None:
        import json

        from evals.agentic_qa import _init_records_file

        path = tmp_path / "records.jsonl"
        path.write_text(json.dumps(_record("grep", "old_symbol")) + "\n")
        prior = _init_records_file(path, dict(self.FP), resume=False)
        assert prior == {}
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"fingerprint": self.FP}

    def test_resume_keeps_last_record_per_question(self, tmp_path: Path) -> None:
        import json

        from evals.agentic_qa import _init_records_file

        path = tmp_path / "records.jsonl"
        lines = [
            json.dumps({"fingerprint": self.FP}),
            json.dumps(_record("grep", "symbol_a", f1=0.2)),
            json.dumps(_record("grep", "symbol_a", f1=0.9)),
            json.dumps(_record("graph", "symbol_a", f1=0.5)),
        ]
        path.write_text("\n".join(lines) + "\n")
        prior = _init_records_file(path, dict(self.FP), resume=True)
        assert [r["f1"] for r in prior["grep"]] == [0.9]
        assert [r["f1"] for r in prior["graph"]] == [0.5]

    def test_resume_rejects_mismatched_fingerprint(self, tmp_path: Path) -> None:
        import json

        import pytest
        import typer

        from evals.agentic_qa import _init_records_file

        path = tmp_path / "records.jsonl"
        path.write_text(json.dumps({"fingerprint": {**self.FP, "seed": 9}}) + "\n")
        with pytest.raises(typer.BadParameter):
            _init_records_file(path, dict(self.FP), resume=True)

    def test_resume_rejects_headerless_legacy_file(self, tmp_path: Path) -> None:
        import json

        import pytest
        import typer

        from evals.agentic_qa import _init_records_file

        path = tmp_path / "records.jsonl"
        path.write_text(json.dumps(_record("grep", "symbol_a")) + "\n")
        with pytest.raises(typer.BadParameter):
            _init_records_file(path, dict(self.FP), resume=True)

    def test_resume_drops_api_error_records_keeps_timeouts(
        self, tmp_path: Path
    ) -> None:
        import json

        from evals.agentic_qa import _init_records_file

        path = tmp_path / "records.jsonl"
        lines = [
            json.dumps({"fingerprint": self.FP}),
            json.dumps(_record("grep", "symbol_a", error="RuntimeError: credits")),
            json.dumps(_record("grep", "symbol_b", error="TimeoutError: 300s")),
        ]
        path.write_text("\n".join(lines) + "\n")
        prior = _init_records_file(path, dict(self.FP), resume=True)
        assert [r["name"] for r in prior.get("grep", [])] == ["symbol_b"]

    def test_resume_without_existing_file_starts_fresh(self, tmp_path: Path) -> None:
        import json

        from evals.agentic_qa import _init_records_file

        path = tmp_path / "records.jsonl"
        prior = _init_records_file(path, dict(self.FP), resume=True)
        assert prior == {}
        assert json.loads(path.read_text().splitlines()[0]) == {"fingerprint": self.FP}


class TestCollectCallSiteScopes:
    # Decorators, argument defaults, and annotations evaluate at definition
    # time in the ENCLOSING scope; attributing them to the function being
    # defined gives the multihop oracle false second-hop caller files
    # (Greptile review on PR #1388).
    FIRST_PARTY = {
        "make_decorator",
        "default_factory",
        "annotation_helper",
        "return_helper",
    }

    def _sites(self, src: str) -> list:
        import ast

        from evals.agentic_qa import _collect_call_sites

        sites: list = []
        _collect_call_sites("m.py", ast.parse(src), [], self.FIRST_PARTY, sites)
        return sites

    def test_function_header_calls_belong_to_enclosing_scope(self) -> None:
        src = (
            "@make_decorator()\n"
            "def decorated(\n"
            "    x=default_factory(),\n"
            "    y: annotation_helper() = 2,\n"
            ") -> return_helper():\n"
            "    return x\n"
        )
        sites = self._sites(src)
        assert {s.callee for s in sites} == self.FIRST_PARTY
        assert all(s.func is None for s in sites), sites

    def test_lambda_default_belongs_to_enclosing_scope(self) -> None:
        sites = self._sites("handler = lambda z=default_factory(): z\n")
        (site,) = sites
        assert site.func is None

    def test_body_calls_still_belong_to_the_function(self) -> None:
        sites = self._sites("def caller_fn():\n    return default_factory()\n")
        (site,) = sites
        assert site.func == "caller_fn"

    def test_deferred_lambda_body_belongs_to_lambda_scope(self) -> None:
        # A lambda that is stored and called later runs its body in the
        # lambda scope, which the multihop oracle cannot name-match, so the
        # marker must stay.
        from evals.agentic_qa import _LAMBDA_SCOPE

        sites = self._sites("handler = lambda: default_factory()\n")
        (site,) = sites
        assert site.func == _LAMBDA_SCOPE

    def test_immediately_invoked_lambda_belongs_to_enclosing_scope(self) -> None:
        # A lambda invoked at definition time (here in a decorator
        # expression) runs its body in the enclosing scope, not an anonymous
        # one, so its calls must NOT carry the lambda marker or the multihop
        # oracle wrongly drops an otherwise-valid target (Greptile review on
        # PR #1388).
        from evals.agentic_qa import _LAMBDA_SCOPE

        src = "@(lambda: make_decorator())()\ndef decorated():\n    return 1\n"
        sites = self._sites(src)
        callees = {s.callee for s in sites}
        assert "make_decorator" in callees
        for site in sites:
            assert site.func != _LAMBDA_SCOPE, site

    def test_namedexpr_bound_immediate_lambda_belongs_to_enclosing_scope(
        self,
    ) -> None:
        # `@(f := lambda: make_decorator())()` binds the lambda with a walrus
        # then invokes it immediately; the call target is a NamedExpr wrapping
        # the lambda, so it must be unwrapped or the case is dropped (Greptile
        # review on PR #1388).
        from evals.agentic_qa import _LAMBDA_SCOPE

        src = (
            "@(factory := lambda: make_decorator())()\ndef decorated():\n    return 1\n"
        )
        sites = self._sites(src)
        assert "make_decorator" in {s.callee for s in sites}
        for site in sites:
            assert site.func != _LAMBDA_SCOPE, site


def test_run_fingerprint_includes_backend_identity() -> None:
    # The same model id served through a different provider, endpoint, or
    # region has different latency and token behavior, so records from one
    # backend must not resume into a run on another (Greptile review on
    # PR #1388).
    from codebase_rag.config import ModelConfig
    from evals.agentic_qa import _run_fingerprint

    base = ModelConfig(provider="anthropic", model_id="m1", endpoint=None)
    other = ModelConfig(
        provider="openai", model_id="m1", endpoint="https://proxy.example"
    )
    fp_base = _run_fingerprint("django", "abc", "calls", 2, 0, base)
    fp_other = _run_fingerprint("django", "abc", "calls", 2, 0, other)
    assert fp_base != fp_other
    assert fp_base["provider"] == "anthropic"
    assert fp_other["endpoint"] == "https://proxy.example"


def test_run_fingerprint_includes_cypher_backend() -> None:
    # Graph-condition runs generate Cypher with a separate backend
    # (active_cypher_config); changing its provider/model/endpoint changes
    # accuracy, latency, and tokens, so records must not resume across it
    # (Greptile review on PR #1388).
    from codebase_rag.config import ModelConfig
    from evals.agentic_qa import _run_fingerprint

    orch = ModelConfig(provider="anthropic", model_id="m1", endpoint=None)
    cypher_a = ModelConfig(provider="anthropic", model_id="cy1", endpoint=None)
    cypher_b = ModelConfig(provider="openai", model_id="cy2", endpoint="https://x")
    fp_a = _run_fingerprint("django", "abc", "calls", 2, 0, orch, cypher=cypher_a)
    fp_b = _run_fingerprint("django", "abc", "calls", 2, 0, orch, cypher=cypher_b)
    assert fp_a != fp_b
    assert fp_a["cypher_model"] == "cy1"
    assert fp_b["cypher_provider"] == "openai"
    assert fp_b["cypher_endpoint"] == "https://x"


def test_run_fingerprint_omits_cypher_when_not_passed() -> None:
    # A grep-only run does not touch the Cypher backend, so its identity must
    # not enter the fingerprint or an unused Cypher setting change would
    # wrongly reject grep resume (Greptile review on PR #1388).
    from codebase_rag.config import ModelConfig
    from evals.agentic_qa import _run_fingerprint

    orch = ModelConfig(provider="anthropic", model_id="m1", endpoint=None)
    fp = _run_fingerprint("django", "abc", "calls", 2, 0, orch)
    assert not any(k.startswith("cypher_") for k in fp)


def test_run_fingerprint_includes_thinking_budget() -> None:
    # thinking_budget is forwarded to the model by get_provider_from_config
    # and changes its behavior, so runs with different budgets must not
    # resume into each other (CodeRabbit review on PR #1388).
    from codebase_rag.config import ModelConfig
    from evals.agentic_qa import _run_fingerprint

    base = ModelConfig(provider="google", model_id="m1", endpoint=None)
    budgeted = ModelConfig(
        provider="google", model_id="m1", endpoint=None, thinking_budget=1024
    )
    fp_base = _run_fingerprint("django", "abc", "calls", 2, 0, base)
    fp_budgeted = _run_fingerprint("django", "abc", "calls", 2, 0, budgeted)
    assert fp_base != fp_budgeted
    assert fp_budgeted["thinking_budget"] == 1024


def test_run_fingerprint_includes_execution_settings() -> None:
    # AGENT_RETRIES and SHELL_COMMAND_TIMEOUT change how the agent executes
    # (retry count and how long a shell probe may run), so records made under
    # different settings must not resume together (Greptile review on
    # PR #1388).
    from codebase_rag.config import ModelConfig
    from evals.agentic_qa import _run_fingerprint

    cfg = ModelConfig(provider="anthropic", model_id="m1", endpoint=None)
    base = _run_fingerprint(
        "django", "abc", "calls", 2, 0, cfg, agent_retries=3, shell_timeout=30
    )
    retried = _run_fingerprint(
        "django", "abc", "calls", 2, 0, cfg, agent_retries=5, shell_timeout=30
    )
    slower = _run_fingerprint(
        "django", "abc", "calls", 2, 0, cfg, agent_retries=3, shell_timeout=60
    )
    assert base != retried
    assert base != slower
    assert base["agent_retries"] == 3
    assert base["shell_timeout"] == 30
