from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)


@pytest.fixture
def javascript_this_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with this binding patterns."""
    project_path = temp_repo / "javascript_this_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "utils").mkdir()

    (project_path / "utils" / "helpers.js").write_text(
        encoding="utf-8",
        data="""
export function helperFunction() {
    console.log('Helper this:', this);
    return this;
}

export const helperArrow = () => {
    console.log('Helper arrow this:', this);
    return this;
};
""",
    )

    return project_path


def test_this_in_different_contexts(
    javascript_this_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test 'this' binding in various contexts."""
    test_file = javascript_this_project / "this_contexts.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Global context
console.log(this); // window/global

function globalFunction() {
    console.log('globalFunction this:', this);
    return this;
}

// Object method context
const obj = {
    name: 'MyObject',

    method: function() {
        console.log('method this:', this);
        return this.name;
    },

    arrowMethod: () => {
        console.log('arrowMethod this:', this);
        return this; // lexical this
    },

    nestedMethod: function() {
        console.log('nestedMethod this:', this);

        const innerFunction = function() {
            console.log('innerFunction this:', this);
            return this;
        };

        const innerArrow = () => {
            console.log('innerArrow this:', this);
            return this; // lexical this from nestedMethod
        };

        return {
            inner: innerFunction(),
            arrow: innerArrow()
        };
    },

    callbackMethod: function() {
        const self = this; // Common pattern to preserve this

        setTimeout(function() {
            console.log('setTimeout function this:', this);
            console.log('self:', self);
        }, 100);

        setTimeout(() => {
            console.log('setTimeout arrow this:', this);
        }, 100);

        return this;
    }
};

// Constructor context
function Constructor(value) {
    this.value = value;
    console.log('Constructor this:', this);

    this.method = function() {
        console.log('Constructor method this:', this);
        return this.value;
    };

    this.arrowMethod = () => {
        console.log('Constructor arrow this:', this);
        return this.value;
    };
}

Constructor.prototype.protoMethod = function() {
    console.log('Proto method this:', this);
    return this.value;
};

// Class context
class MyClass {
    constructor(name) {
        this.name = name;
        console.log('Class constructor this:', this);

        // Bound method pattern
        this.boundMethod = this.method.bind(this);

        // Arrow property
        this.arrowProperty = () => {
            console.log('Arrow property this:', this);
            return this.name;
        };
    }

    method() {
        console.log('Class method this:', this);
        return this.name;
    }

    static staticMethod() {
        console.log('Static method this:', this);
        return this; // The class itself
    }
}

// Event handler context
const button = {
    click: function() {
        console.log('Button click this:', this);
    },

    addHandler: function() {
        // Traditional function loses this
        document.addEventListener('click', function() {
            console.log('Handler function this:', this); // document
        });

        // Arrow function preserves this
        document.addEventListener('click', () => {
            console.log('Handler arrow this:', this); // button object
        });

        // Bound function
        document.addEventListener('click', this.click.bind(this));
    }
};

// Call site determines this
const func = obj.method;
console.log(func()); // undefined (lost context)
console.log(obj.method()); // 'MyObject'

// Borrowing methods
const borrowed = {
    name: 'Borrowed'
};
console.log(obj.method.call(borrowed)); // 'Borrowed'

// Array methods and this
const numbers = [1, 2, 3];
numbers.forEach(function(num) {
    console.log('forEach this:', this);
}, obj); // Second argument is thisArg

// Using all contexts
globalFunction();
obj.method();
obj.arrowMethod();
obj.nestedMethod();
obj.callbackMethod();

const instance = new Constructor(42);
instance.method();
instance.arrowMethod();
instance.protoMethod();

const classInstance = new MyClass('ClassInstance');
classInstance.method();
classInstance.arrowProperty();
MyClass.staticMethod();

