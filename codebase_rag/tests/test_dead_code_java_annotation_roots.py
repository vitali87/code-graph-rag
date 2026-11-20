"""Issue #2739: a Java method a framework invokes through an annotation is a
dead-code root, as a C# method with `[HttpGet]` or `[Fact]` is.

Spring/Jakarta hooks (`@PostConstruct`, `@Scheduled`, `@EventListener`,
`@Bean`, `@GetMapping`, `@KafkaListener`, ...) are usually package-private, so
the "exported" rule does not root them, and nothing else read Java
annotations: every such method, and everything only it calls, was reported.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import default_dead_code_config
from codebase_rag.parser_loader import load_parsers
from evals.dead_code import cgr_dead_code

PROJECT = "jannot"
PKG = "src/main/java/com/acme"

CALC = """package com.acme;

class Calc {
    int add(int a, int b) {
        return a + b;
    }
}
"""

JOBS = """package com.acme;

import jakarta.annotation.PostConstruct;
import org.springframework.context.annotation.Bean;
import org.springframework.context.event.EventListener;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.boot.context.event.ApplicationReadyEvent;

class Jobs {
    @PostConstruct
    void warmUp() {
        prime();
    }

    private void prime() {}

    @Scheduled(fixedRate = 60000)
    void purge() {
        sweep();
    }

    private void sweep() {}

    @EventListener(ApplicationReadyEvent.class)
    void onReady() {}

    @Bean
    Calc calc() {
        return new Calc();
    }

    void unused() {
        orphan();
    }

    private void orphan() {}

    @Deprecated
    void old() {}
}
"""


def _dead(tmp_path: Path, files: dict[str, str]) -> set[str]:
    root = tmp_path / PROJECT
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    dead = cgr_dead_code(root, PROJECT, default_dead_code_config(False, False))
    return {qn.rsplit(".", 1)[-1].split("(", 1)[0] for qn in dead}


@pytest.fixture(scope="module")
def jobs_dead(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.JAVA not in parsers:
        # A module-scoped fixture runs before the per-test grammar skip
        # hook is installed, so a base install must skip here.
        pytest.skip("java parser not available")
    return _dead(
        tmp_path_factory.mktemp("jobs"),
        {f"{PKG}/Calc.java": CALC, f"{PKG}/Jobs.java": JOBS},
    )


@pytest.mark.parametrize("method", ["warmUp", "purge", "onReady", "calc"])
def test_an_annotated_framework_hook_is_not_dead(
    jobs_dead: set[str], method: str
) -> None:
    assert method not in jobs_dead


@pytest.mark.parametrize("helper", ["prime", "sweep"])
def test_a_helper_only_a_framework_hook_calls_is_not_dead(
    jobs_dead: set[str], helper: str
) -> None:
    assert helper not in jobs_dead


@pytest.mark.parametrize(
    "annotation",
    [
        "@PreDestroy",
        '@GetMapping("/users")',
        '@PostMapping(path = "/users")',
        '@RequestMapping(value = "/x", method = RequestMethod.PUT)',
        "@DeleteMapping",
        "@PatchMapping",
        "@ExceptionHandler(IllegalStateException.class)",
        "@ModelAttribute",
        "@InitBinder",
        "@TransactionalEventListener",
        '@KafkaListener(topics = "orders")',
        '@RabbitListener(queues = "q")',
        '@JmsListener(destination = "d")',
        '@SqsListener("queue")',
        '@Path("/items")',
        "@PrePersist",
        "@PostLoad",
        '@org.springframework.scheduling.annotation.Scheduled(cron = "0 0 * * * *")',
    ],
)
def test_each_framework_annotation_roots_its_method(
    tmp_path: Path, annotation: str
) -> None:
    source = (
        "package com.acme;\n\nclass Hooks {\n"
        f"    {annotation}\n    void hook() {{\n        helper();\n    }}\n\n"
        "    private void helper() {}\n}\n"
    )

    dead = _dead(tmp_path, {f"{PKG}/Hooks.java": source})

    assert not {"hook", "helper"} & dead


# Negative: what must not change.


def test_an_unannotated_method_and_its_callee_stay_dead(jobs_dead: set[str]) -> None:
    assert {"unused", "orphan"} <= jobs_dead


def test_an_unrelated_annotation_does_not_root(jobs_dead: set[str]) -> None:
    assert "old" in jobs_dead


@pytest.mark.parametrize("decorator", ["@scheduled", "@bean", "@postconstruct"])
def test_the_java_names_do_not_root_a_python_function(
    tmp_path: Path, decorator: str
) -> None:
    source = f"{decorator}\ndef _hook():\n    return 1\n"

    assert "_hook" in _dead(tmp_path, {"app.py": source})
