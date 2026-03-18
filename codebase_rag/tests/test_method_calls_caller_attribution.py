from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

if TYPE_CHECKING:
    pass


def _get_method_caller_calls(mock_ingestor: MagicMock) -> list:
    return [
        c
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
        if c.args[0][0] == cs.NodeLabel.METHOD
    ]


def _get_function_caller_calls(mock_ingestor: MagicMock) -> list:
    return [
        c
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
        if c.args[0][0] == cs.NodeLabel.FUNCTION
    ]


def _get_module_caller_calls(mock_ingestor: MagicMock) -> list:
    return [
        c
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
        if c.args[0][0] == cs.NodeLabel.MODULE
    ]


def _caller_qn(call: MagicMock) -> str:
    return call.args[0][2]


def _callee_qn(call: MagicMock) -> str:
    return call.args[2][2]


class TestCppMethodCallerAttribution:
    def test_simple_class_method_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "player.cpp").write_text(
            encoding="utf-8",
            data="""
class Player {
public:
    void handleArtifact() {}

    void handleArtifactWatcherCb() {
        handleArtifact();
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        callees = [_callee_qn(c) for c in method_calls]

        watcher_callers = [qn for qn in callers if "handleArtifactWatcherCb" in qn]
        assert len(watcher_callers) >= 1

        artifact_callees = [qn for qn in callees if "handleArtifact" in qn]
        assert len(artifact_callees) >= 1

    def test_struct_method_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "sensor.cpp").write_text(
            encoding="utf-8",
            data="""
struct Sensor {
    int readRaw() { return 42; }

    int readCalibrated() {
        return readRaw() * 2;
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        callees = [_callee_qn(c) for c in method_calls]

        assert any("readCalibrated" in qn for qn in callers)
        assert any("readRaw" in qn for qn in callees)

    def test_multiple_methods_calling_each_other(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "calc.cpp").write_text(
            encoding="utf-8",
            data="""
class Calculator {
public:
    int add(int a, int b) { return a + b; }
    int multiply(int a, int b) { return a * b; }

    int compute(int x) {
        int sum = add(x, 1);
        return multiply(sum, 2);
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        compute_calls = [c for c in method_calls if "compute" in _caller_qn(c)]
        compute_callees = {_callee_qn(c) for c in compute_calls}

        assert any("add" in qn for qn in compute_callees)
        assert any("multiply" in qn for qn in compute_callees)

    def test_constructor_body_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "init.cpp").write_text(
            encoding="utf-8",
            data="""
class Engine {
public:
    void initialize() {}

    Engine() {
        initialize();
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callees = [_callee_qn(c) for c in method_calls]
        assert any("initialize" in qn for qn in callees)

    def test_method_calling_free_function_has_method_caller(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "mixed.cpp").write_text(
            encoding="utf-8",
            data="""
void freeHelper() {}

class Service {
public:
    void process() {
        freeHelper();
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        process_calls = [c for c in method_calls if "process" in _caller_qn(c)]
        assert len(process_calls) >= 1

    def test_multiple_classes_in_one_file(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "multi.cpp").write_text(
            encoding="utf-8",
            data="""
class Alpha {
public:
    void step1() {}
    void run() { step1(); }
};

class Beta {
public:
    void step2() {}
    void execute() { step2(); }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = {_caller_qn(c) for c in method_calls}
        callees = {_callee_qn(c) for c in method_calls}

        assert any("run" in qn for qn in callers)
        assert any("execute" in qn for qn in callers)
        assert any("step1" in qn for qn in callees)
        assert any("step2" in qn for qn in callees)

    def test_method_with_parameters(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "params.cpp").write_text(
            encoding="utf-8",
            data="""
class Parser {
public:
    int parse(const char* input, int length) { return 0; }

    int parseFile(const char* path) {
        return parse(path, 100);
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("parseFile" in qn for qn in callers)

    def test_virtual_method_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "virtual.cpp").write_text(
            encoding="utf-8",
            data="""
class Base {
public:
    virtual void onEvent() {}

    void dispatch() {
        onEvent();
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        dispatch_calls = [c for c in method_calls if "dispatch" in _caller_qn(c)]
        assert len(dispatch_calls) >= 1
        assert any("onEvent" in _callee_qn(c) for c in dispatch_calls)

    def test_method_calling_another_via_this_pointer(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "this_ptr.cpp").write_text(
            encoding="utf-8",
            data="""
class Widget {
public:
    void repaint() {}

    void resize(int w, int h) {
        this->repaint();
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("resize" in qn for qn in callers)

    def test_deeply_nested_call_chain(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "chain.cpp").write_text(
            encoding="utf-8",
            data="""
class Pipeline {
public:
    int validate() { return 1; }
    int transform(int x) { return x * 2; }
    int output(int x) { return x; }

    int run() {
        int v = validate();
        int t = transform(v);
        return output(t);
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        run_calls = [c for c in method_calls if "run" in _caller_qn(c)]
        run_callees = {_callee_qn(c) for c in run_calls}

        assert any("validate" in qn for qn in run_callees)
        assert any("transform" in qn for qn in run_callees)
        assert any("output" in qn for qn in run_callees)

    def test_static_method_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "static.cpp").write_text(
            encoding="utf-8",
            data="""
class Factory {
public:
    static int create() { return 0; }

    static int build() {
        return create();
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("build" in qn for qn in callers)

    def test_const_method_calls(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "const.cpp").write_text(
            encoding="utf-8",
            data="""
class Container {
public:
    int size() const { return 10; }

    bool empty() const {
        return size() == 0;
    }
};
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.CPP)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("empty" in qn for qn in callers)


class TestPythonMethodCallerAttribution:
    def test_method_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "service.py").write_text(
            encoding="utf-8",
            data="""
class Service:
    def validate(self):
        pass

    def process(self):
        self.validate()
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.PYTHON)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("process" in qn for qn in callers)

    def test_multiple_methods_calling_each_other(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "pipeline.py").write_text(
            encoding="utf-8",
            data="""
class Pipeline:
    def step1(self):
        pass

    def step2(self):
        self.step1()

    def run(self):
        self.step2()
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.PYTHON)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = {_caller_qn(c) for c in method_calls}
        assert any("step2" in qn for qn in callers)
        assert any("run" in qn for qn in callers)

    def test_dunder_init_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "init.py").write_text(
            encoding="utf-8",
            data="""
class Config:
    def _load(self):
        pass

    def __init__(self):
        self._load()
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.PYTHON)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("__init__" in qn for qn in callers)


class TestJavaScriptMethodCallerAttribution:
    def test_class_method_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "service.js").write_text(
            encoding="utf-8",
            data="""
class Service {
    validate() {
        return true;
    }

    process() {
        return this.validate();
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.JS)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("process" in qn for qn in callers)

    def test_constructor_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "widget.js").write_text(
            encoding="utf-8",
            data="""
class Widget {
    setup() {}

    constructor() {
        this.setup();
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.JS)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callees = [_callee_qn(c) for c in method_calls]
        assert any("setup" in qn for qn in callees)


class TestTypeScriptMethodCallerAttribution:
    def test_class_method_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "handler.ts").write_text(
            encoding="utf-8",
            data="""
class Handler {
    private validate(): boolean {
        return true;
    }

    public handle(): void {
        this.validate();
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.TS)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("handle" in qn for qn in callers)

    def test_multiple_methods_with_types(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "repo.ts").write_text(
            encoding="utf-8",
            data="""
class Repository {
    find(id: number): string { return ""; }
    validate(data: string): boolean { return true; }

    save(id: number): boolean {
        const item = this.find(id);
        return this.validate(item);
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.TS)

        method_calls = _get_method_caller_calls(mock_ingestor)
        save_calls = [c for c in method_calls if "save" in _caller_qn(c)]
        save_callees = {_callee_qn(c) for c in save_calls}
        assert any("find" in qn for qn in save_callees)
        assert any("validate" in qn for qn in save_callees)


class TestJavaMethodCallerAttribution:
    def test_method_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "Service.java").write_text(
            encoding="utf-8",
            data="""
public class Service {
    private boolean validate() {
        return true;
    }

    public void process() {
        validate();
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.JAVA)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("process" in qn for qn in callers)

    def test_constructor_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "Config.java").write_text(
            encoding="utf-8",
            data="""
public class Config {
    private void loadDefaults() {}

    public Config() {
        loadDefaults();
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.JAVA)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callees = [_callee_qn(c) for c in method_calls]
        assert any("loadDefaults" in qn for qn in callees)

    def test_multiple_methods_calling_each_other(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "Calculator.java").write_text(
            encoding="utf-8",
            data="""
public class Calculator {
    public int add(int a, int b) { return a + b; }
    public int multiply(int a, int b) { return a * b; }

    public int compute(int x) {
        int sum = add(x, 1);
        return multiply(sum, 2);
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.JAVA)

        method_calls = _get_method_caller_calls(mock_ingestor)
        compute_calls = [c for c in method_calls if "compute" in _caller_qn(c)]
        compute_callees = {_callee_qn(c) for c in compute_calls}
        assert any("add" in qn for qn in compute_callees)
        assert any("multiply" in qn for qn in compute_callees)


class TestRustMethodCallerAttribution:
    def test_impl_method_calls_method(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "lib.rs").write_text(
            encoding="utf-8",
            data="""
struct Player {
    health: i32,
}

impl Player {
    fn heal(&mut self) {
        self.health += 10;
    }

    fn take_damage(&mut self, amount: i32) {
        self.health -= amount;
        self.heal();
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.RUST)

        method_calls = _get_method_caller_calls(mock_ingestor)
        callers = [_caller_qn(c) for c in method_calls]
        assert any("take_damage" in qn for qn in callers)

    def test_multiple_impl_methods(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        (temp_repo / "lib.rs").write_text(
            encoding="utf-8",
            data="""
struct Pipeline;

impl Pipeline {
    fn validate(&self) -> bool { true }
    fn transform(&self, x: i32) -> i32 { x * 2 }

    fn run(&self, input: i32) -> i32 {
        if self.validate() {
            self.transform(input)
        } else {
            0
        }
    }
}
""",
        )
        run_updater(temp_repo, mock_ingestor, cs.SupportedLanguage.RUST)

        method_calls = _get_method_caller_calls(mock_ingestor)
        run_calls = [c for c in method_calls if "run" in _caller_qn(c)]
        assert len(run_calls) >= 1