// Detached method
const detached = classInstance.method;
const bound = classInstance.boundMethod;
detached(); // undefined this
bound(); // preserved this
""",
    )

    run_updater(javascript_this_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    method_calls = get_nodes(mock_ingestor, "Method")

    all_callables = function_calls + method_calls
    this_context_callables = [
        call
        for call in all_callables
        if "this_contexts" in call[0][1]["qualified_name"]
    ]

    assert len(this_context_callables) >= 10, (
        f"Expected at least 10 functions/methods with this context, found {len(this_context_callables)}"
    )

    created_functions = get_qualified_names(function_calls)
    arrow_patterns = ["arrowMethod", "innerArrow", "arrowProperty"]

    arrow_functions_found = [
        func
        for func in created_functions
        if any(pattern in func for pattern in arrow_patterns)
    ]

    assert len(arrow_functions_found) >= 2, (
        f"Expected at least 2 arrow functions, found {len(arrow_functions_found)}"
    )


def test_bind_call_apply_methods(
    javascript_this_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test bind(), call(), and apply() method usage."""
    test_file = javascript_this_project / "bind_call_apply.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Function to be bound/called/applied
function greet(greeting, punctuation) {
    return `${greeting}, ${this.name}${punctuation}`;
}

const person1 = { name: 'Alice' };
const person2 = { name: 'Bob' };

// Using call()
console.log(greet.call(person1, 'Hello', '!'));
console.log(greet.call(person2, 'Hi', '.'));

// Using apply()
console.log(greet.apply(person1, ['Hello', '!']));
console.log(greet.apply(person2, ['Hi', '.']));

// Using bind()
const greetAlice = greet.bind(person1);
const greetBob = greet.bind(person2);

console.log(greetAlice('Hey', '!'));
console.log(greetBob('Howdy', '.'));

// Partial application with bind
const greetWithHello = greet.bind(null, 'Hello');
console.log(greetWithHello.call(person1, '!'));

// Method borrowing
const obj1 = {
    values: [1, 2, 3],

    sum: function() {
        return this.values.reduce((a, b) => a + b, 0);
    },

    multiply: function(factor) {
        return this.values.map(v => v * factor);
    }
};

const obj2 = {
    values: [4, 5, 6]
};

// Borrowing methods
console.log(obj1.sum.call(obj2)); // 15
console.log(obj1.multiply.apply(obj2, [2])); // [8, 10, 12]

// Binding methods
const boundSum = obj1.sum.bind(obj2);
console.log(boundSum()); // 15

// Hard binding pattern
function hardBind(fn, context) {
    return function bound(...args) {
        return fn.apply(context, args);
    };
}

const hardBoundGreet = hardBind(greet, person1);
console.log(hardBoundGreet('Bonjour', '!')); // Cannot be overridden

// Soft binding pattern
function softBind(fn, context) {
    return function bound(...args) {
        return fn.apply(
            (!this || this === window) ? context : this,
            args
        );
    };
}

const softBoundGreet = softBind(greet, person1);
console.log(softBoundGreet('Hola', '!')); // Uses person1
console.log(softBoundGreet.call(person2, 'Ciao', '!')); // Can override to person2

// Constructor binding
function Component(name) {
    this.name = name;

    // Binding in constructor
    this.handleClick = this.handleClick.bind(this);

    // Alternative: arrow function property
    this.handleHover = () => {
        console.log(`${this.name} hovered`);
    };
}

Component.prototype.handleClick = function() {
    console.log(`${this.name} clicked`);
};

Component.prototype.render = function() {
    // Method needs binding when passed as callback
    setTimeout(this.handleClick, 100);
    setTimeout(this.handleHover, 100);

    // Or use arrow function
    setTimeout(() => this.handleClick(), 100);
};

// Array method binding
const processor = {
    factor: 10,

    processArray: function(arr) {
        // Need to bind or use arrow function
        return arr.map(function(item) {
            return item * this.factor;
        }.bind(this));

        // Or use arrow function
        // return arr.map(item => item * this.factor);
    },

    filterArray: function(arr) {
        const self = this;
        return arr.filter(function(item) {
            return item > self.factor;
        });
    }
};

// Function currying with bind
function multiply(a, b, c) {
    return a * b * c;
}

const multiplyBy2 = multiply.bind(null, 2);
const multiplyBy2And3 = multiply.bind(null, 2, 3);

console.log(multiplyBy2(3, 4)); // 24
console.log(multiplyBy2And3(4)); // 24

