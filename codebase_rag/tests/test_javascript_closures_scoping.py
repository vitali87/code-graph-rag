"""
Comprehensive JavaScript closure and scoping parsing and relationship testing.
Tests all possible JavaScript closure patterns, scoping rules, and hoisting behavior.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def javascript_closures_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with all closure and scoping patterns."""
    project_path = temp_repo / "javascript_closures_test"
    project_path.mkdir()

    # Create basic structure
    (project_path / "src").mkdir()
    (project_path / "utils").mkdir()

    # Create helper files
    (project_path / "src" / "counter.js").write_text("export let globalCounter = 0;")
    (project_path / "utils" / "logger.js").write_text(
        "export function log(message) { console.log(message); }"
    )

    return project_path


def test_basic_closures(
    javascript_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic closure patterns and variable capture."""
    test_file = javascript_closures_project / "basic_closures.js"
    test_file.write_text(
        """
// Basic closure - function accessing outer variable
function outerFunction(x) {
    const outerVar = x;

    function innerFunction(y) {
        return outerVar + y; // Closure over outerVar
    }

    return innerFunction;
}

// Closure with multiple captured variables
function createCounter(initialValue) {
    let count = initialValue;
    let step = 1;

    return function(newStep) {
        if (newStep !== undefined) {
            step = newStep;
        }
        count += step;
        return count;
    };
}

// Closure capturing function parameters
function multiplierFactory(multiplier) {
    return function(value) {
        return value * multiplier;
    };
}

// Closure with nested functions
function nestedClosures(a) {
    function level1(b) {
        function level2(c) {
            function level3(d) {
                return a + b + c + d; // Closure over all outer variables
            }
            return level3;
        }
        return level2;
    }
    return level1;
}

// Closure in arrow functions
const createAdder = (x) => {
    return (y) => x + y; // Arrow function closure
};

const createFormatter = (prefix) => (suffix) => (text) =>
    `${prefix}${text}${suffix}`; // Chained arrow function closures

// Closure with side effects
function createLogger(logLevel) {
    const logs = [];

    return function(message) {
        const timestamp = new Date().toISOString();
        const logEntry = `[${timestamp}] ${logLevel}: ${message}`;
        logs.push(logEntry);
        console.log(logEntry);
        return logs.length;
    };
}

// Closure accessing outer scope variables
let globalConfig = { debug: true };

function createDebugger(module) {
    const moduleConfig = { name: module, enabled: true };

    return function(message) {
        if (globalConfig.debug && moduleConfig.enabled) {
            console.log(`[${moduleConfig.name}] ${message}`);
        }
    };
}

// Using closures
const addFive = outerFunction(5);
const result1 = addFive(3); // Should be 8

const counter = createCounter(10);
const count1 = counter(); // 11
const count2 = counter(2); // 13
const count3 = counter(); // 15

const double = multiplierFactory(2);
const triple = multiplierFactory(3);
const doubled = double(4); // 8
const tripled = triple(4); // 12

const nested = nestedClosures(1)(2)(3);
const nestedResult = nested(4); // 10

const add10 = createAdder(10);
const addResult = add10(5); // 15

const htmlFormatter = createFormatter('<')('>');
const formatted = htmlFormatter('Hello'); // <Hello>

const infoLogger = createLogger('INFO');
const debugLogger = createLogger('DEBUG');
const logCount1 = infoLogger('Application started');
const logCount2 = debugLogger('Debug message');

const appDebugger = createDebugger('App');
appDebugger('Debug from app');

// Functions demonstrating closure behavior
function testClosures() {
    const localVar = 'local';

    function inner() {
        return localVar;
    }

    return inner();
}

const closureTest = testClosures();

// Closure with loop (classic problem)
function createFunctionArray() {
    const functions = [];

    for (let i = 0; i < 3; i++) {
        functions.push(function() {
            return i; // Closure over loop variable
        });
    }

    return functions;
}

// Closure with loop using var (different behavior)
function createFunctionArrayVar() {
    const functions = [];

    for (var j = 0; j < 3; j++) {
        functions.push((function(index) {
            return function() {
                return index; // IIFE to capture current value
            };
        })(j));
    }

    return functions;
}

const letFunctions = createFunctionArray();
const varFunctions = createFunctionArrayVar();

const letResults = letFunctions.map(fn => fn());
const varResults = varFunctions.map(fn => fn());
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_closures_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_closures_project.name

    # Expected functions demonstrating closures
    expected_functions = [
        f"{project_name}.basic_closures.outerFunction",
        f"{project_name}.basic_closures.createCounter",
        f"{project_name}.basic_closures.multiplierFactory",
        f"{project_name}.basic_closures.nestedClosures",
        f"{project_name}.basic_closures.createAdder",
        f"{project_name}.basic_closures.createFormatter",
        f"{project_name}.basic_closures.createLogger",
        f"{project_name}.basic_closures.createDebugger",
        f"{project_name}.basic_closures.testClosures",
        f"{project_name}.basic_closures.createFunctionArray",
        f"{project_name}.basic_closures.createFunctionArrayVar",
    ]

    # Get all Function node creation calls
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Verify closure functions were created
    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )

    # Verify nested functions are captured
    nested_functions = [
        call
        for call in function_calls
        if "basic_closures" in call[0][1]["qualified_name"]
        and len(call[0][1]["qualified_name"].split("."))
        > 3  # More than project.module.function means nested
    ]

    assert len(nested_functions) >= 5, (
        f"Expected at least 5 nested functions from closures, found {len(nested_functions)}"
    )

    # Verify function calls are tracked
    call_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    closure_calls = [
        call for call in call_relationships if "basic_closures" in call.args[0][2]
    ]

    assert len(closure_calls) >= 10, (
        f"Expected at least 10 function calls in closure code, found {len(closure_calls)}"
    )