// Polyfill-like implementation
Function.prototype.myBind = function(context, ...args1) {
    const fn = this;
    return function bound(...args2) {
        return fn.apply(context, [...args1, ...args2]);
    };
};

const myBoundGreet = greet.myBind(person1, 'Salut');
console.log(myBoundGreet('!'));

// Using bind/call/apply
const component = new Component('MyComponent');
component.render();

const result1 = processor.processArray([1, 2, 3]);
const result2 = processor.filterArray([5, 10, 15, 20]);

console.log(result1); // [10, 20, 30]
console.log(result2); // [15, 20]
""",
    )

    run_updater(javascript_this_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    bind_call_apply_calls = [
        call
        for call in call_relationships
        if "bind_call_apply" in call.args[0][2]
        and any(
            method in str(call.args[2][2]) for method in [".bind", ".call", ".apply"]
        )
    ]

    assert len(bind_call_apply_calls) >= 5, (
        f"Expected at least 5 bind/call/apply calls, found {len(bind_call_apply_calls)}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")
    project_name = javascript_this_project.name

    expected_functions = [
        f"{project_name}.bind_call_apply.greet",
        f"{project_name}.bind_call_apply.hardBind",
        f"{project_name}.bind_call_apply.softBind",
        f"{project_name}.bind_call_apply.Component",
        f"{project_name}.bind_call_apply.multiply",
    ]

    for expected in expected_functions:
        assert expected in created_functions, f"Missing function: {expected}"


def test_arrow_functions_lexical_this(
    javascript_this_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test arrow functions and lexical this binding."""
    test_file = javascript_this_project / "arrow_lexical_this.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Arrow functions and lexical this

// Global arrow function
const globalArrow = () => {
    console.log('Global arrow this:', this);
    return this;
};

// Object with arrow functions
const obj = {
    name: 'Object',

    // Arrow function as method (problematic)
    arrowMethod: () => {
        console.log('Arrow method this:', this); // Not obj!
        return this.name; // undefined
    },

    // Regular method with arrow function inside
    regularMethod: function() {
        console.log('Regular method this:', this);

        // Arrow function captures this from regularMethod
        const arrow = () => {
            console.log('Inner arrow this:', this);
            return this.name;
        };

        // Regular function loses this
        const regular = function() {
            console.log('Inner regular this:', this);
            return this.name;
        };

        return {
            arrow: arrow(),
            regular: regular(),
            boundRegular: regular.bind(this)()
        };
    },

    // Common patterns
    delayedMethod: function() {
        // Problem: setTimeout with regular function
        setTimeout(function() {
            console.log('SetTimeout regular:', this); // window/global
        }, 100);

        // Solution 1: Arrow function
        setTimeout(() => {
            console.log('SetTimeout arrow:', this); // obj
            this.name = 'Modified';
        }, 100);

        // Solution 2: bind
        setTimeout(function() {
            console.log('SetTimeout bound:', this); // obj
        }.bind(this), 100);

        // Solution 3: self/that pattern
        const self = this;
        setTimeout(function() {
            console.log('SetTimeout self:', self); // obj
        }, 100);
    },

    // Array methods with arrow functions
    processArray: function(arr) {
        // Arrow function preserves this
        const doubled = arr.map(n => n * 2);

        const processed = arr.map(n => {
            console.log('Map arrow this:', this);
            return n + this.name.length;
        });

        const filtered = arr.filter(n => {
            return n > this.name.length;
        });

        return { doubled, processed, filtered };
    }
};

// Constructor with arrow functions
function Widget(id) {
    this.id = id;

    // Arrow function property
    this.getId = () => {
        console.log('Widget arrow property this:', this);
        return this.id;
    };

    // Regular method
    this.regularGetId = function() {
        console.log('Widget regular method this:', this);
        return this.id;
    };
}

Widget.prototype.protoMethod = function() {
    // Arrow functions in prototype methods
    const arrow = () => {
        console.log('Proto arrow this:', this);
        return this.id;
    };

    return arrow();
};

// Class with arrow functions
class Component {
    constructor(name) {
        this.name = name;

        // Arrow function property (bound to instance)
        this.handleClick = () => {
            console.log('Class arrow property this:', this);
            return this.name;
        };
    }

    // Regular method
    regularMethod() {
        // Arrow function inside method
        const helper = () => {
            console.log('Class method arrow this:', this);
            return this.name;
        };

        return helper();
    }

    // Arrow function in class field (ES2022)
    arrowField = () => {
        console.log('Class arrow field this:', this);
        return this.name;
    };
}

// Event handlers with arrow functions
class Button {
    constructor(label) {
        this.label = label;
        this.clickCount = 0;
    }

    // Problem: regular method as event handler
    handleClickProblem() {
        this.clickCount++; // this is undefined/wrong
        console.log(`${this.label} clicked ${this.clickCount} times`);
    }

    // Solution 1: Arrow function property
    handleClickArrow = () => {
        this.clickCount++;
        console.log(`${this.label} clicked ${this.clickCount} times`);
    };

    // Solution 2: Bind in constructor
    constructor2(label) {
        this.label = label;
        this.clickCount = 0;
        this.handleClickBound = this.handleClickProblem.bind(this);
    }

    attach() {
        // Problem
        element.addEventListener('click', this.handleClickProblem); // loses this

        // Solutions
        element.addEventListener('click', this.handleClickArrow); // preserves this
        element.addEventListener('click', this.handleClickBound); // preserves this
        element.addEventListener('click', () => this.handleClickProblem()); // preserves this
    }
}

// Arrow functions and arguments
function regularFunction() {
    console.log('Regular arguments:', arguments);

    const arrow = () => {
        console.log('Arrow arguments:', arguments); // Parent's arguments
    };

    arrow(1, 2, 3); // Still shows regularFunction's arguments
}

// Using arrow functions
console.log(globalArrow());
console.log(obj.arrowMethod()); // undefined (wrong this)
console.log(obj.regularMethod()); // Works correctly
obj.delayedMethod();
console.log(obj.processArray([1, 2, 3, 4, 5]));

const widget = new Widget(123);
console.log(widget.getId()); // 123
console.log(widget.regularGetId()); // 123

const detachedArrow = widget.getId;
const detachedRegular = widget.regularGetId;
console.log(detachedArrow()); // Still 123 (bound)
console.log(detachedRegular()); // undefined (lost context)

const component = new Component('MyComponent');
console.log(component.handleClick());
console.log(component.regularMethod());
console.log(component.arrowField());

regularFunction(10, 20, 30);
""",
    )

    run_updater(javascript_this_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    arrow_functions = [
        call
        for call in function_calls
        if "arrow_lexical_this" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["arrow", "Arrow", "handleClick"]
        )
    ]

    assert len(arrow_functions) >= 5, (
        f"Expected at least 5 arrow functions, found {len(arrow_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    classes_with_arrows = [
        call
        for call in class_calls
        if "arrow_lexical_this" in call[0][1]["qualified_name"]
    ]

    assert len(classes_with_arrows) >= 2, (
        f"Expected at least 2 classes with arrow functions, found {len(classes_with_arrows)}"
    )


def test_this_in_callbacks_and_events(
    javascript_this_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test this binding in callbacks and event handlers."""
    test_file = javascript_this_project / "callbacks_events.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Callbacks and event handlers

// Timer callbacks
const timer = {
    seconds: 0,

    start: function() {
        // Problem: loses this in callback
        setInterval(function() {
            this.seconds++; // this is window/global
            console.log(this.seconds); // NaN
        }, 1000);
    },

    startFixed1: function() {
        // Solution 1: arrow function
        setInterval(() => {
            this.seconds++;
            console.log(this.seconds);
        }, 1000);
    },

    startFixed2: function() {
        // Solution 2: bind
        setInterval(function() {
            this.seconds++;
            console.log(this.seconds);
        }.bind(this), 1000);
    },

    startFixed3: function() {
        // Solution 3: closure
        const self = this;
        setInterval(function() {
            self.seconds++;
            console.log(self.seconds);
        }, 1000);
    }
};