def test_variable_scoping(
    javascript_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test JavaScript variable scoping rules (var, let, const)."""
    test_file = javascript_closures_project / "variable_scoping.js"
    test_file.write_text(
        """
// Global scope
var globalVar = "global var";
let globalLet = "global let";
const globalConst = "global const";

// Function scope
function functionScope() {
    var functionVar = "function var";
    let functionLet = "function let";
    const functionConst = "function const";

    console.log(globalVar, globalLet, globalConst);
    console.log(functionVar, functionLet, functionConst);

    // Inner function accessing outer scope
    function innerFunction() {
        var innerVar = "inner var";
        console.log(functionVar, functionLet, functionConst); // Access outer scope
        console.log(innerVar);
    }

    innerFunction();
    // console.log(innerVar); // Would cause ReferenceError
}

// Block scope with let and const
function blockScope() {
    var varInFunction = "var in function";

    if (true) {
        var varInBlock = "var in block"; // Function scoped
        let letInBlock = "let in block"; // Block scoped
        const constInBlock = "const in block"; // Block scoped

        console.log(varInFunction, varInBlock, letInBlock, constInBlock);
    }

    console.log(varInFunction, varInBlock); // varInBlock is accessible
    // console.log(letInBlock, constInBlock); // Would cause ReferenceError

    for (let i = 0; i < 3; i++) {
        let loopLet = `loop let ${i}`;
        const loopConst = `loop const ${i}`;
        var loopVar = `loop var ${i}`; // Function scoped

        console.log(loopLet, loopConst, loopVar);
    }

    // console.log(i, loopLet, loopConst); // Would cause ReferenceError
    console.log(loopVar); // Accessible (last iteration value)
}

// Temporal Dead Zone demonstration
function temporalDeadZone() {
    console.log(varHoisted); // undefined (hoisted)
    // console.log(letTDZ); // ReferenceError (temporal dead zone)
    // console.log(constTDZ); // ReferenceError (temporal dead zone)

    var varHoisted = "var hoisted";
    let letTDZ = "let in TDZ";
    const constTDZ = "const in TDZ";

    console.log(varHoisted, letTDZ, constTDZ);
}

// Closure with different scoping
function closureScoping() {
    const outerConst = "outer";
    let outerLet = "outer";
    var outerVar = "outer";

    function createClosures() {
        const closures = [];

        for (var i = 0; i < 3; i++) {
            closures.push(function() {
                return { outerConst, outerLet, outerVar, i };
            });
        }

        return closures;
    }

    function createClosuresLet() {
        const closures = [];

        for (let j = 0; j < 3; j++) {
            closures.push(function() {
                return { outerConst, outerLet, outerVar, j };
            });
        }

        return closures;
    }

    return { createClosures, createClosuresLet };
}

// Scope chain demonstration
var scopeVar = "global scope";

function outerScope() {
    var scopeVar = "outer scope";

    function middleScope() {
        var scopeVar = "middle scope";

        function innerScope() {
            // Uses innermost scopeVar
            console.log(scopeVar);

            function deeperScope() {
                // Still uses middle scope's scopeVar
                console.log(scopeVar);
            }

            return deeperScope;
        }

        return innerScope;
    }

    return middleScope;
}

// Module pattern with scope
const modulePattern = (function() {
    let privateVar = 0;
    const privateConst = "private";

    function privateFunction() {
        return `Private: ${privateVar}`;
    }

    return {
        getPrivateVar: function() {
            return privateVar;
        },

        setPrivateVar: function(value) {
            privateVar = value;
        },

        getPrivateMessage: function() {
            return privateFunction();
        },

        publicVar: "public"
    };
})();

// Arrow functions and scope
function arrowFunctionScope() {
    const outerThis = this;
    const contextValue = "outer context";

    const regularFunction = function() {
        console.log(this); // Different 'this'
        console.log(contextValue); // Closure
    };

    const arrowFunction = () => {
        console.log(this); // Inherits 'this' from outer scope
        console.log(contextValue); // Closure
    };

    return { regularFunction, arrowFunction };
}

// Using scoping functions
functionScope();
blockScope();
temporalDeadZone();

const { createClosures, createClosuresLet } = closureScoping();
const varClosures = createClosures();
const letClosures = createClosuresLet();

const varResults = varClosures.map(fn => fn());
const letResults = letClosures.map(fn => fn());

const scopeChain = outerScope()()();
scopeChain();

const privateValue = modulePattern.getPrivateVar();
modulePattern.setPrivateVar(42);
const privateMessage = modulePattern.getPrivateMessage();

const { regularFunction, arrowFunction } = arrowFunctionScope();
regularFunction();
arrowFunction();

// Scope with try-catch
function tryCatchScope() {
    try {
        let tryLet = "try block";
        const tryConst = "try block";
        var tryVar = "try block";

        throw new Error("Test error");
    } catch (error) {
        let catchLet = "catch block";
        const catchConst = "catch block";
        var catchVar = "catch block";

        console.log(error.message);
        // console.log(tryLet, tryConst); // ReferenceError
        console.log(tryVar); // Accessible
    } finally {
        let finallyLet = "finally block";
        const finallyConst = "finally block";
        var finallyVar = "finally block";

        // console.log(catchLet, catchConst); // ReferenceError
        console.log(tryVar, catchVar); // Accessible
    }

    // console.log(finallyLet, finallyConst); // ReferenceError
    console.log(tryVar, catchVar, finallyVar); // All accessible
}

tryCatchScope();
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_closures_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_closures_project.name

    # Expected functions demonstrating scoping
    expected_scoping_functions = [
        f"{project_name}.variable_scoping.functionScope",
        f"{project_name}.variable_scoping.blockScope",
        f"{project_name}.variable_scoping.temporalDeadZone",
        f"{project_name}.variable_scoping.closureScoping",
        f"{project_name}.variable_scoping.outerScope",
        f"{project_name}.variable_scoping.arrowFunctionScope",
        f"{project_name}.variable_scoping.tryCatchScope",
    ]

    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Verify scoping functions were created
    found_scoping_functions = [
        func for func in expected_scoping_functions if func in created_functions
    ]
    assert len(found_scoping_functions) >= 5, (
        f"Expected at least 5 scoping functions, found {len(found_scoping_functions)}"
    )

    # Should have nested functions demonstrating scope chain
    nested_scoping_functions = [
        call
        for call in function_calls
        if "variable_scoping" in call[0][1]["qualified_name"]
        and len(call[0][1]["qualified_name"].split(".")) > 3
    ]

    assert len(nested_scoping_functions) >= 8, (
        f"Expected at least 8 nested scoping functions, found {len(nested_scoping_functions)}"
    )


def test_hoisting_behavior(
    javascript_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test JavaScript hoisting behavior for variables and functions."""
    test_file = javascript_closures_project / "hoisting_behavior.js"
    test_file.write_text(
        """
// Function declaration hoisting
console.log(hoistedFunction()); // Works due to hoisting

function hoistedFunction() {
    return "I am hoisted!";
}

// Variable hoisting with var
console.log(hoistedVar); // undefined (declared but not initialized)
var hoistedVar = "Now I have a value";

// Function expression - not hoisted
// console.log(notHoisted()); // TypeError: notHoisted is not a function
var notHoisted = function() {
    return "I am not hoisted";
};

// Let and const - temporal dead zone
function temporalDeadZoneExample() {
    // console.log(letVar); // ReferenceError
    // console.log(constVar); // ReferenceError

    let letVar = "let variable";
    const constVar = "const variable";

    console.log(letVar, constVar);
}

// Hoisting in function scope
function hoistingInFunction() {
    console.log(typeof innerFunction); // "function"
    console.log(typeof innerVar); // "undefined"
    // console.log(typeof innerLet); // ReferenceError

    var innerVar = "inner var";
    let innerLet = "inner let";

    function innerFunction() {
        return "inner function";
    }

    // Function expression hoisting
    console.log(typeof funcExpr); // "undefined"
    var funcExpr = function() {
        return "function expression";
    };

    console.log(funcExpr()); // Now it works
}

// Complex hoisting scenario
function complexHoisting() {
    // Function declaration wins over var declaration
    console.log(typeof duplicateName); // "function"

    var duplicateName = "variable";

    function duplicateName() {
        return "function";
    }

    console.log(typeof duplicateName); // "string" (reassigned)

    // Nested function hoisting
    function outer() {
        console.log(typeof nestedHoisted); // "function"

        function nestedHoisted() {
            return "nested hoisted";
        }

        if (false) {
            // This function is still hoisted even though unreachable
            function conditionalHoisted() {
                return "conditional";
            }
        }

        // conditionalHoisted is hoisted in function scope
        console.log(typeof conditionalHoisted); // "function" or "undefined" (depends on engine)
    }

    return outer;
}

// Hoisting with closures
function hoistingWithClosures() {
    var closureVar = "outer";

    function createClosure() {
        console.log(closureVar); // undefined due to hoisting

        var closureVar = "inner"; // Hoisted declaration shadows outer

        return function() {
            return closureVar;
        };
    }

    return createClosure();
}

// Block scoping vs hoisting
function blockScopingVsHoisting() {
    var functionScoped = "function scoped";

    if (true) {
        // var is hoisted to function scope
        var hoistedInBlock = "hoisted";

        // let and const are block scoped
        let blockScoped = "block scoped";
        const alsoBlockScoped = "also block scoped";

        function blockFunction() {
            return "block function";
        }
    }

    console.log(hoistedInBlock); // Accessible
    // console.log(blockScoped); // ReferenceError
    // console.log(alsoBlockScoped); // ReferenceError
    console.log(blockFunction()); // May or may not be accessible (engine dependent)
}

// Hoisting in loops
function hoistingInLoops() {
    console.log(typeof loopVar); // "undefined"
    console.log(typeof loopFunction); // "function"

    for (var i = 0; i < 3; i++) {
        var loopVar = i;

        function loopFunction() {
            return "loop function";
        }
    }

    console.log(loopVar); // Last value from loop
    console.log(loopFunction()); // Function is hoisted and accessible
}

// Arrow functions and hoisting
function arrowFunctionHoisting() {
    // console.log(arrowFunc()); // TypeError: arrowFunc is not a function

    var arrowFunc = () => "arrow function";

    console.log(arrowFunc()); // Now it works

    // Let/const with arrow functions
    // console.log(letArrow()); // ReferenceError
    let letArrow = () => "let arrow";

    // console.log(constArrow()); // ReferenceError
    const constArrow = () => "const arrow";
}

// Class hoisting
function classHoisting() {
    // console.log(new HoistedClass()); // ReferenceError

    class HoistedClass {
        constructor() {
            this.message = "class";
        }
    }

    console.log(new HoistedClass());
}

// Using hoisting functions
const result1 = hoistedFunction();
console.log(hoistedVar);

temporalDeadZoneExample();
hoistingInFunction();

const outer = complexHoisting();
outer();

const closure = hoistingWithClosures();
const closureResult = closure();

blockScopingVsHoisting();
hoistingInLoops();
arrowFunctionHoisting();
classHoisting();

// Hoisting with immediate invocation
(function() {
    console.log(typeof immediateHoisted); // "function"

    function immediateHoisted() {
        return "immediately invoked";
    }

    console.log(immediateHoisted());
})();

// Hoisting edge cases
function hoistingEdgeCases() {
    // Function parameter shadows hoisted var
    function parameterShadowing(hoistedVar) {
        console.log(hoistedVar); // Parameter value, not hoisted var
        var hoistedVar = "local"; // This declaration is hoisted but doesn't shadow parameter
        console.log(hoistedVar); // "local"
    }

    parameterShadowing("parameter");

    // Nested function name hoisting
    function outerNested() {
        function innerNested() {
            return "inner";
        }

        return innerNested;
    }

    return outerNested;
}

const edgeCases = hoistingEdgeCases();
const nested = edgeCases();
const nestedResult = nested();
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_closures_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = javascript_closures_project.name

    # Expected functions demonstrating hoisting
    expected_hoisting_functions = [
        f"{project_name}.hoisting_behavior.hoistedFunction",
        f"{project_name}.hoisting_behavior.temporalDeadZoneExample",
        f"{project_name}.hoisting_behavior.hoistingInFunction",
        f"{project_name}.hoisting_behavior.complexHoisting",
        f"{project_name}.hoisting_behavior.hoistingWithClosures",
        f"{project_name}.hoisting_behavior.blockScopingVsHoisting",
        f"{project_name}.hoisting_behavior.hoistingInLoops",
        f"{project_name}.hoisting_behavior.arrowFunctionHoisting",
        f"{project_name}.hoisting_behavior.classHoisting",
        f"{project_name}.hoisting_behavior.hoistingEdgeCases",
    ]

    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    created_functions = {call[0][1]["qualified_name"] for call in function_calls}

    # Verify hoisting functions were created
    found_hoisting_functions = [
        func for func in expected_hoisting_functions if func in created_functions
    ]
    assert len(found_hoisting_functions) >= 7, (
        f"Expected at least 7 hoisting functions, found {len(found_hoisting_functions)}"
    )

    # Should have classes demonstrating hoisting behavior
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    hoisting_classes = [
        call
        for call in class_calls
        if "hoisting_behavior" in call[0][1]["qualified_name"]
    ]

    assert len(hoisting_classes) >= 1, (
        f"Expected at least 1 class in hoisting behavior tests, found {len(hoisting_classes)}"
    )


def test_module_patterns_iife(
    javascript_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test module patterns using IIFE and closures."""
    test_file = javascript_closures_project / "module_patterns.js"
    test_file.write_text(
        """
// Basic module pattern
const BasicModule = (function() {
    let privateVariable = 0;
    const privateConstant = "private";

    function privateFunction() {
        return privateVariable++;
    }

    return {
        publicMethod: function() {
            return privateFunction();
        },

        getPrivateConstant: function() {
            return privateConstant;
        },

        setPrivateVariable: function(value) {
            privateVariable = value;
        }
    };
})();

// Revealing module pattern
const RevealingModule = (function() {
    let counter = 0;
    let name = "RevealingModule";

    function increment() {
        counter++;
    }

    function decrement() {
        counter--;
    }

    function getCount() {
        return counter;
    }

    function getName() {
        return name;
    }

    function reset() {
        counter = 0;
    }

    // Reveal public interface
    return {
        increment: increment,
        decrement: decrement,
        count: getCount,
        name: getName,
        reset: reset
    };
})();

// Module with parameters
const ParameterizedModule = (function(globalVar, $) {
    let internalState = globalVar || "default";

    function utilityFunction(selector) {
        return $(selector);
    }

    function processData(data) {
        return data.map(item => `${internalState}: ${item}`);
    }

    return {
        process: processData,
        query: utilityFunction,
        getState: function() {
            return internalState;
        }
    };
})(window.globalVariable, window.jQuery);

// Namespace pattern
const MyNamespace = (function() {
    const namespace = {};

    namespace.Utils = (function() {
        function helper1() {
            return "helper1";
        }

        function helper2() {
            return "helper2";
        }

        return {
            helper1: helper1,
            helper2: helper2
        };
    })();

    namespace.Data = (function() {
        let storage = [];

        function add(item) {
            storage.push(item);
        }

        function remove(item) {
            const index = storage.indexOf(item);
            if (index > -1) {
                storage.splice(index, 1);
            }
        }

        function getAll() {
            return storage.slice(); // Return copy
        }

        return {
            add: add,
            remove: remove,
            getAll: getAll
        };
    })();

    return namespace;
})();

// Singleton pattern
const Singleton = (function() {
    let instance;

    function createInstance() {
        const object = {
            name: "Singleton Instance",
            id: Math.random(),

            method: function() {
                return `${this.name} - ${this.id}`;
            }
        };

        return object;
    }

    return {
        getInstance: function() {
            if (!instance) {
                instance = createInstance();
            }
            return instance;
        }
    };
})();

// Factory pattern with closures
const Factory = (function() {
    function createUser(name, email) {
        let userData = {
            name: name,
            email: email,
            id: generateId()
        };

        function generateId() {
            return Date.now() + Math.random();
        }

        function getInfo() {
            return `${userData.name} (${userData.email})`;
        }

        function updateEmail(newEmail) {
            userData.email = newEmail;
        }

        return {
            getInfo: getInfo,
            updateEmail: updateEmail,
            getId: function() {
                return userData.id;
            }
        };
    }

    function createProduct(name, price) {
        let productData = {
            name: name,
            price: price,
            id: generateProductId()
        };

        function generateProductId() {
            return `PROD_${Date.now()}`;
        }

        function getPrice() {
            return productData.price;
        }

        function setPrice(newPrice) {
            if (newPrice > 0) {
                productData.price = newPrice;
            }
        }

        return {
            getPrice: getPrice,
            setPrice: setPrice,
            getName: function() {
                return productData.name;
            },
            getId: function() {
                return productData.id;
            }
        };
    }

    return {
        createUser: createUser,
        createProduct: createProduct
    };
})();

// Observer pattern with closures
const Observer = (function() {
    let observers = [];

    function addObserver(observer) {
        observers.push(observer);
    }

    function removeObserver(observer) {
        const index = observers.indexOf(observer);
        if (index > -1) {
            observers.splice(index, 1);
        }
    }

    function notifyObservers(data) {
        observers.forEach(observer => {
            if (typeof observer.update === 'function') {
                observer.update(data);
            }
        });
    }

    return {
        subscribe: addObserver,
        unsubscribe: removeObserver,
        notify: notifyObservers
    };
})();

// Configuration module
const Config = (function() {
    const defaultConfig = {
        apiUrl: 'https://api.example.com',
        timeout: 5000,
        retries: 3
    };

    let currentConfig = Object.assign({}, defaultConfig);

    function set(key, value) {
        if (key in defaultConfig) {
            currentConfig[key] = value;
        }
    }

    function get(key) {
        return currentConfig[key];
    }

    function reset() {
        currentConfig = Object.assign({}, defaultConfig);
    }

    function getAll() {
        return Object.assign({}, currentConfig);
    }

    return {
        set: set,
        get: get,
        reset: reset,
        getAll: getAll
    };
})();

// Using module patterns
const basicResult = BasicModule.publicMethod();
const privateConst = BasicModule.getPrivateConstant();
BasicModule.setPrivateVariable(10);

RevealingModule.increment();
RevealingModule.increment();
const count = RevealingModule.count();
const moduleName = RevealingModule.name();
RevealingModule.reset();

const processedData = ParameterizedModule.process(['item1', 'item2']);
const state = ParameterizedModule.getState();

const helper1Result = MyNamespace.Utils.helper1();
MyNamespace.Data.add('item1');
MyNamespace.Data.add('item2');
const allData = MyNamespace.Data.getAll();

const instance1 = Singleton.getInstance();
const instance2 = Singleton.getInstance();
const sameInstance = instance1 === instance2;

const user = Factory.createUser('John', 'john@example.com');
const product = Factory.createProduct('Widget', 19.99);
const userInfo = user.getInfo();
const productPrice = product.getPrice();

Observer.subscribe({
    update: function(data) {
        console.log('Observer 1:', data);
    }
});

Observer.notify('Test notification');

Config.set('timeout', 10000);
const timeout = Config.get('timeout');
const allConfig = Config.getAll();
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_closures_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify module patterns create proper function relationships
    call_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    module_calls = [
        call for call in call_relationships if "module_patterns" in call.args[0][2]
    ]

    assert len(module_calls) >= 15, (
        f"Expected at least 15 function calls in module patterns, found {len(module_calls)}"
    )

    # Should have many functions from IIFE patterns
    function_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Function"
    ]

    module_functions = [
        call
        for call in function_calls
        if "module_patterns" in call[0][1]["qualified_name"]
    ]

    assert len(module_functions) >= 20, (
        f"Expected at least 20 functions in module patterns, found {len(module_functions)}"
    )


def test_closures_comprehensive(
    javascript_closures_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all closure and scoping patterns create proper relationships."""
    test_file = javascript_closures_project / "comprehensive_closures.js"
    test_file.write_text(
        """
// Every JavaScript closure and scoping pattern in one file

// Basic closure
function createCounter(start = 0) {
    let count = start;

    return function(increment = 1) {
        count += increment;
        return count;
    };
}

// Nested closures with multiple scope levels
function multiLevelClosure(a) {
    const level1Var = a;

    return function(b) {
        const level2Var = b;

        return function(c) {
            const level3Var = c;

            return function(d) {
                return level1Var + level2Var + level3Var + d;
            };
        };
    };
}

// Variable scoping demonstration
var globalVar = "global";

function scopeDemo() {
    var functionVar = "function";
    let blockLet = "outer block";

    if (true) {
        var innerVar = "inner function scoped";
        let blockLet = "inner block";
        const blockConst = "block constant";

        console.log(globalVar, functionVar, innerVar, blockLet, blockConst);
    }

    console.log(globalVar, functionVar, innerVar, blockLet);
    // console.log(blockConst); // ReferenceError
}

// Hoisting demonstration
function hoistingDemo() {
    console.log(typeof hoisted); // "function"
    console.log(typeof varHoisted); // "undefined"

    function hoisted() {
        return "I'm hoisted";
    }

    var varHoisted = "Now I have a value";

    return hoisted();
}

// Module pattern
const Module = (function() {
    let privateData = [];

    function privateMethod() {
        return privateData.length;
    }

    return {
        add: function(item) {
            privateData.push(item);
            return privateMethod();
        },

        getCount: function() {
            return privateMethod();
        }
    };
})();

// Arrow function closure
const createMultiplier = (factor) => (value) => value * factor;

// Class with closure-like behavior
class ClosureClass {
    constructor(initialValue) {
        let privateValue = initialValue;

        this.getValue = function() {
            return privateValue;
        };

        this.setValue = function(value) {
            privateValue = value;
        };
    }

    getValueMethod() {
        return this.getValue();
    }
}

// Using all patterns
const counter = createCounter(10);
const count1 = counter(5);
const count2 = counter();

const multiLevel = multiLevelClosure(1)(2)(3);
const result = multiLevel(4);

scopeDemo();
const hoistedResult = hoistingDemo();

const moduleCount = Module.add("item1");
Module.add("item2");
const totalCount = Module.getCount();

const double = createMultiplier(2);
const doubled = double(5);

const closureInstance = new ClosureClass(100);
const value = closureInstance.getValue();
closureInstance.setValue(200);
const newValue = closureInstance.getValueMethod();

// Complex closure with loop
function createFunctions() {
    const functions = [];

    for (let i = 0; i < 3; i++) {
        functions.push(function() {
            return i;
        });
    }

    return functions;
}

const functions = createFunctions();
const results = functions.map(fn => fn());

// Closure accessing multiple scopes
let outerScope = "outer";

function accessMultipleScopes() {
    let middleScope = "middle";

    return function() {
        let innerScope = "inner";

        return function() {
            return { outerScope, middleScope, innerScope };
        };
    };
}

const accessScopes = accessMultipleScopes()();
const scopeResult = accessScopes();
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=javascript_closures_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    # Verify all relationship types exist
    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    call_relationships = [c for c in all_relationships if c.args[1] == "CALLS"]
    defines_relationships = [c for c in all_relationships if c.args[1] == "DEFINES"]

    # Should have comprehensive closure coverage
    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_closures" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 8, (
        f"Expected at least 8 comprehensive closure calls, found {len(comprehensive_calls)}"
    )

    # Verify relationship structure
    for relationship in comprehensive_calls:
        assert len(relationship.args) == 3, "Call relationship should have 3 args"
        assert relationship.args[1] == "CALLS", "Second arg should be 'CALLS'"

        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        # Source should be our test module
        assert "comprehensive_closures" in source_module, (
            f"Source module should contain test file name: {source_module}"
        )

        # Target should be a valid module name or function
        assert isinstance(target_module, str) and target_module, (
            f"Target should be non-empty string: {target_module}"
        )

    # Test that closure parsing doesn't interfere with other relationships
    assert defines_relationships, "Should still have DEFINES relationships"