// Array method callbacks
const arrayProcessor = {
    multiplier: 10,
    offset: 5,

    processNumbers: function(numbers) {
        // map with regular function - needs binding
        const mapped1 = numbers.map(function(n) {
            return n * this.multiplier; // undefined without binding
        }, this); // thisArg parameter

        // map with arrow function - automatic binding
        const mapped2 = numbers.map(n => n * this.multiplier);

        // filter with bound function
        const filtered = numbers.filter(function(n) {
            return n > this.offset;
        }.bind(this));

        // reduce with arrow function
        const sum = numbers.reduce((acc, n) => {
            return acc + n + this.offset;
        }, 0);

        return { mapped1, mapped2, filtered, sum };
    }
};

// Promise callbacks
const promiseHandler = {
    name: 'PromiseHandler',

    fetchData: function() {
        // Problem with regular function
        fetch('/api/data')
            .then(function(response) {
                console.log(this.name); // undefined
                return response.json();
            })
            .then(function(data) {
                this.processData(data); // Error: this.processData is not a function
            });
    },

    fetchDataFixed: function() {
        // Solution with arrow functions
        fetch('/api/data')
            .then(response => {
                console.log(this.name); // 'PromiseHandler'
                return response.json();
            })
            .then(data => {
                this.processData(data); // Works
            })
            .catch(error => {
                this.handleError(error); // Works
            });
    },

    async fetchDataAsync() {
        try {
            // async/await preserves this
            const response = await fetch('/api/data');
            console.log(this.name); // 'PromiseHandler'
            const data = await response.json();
            this.processData(data); // Works
        } catch (error) {
            this.handleError(error); // Works
        }
    },

    processData: function(data) {
        console.log('Processing:', data);
    },

    handleError: function(error) {
        console.error('Error:', error);
    }
};

// Event listener patterns
class UIComponent {
    constructor(element) {
        this.element = element;
        this.clickCount = 0;

        // Bind methods in constructor
        this.handleClick = this.handleClick.bind(this);
        this.handleMouseOver = this.handleMouseOver.bind(this);

        // Arrow function property
        this.handleMouseOut = () => {
            console.log('Mouse out:', this.element);
        };
    }

    handleClick(event) {
        this.clickCount++;
        console.log(`Clicked ${this.clickCount} times`);
        console.log('This:', this); // UIComponent instance
        console.log('Event target:', event.target);
    }

    handleMouseOver(event) {
        console.log('Mouse over:', this.element);
    }

    attachEvents() {
        // Method 1: Pre-bound method
        this.element.addEventListener('click', this.handleClick);

        // Method 2: Arrow function wrapper
        this.element.addEventListener('mouseover', (e) => this.handleMouseOver(e));

        // Method 3: Arrow function property
        this.element.addEventListener('mouseout', this.handleMouseOut);

        // Problem: Direct method reference
        this.element.addEventListener('dblclick', this.handleDoubleClick); // loses this
    }

    handleDoubleClick(event) {
        console.log('Double click this:', this); // element, not component
    }

    removeEvents() {
        // Can remove pre-bound methods
        this.element.removeEventListener('click', this.handleClick);
        this.element.removeEventListener('mouseout', this.handleMouseOut);

        // Cannot remove inline arrow functions!
        // this.element.removeEventListener('mouseover', (e) => this.handleMouseOver(e)); // Won't work
    }
}

// jQuery-like patterns
const $element = {
    on: function(event, handler) {
        // jQuery binds this to the element
        const boundHandler = handler.bind(this.element);
        this.element.addEventListener(event, boundHandler);
    },

    each: function(callback) {
        this.elements.forEach((element, index) => {
            // jQuery-like: this is the element in callback
            callback.call(element, index, element);
        });
    }
};

// Custom event emitter
class EventEmitter {
    constructor() {
        this.events = {};
    }

    on(event, callback) {
        if (!this.events[event]) {
            this.events[event] = [];
        }
        this.events[event].push(callback);
    }

    emit(event, ...args) {
        if (!this.events[event]) return;

        this.events[event].forEach(callback => {
            // Preserve the original this of callback
            callback.apply(this, args);
        });
    }

    once(event, callback) {
        const wrapper = (...args) => {
            callback.apply(this, args);
            this.off(event, wrapper);
        };
        this.on(event, wrapper);
    }

    off(event, callback) {
        if (!this.events[event]) return;
        this.events[event] = this.events[event].filter(cb => cb !== callback);
    }
}

// Debounce/throttle patterns
function debounce(func, wait) {
    let timeout;

    return function debounced(...args) {
        const context = this; // Preserve this

        clearTimeout(timeout);
        timeout = setTimeout(() => {
            func.apply(context, args); // Apply with correct this
        }, wait);
    };
}

const searchHandler = {
    query: '',

    search: function(term) {
        this.query = term;
        console.log(`Searching for: ${this.query}`);
    },

    // Debounced version preserves this
    debouncedSearch: debounce(function(term) {
        this.search(term);
    }, 300)
};

// Using callbacks and events
const component = new UIComponent(document.getElementById('myElement'));
component.attachEvents();

const emitter = new EventEmitter();
emitter.on('data', function(data) {
    console.log('Data received:', data);
    console.log('Emitter this:', this); // EventEmitter instance
});

searchHandler.debouncedSearch('test query');
""",
    )

    run_updater(javascript_this_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    callback_functions = [
        call
        for call in function_calls
        if "callbacks_events" in call[0][1]["qualified_name"]
    ]

    assert len(callback_functions) >= 10, (
        f"Expected at least 10 callback-related functions, found {len(callback_functions)}"
    )

    method_calls = get_nodes(mock_ingestor, "Method")

    event_methods = [
        call
        for call in method_calls
        if "callbacks_events" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["handle", "attach", "emit"]
        )
    ]

    assert len(event_methods) >= 3, (
        f"Expected at least 3 event handler methods, found {len(event_methods)}"
    )


def test_this_comprehensive(
    javascript_this_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all this binding patterns are covered."""
    test_file = javascript_this_project / "comprehensive_this.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript this binding pattern in one file

// Global context
const globalThis = this;

function globalFunction() {
    'use strict';
    return this; // undefined in strict mode
}

// Object method
const obj = {
    prop: 'value',

    method() {
        return this.prop;
    },

    arrowMethod: () => {
        return this; // lexical this
    }
};

// Constructor
function Constructor(value) {
    this.value = value;
}

Constructor.prototype.getValue = function() {
    return this.value;
};

// Class
class MyClass {
    constructor(name) {
        this.name = name;
        this.boundMethod = this.method.bind(this);
        this.arrowProperty = () => this.name;
    }

    method() {
        return this.name;
    }
}

// bind/call/apply
const bound = obj.method.bind(obj);
const result1 = obj.method.call({ prop: 'different' });
const result2 = obj.method.apply({ prop: 'another' });

// Arrow function
const arrow = () => this;
const innerArrow = function() {
    return () => this;
};

// Callback
setTimeout(function() {
    console.log(this); // global/window
}, 0);

setTimeout(() => {
    console.log(this); // lexical this
}, 0);

// Event handler
const handler = {
    handleEvent() {
        console.log(this); // handler object
    }
};

// Using all patterns
const instance = new Constructor(42);
const classInstance = new MyClass('Test');

console.log(globalFunction());
console.log(obj.method());
console.log(obj.arrowMethod());
console.log(instance.getValue());
console.log(classInstance.method());
console.log(classInstance.boundMethod());
console.log(classInstance.arrowProperty());
console.log(bound());
console.log(arrow());

// Method borrowing
const borrowed = obj.method;
console.log(borrowed()); // undefined

// This in nested functions
function outer() {
    console.log('Outer this:', this);

    function inner() {
        console.log('Inner this:', this);
    }

    const innerArrow = () => {
        console.log('Inner arrow this:', this);
    };

    inner();
    innerArrow();
}

outer.call({ context: 'custom' });
""",
    )

    run_updater(javascript_this_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    comprehensive_calls = [
        call for call in calls_relationships if "comprehensive_this" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 5, (
        f"Expected at least 5 comprehensive this-related calls, found {len(comprehensive_calls)}"
    )

    binding_calls = [
        call
        for call in comprehensive_calls
        if any(
            method in str(call.args[2][2]) for method in [".bind", ".call", ".apply"]
        )
    ]

    assert len(binding_calls) >= 2, (
        f"Expected at least 2 bind/call/apply calls, found {len(binding_calls)}"
    )
