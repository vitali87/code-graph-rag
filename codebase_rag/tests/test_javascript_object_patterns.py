from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_relationships,
    run_updater,
)


@pytest.fixture
def javascript_object_patterns_project(temp_repo: Path) -> Path:
    """Create a comprehensive JavaScript project with object patterns."""
    project_path = temp_repo / "javascript_object_patterns_test"
    project_path.mkdir()

    (project_path / "patterns").mkdir()
    (project_path / "factories").mkdir()

    (project_path / "patterns" / "basic.js").write_text(
        encoding="utf-8",
        data="""
export function createUser(name, email) {
    return {
        name,
        email,
        greet() {
            return `Hello, I'm ${this.name}`;
        }
    };
}

export const userFactory = {
    create(name, email) {
        return createUser(name, email);
    }
};
""",
    )

    return project_path


def test_object_literals(
    javascript_object_patterns_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test object literal patterns and syntax."""
    test_file = javascript_object_patterns_project / "object_literals.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic object literals

// Simple object literal
const person = {
    name: 'Alice',
    age: 30,
    city: 'New York'
};

// Object with methods
const calculator = {
    result: 0,

    add(value) {
        this.result += value;
        return this;
    },

    subtract(value) {
        this.result -= value;
        return this;
    },

    multiply(value) {
        this.result *= value;
        return this;
    },

    divide(value) {
        if (value !== 0) {
            this.result /= value;
        }
        return this;
    },

    getValue() {
        return this.result;
    },

    reset() {
        this.result = 0;
        return this;
    }
};

// Object with computed property names
const dynamicKey = 'status';
const config = {
    name: 'MyApp',
    version: '1.0.0',
    [dynamicKey]: 'active',
    [`${dynamicKey}_message`]: 'Running',
    ['feature_' + Math.random()]: true
};

// Object with property shorthand
function createPoint(x, y) {
    return {
        x,  // Same as x: x
        y,  // Same as y: y

        // Method shorthand
        toString() {
            return `(${this.x}, ${this.y})`;
        },

        distance(other) {
            const dx = this.x - other.x;
            const dy = this.y - other.y;
            return Math.sqrt(dx * dx + dy * dy);
        },

        // Getter/setter shorthand
        get magnitude() {
            return Math.sqrt(this.x * this.x + this.y * this.y);
        },

        set magnitude(value) {
            const currentMag = this.magnitude;
            if (currentMag !== 0) {
                const scale = value / currentMag;
                this.x *= scale;
                this.y *= scale;
            }
        }
    };
}

// Nested objects
const user = {
    personal: {
        name: 'John',
        age: 25,
        address: {
            street: '123 Main St',
            city: 'Boston',
            country: 'USA',

            getFullAddress() {
                return `${this.street}, ${this.city}, ${this.country}`;
            }
        }
    },

    professional: {
        company: 'TechCorp',
        position: 'Developer',
        skills: ['JavaScript', 'Python', 'React'],

        addSkill(skill) {
            if (!this.skills.includes(skill)) {
                this.skills.push(skill);
            }
        },

        hasSkill(skill) {
            return this.skills.includes(skill);
        }
    },

    // Method accessing nested properties
    getFullProfile() {
        return {
            name: this.personal.name,
            company: this.professional.company,
            location: this.personal.address.city,
            skills: [...this.professional.skills]
        };
    }
};

// Object with array properties and methods
const playlist = {
    name: 'My Favorites',
    songs: [],
    currentIndex: 0,

    add(song) {
        this.songs.push({
            ...song,
            id: Date.now() + Math.random(),
            addedAt: new Date()
        });
        return this;
    },

    remove(id) {
        this.songs = this.songs.filter(song => song.id !== id);
        return this;
    },

    play(index = this.currentIndex) {
        if (index >= 0 && index < this.songs.length) {
            this.currentIndex = index;
            console.log(`Playing: ${this.songs[index].title}`);
            return this.songs[index];
        }
        return null;
    },

    next() {
        const nextIndex = (this.currentIndex + 1) % this.songs.length;
        return this.play(nextIndex);
    },

    previous() {
        const prevIndex = this.currentIndex - 1 < 0
            ? this.songs.length - 1
            : this.currentIndex - 1;
        return this.play(prevIndex);
    },

    shuffle() {
        for (let i = this.songs.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [this.songs[i], this.songs[j]] = [this.songs[j], this.songs[i]];
        }
        return this;
    },

    get length() {
        return this.songs.length;
    },

    get current() {
        return this.songs[this.currentIndex];
    }
};

// Object with Symbol properties
const secretKey = Symbol('secret');
const publicKey = Symbol.for('public');

const secureObject = {
    publicData: 'visible',
    [secretKey]: 'hidden',
    [publicKey]: 'shared',

    getSecret() {
        return this[secretKey];
    },

    setSecret(value) {
        this[secretKey] = value;
    }
};

// Object with function properties
const mathUtils = {
    pi: Math.PI,
    e: Math.E,

    // Regular function property
    square: function(x) {
        return x * x;
    },

    // Arrow function property
    cube: (x) => x * x * x,

    // Method shorthand
    power(base, exponent) {
        return Math.pow(base, exponent);
    },

    // Generator method
    *fibonacci(n) {
        let a = 0, b = 1;
        for (let i = 0; i < n; i++) {
            yield a;
            [a, b] = [b, a + b];
        }
    },

    // Async method
    async delay(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
};

// Using object literals
const point1 = createPoint(3, 4);
const point2 = createPoint(0, 0);

console.log(point1.toString()); // "(3, 4)"
console.log(point1.magnitude); // 5
console.log(point1.distance(point2)); // 5

calculator.add(10).multiply(2).subtract(5);
console.log(calculator.getValue()); // 15

playlist
    .add({ title: 'Song 1', artist: 'Artist 1' })
    .add({ title: 'Song 2', artist: 'Artist 2' })
    .shuffle()
    .play();

console.log(user.getFullProfile());
console.log(user.personal.address.getFullAddress());

// Math utils usage
console.log(mathUtils.square(5)); // 25
console.log(mathUtils.cube(3)); // 27
console.log(mathUtils.power(2, 8)); // 256

for (const num of mathUtils.fibonacci(5)) {
    console.log(num); // 0, 1, 1, 2, 3
}

// Secure object
console.log(secureObject.publicData); // visible
console.log(secureObject.getSecret()); // hidden
console.log(secureObject[Symbol.for('public')]); // shared
""",
    )

    run_updater(javascript_object_patterns_project, mock_ingestor)

    project_name = javascript_object_patterns_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_functions = [
        f"{project_name}.object_literals.createPoint",
    ]

    for expected in expected_functions:
        assert expected in created_functions, (
            f"Missing object literal function: {expected}"
        )

    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    object_like_nodes = [
        call
        for call in all_nodes
        if "object_literals" in call[0][1].get("qualified_name", "")
        and any(
            pattern in call[0][1].get("qualified_name", "")
            for pattern in ["calculator", "user", "playlist", "mathUtils"]
        )
    ]

    assert len(object_like_nodes) >= 2, (
        f"Expected at least 2 object-like nodes, found {len(object_like_nodes)}"
    )


def test_factory_functions(
    javascript_object_patterns_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test factory function patterns."""
    test_file = javascript_object_patterns_project / "factory_functions.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Factory function patterns

// Basic factory function
function createUser(name, email, role = 'user') {
    return {
        name,
        email,
        role,
        id: Math.random().toString(36).substr(2, 9),
        createdAt: new Date(),

        getProfile() {
            return {
                name: this.name,
                email: this.email,
                role: this.role,
                id: this.id
            };
        },

        updateEmail(newEmail) {
            this.email = newEmail;
            return this;
        },

        hasRole(requiredRole) {
            return this.role === requiredRole;
        }
    };
}

// Factory with private variables (closure)
function createCounter(initialValue = 0) {
    let count = initialValue;

    return {
        get value() {
            return count;
        },

        increment(step = 1) {
            count += step;
            return this;
        },

        decrement(step = 1) {
            count -= step;
            return this;
        },

        reset() {
            count = initialValue;
            return this;
        },

        // Private variable is truly private
        // No direct access to 'count' from outside
    };
}

// Factory with configuration object
function createApiClient(config) {
    const {
        baseURL,
        timeout = 5000,
        retries = 3,
        headers = {},
        ...options
    } = config;

    return {
        baseURL,
        timeout,
        retries,
        headers: { ...headers },

        async request(endpoint, options = {}) {
            const url = `${this.baseURL}${endpoint}`;
            const requestOptions = {
                timeout: this.timeout,
                headers: { ...this.headers, ...options.headers },
                ...options
            };

            let attempts = 0;
            while (attempts <= this.retries) {
                try {
                    const response = await fetch(url, requestOptions);
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    return response.json();
                } catch (error) {
                    attempts++;
                    if (attempts > this.retries) throw error;
                    await this.delay(1000 * attempts);
                }
            }
        },

        async get(endpoint, options = {}) {
            return this.request(endpoint, { method: 'GET', ...options });
        },

        async post(endpoint, data, options = {}) {
            return this.request(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
                ...options
            });
        },

        async put(endpoint, data, options = {}) {
            return this.request(endpoint, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
                ...options
            });
        },

        async delete(endpoint, options = {}) {
            return this.request(endpoint, { method: 'DELETE', ...options });
        },

        setHeader(name, value) {
            this.headers[name] = value;
            return this;
        },

        removeHeader(name) {
            delete this.headers[name];
            return this;
        },

        delay(ms) {
            return new Promise(resolve => setTimeout(resolve, ms));
        }
    };
}

// Factory with multiple creation methods
const userFactory = {
    createBasic(name, email) {
        return createUser(name, email, 'user');
    },

    createAdmin(name, email) {
        return createUser(name, email, 'admin');
    },

    createModerator(name, email) {
        return createUser(name, email, 'moderator');
    },

    createFromData(userData) {
        const { name, email, role, ...extra } = userData;
        const user = createUser(name, email, role);
        return { ...user, ...extra };
    },

    createBatch(usersData) {
        return usersData.map(data => this.createFromData(data));
    }
};

// Factory with inheritance-like patterns
function createVehicle(type, make, model) {
    return {
        type,
        make,
        model,
        speed: 0,

        start() {
            console.log(`${this.make} ${this.model} started`);
            return this;
        },

        stop() {
            this.speed = 0;
            console.log(`${this.make} ${this.model} stopped`);
            return this;
        },

        accelerate(amount) {
            this.speed += amount;
            console.log(`Speed: ${this.speed}`);
            return this;
        },

        getInfo() {
            return `${this.type}: ${this.make} ${this.model}`;
        }
    };
}

function createCar(make, model, doors = 4) {
    const vehicle = createVehicle('Car', make, model);

    return {
        ...vehicle,
        doors,

        // Override/extend methods
        start() {
            console.log('Car engine starting...');
            return vehicle.start.call(this);
        },

        honk() {
            console.log('Beep beep!');
            return this;
        },

        getInfo() {
            return `${vehicle.getInfo.call(this)} (${this.doors} doors)`;
        }
    };
}

function createMotorcycle(make, model, engineSize) {
    const vehicle = createVehicle('Motorcycle', make, model);

    return {
        ...vehicle,
        engineSize,

        start() {
            console.log('Motorcycle engine roaring...');
            return vehicle.start.call(this);
        },

        wheelie() {
            if (this.speed > 10) {
                console.log('Doing a wheelie!');
            } else {
                console.log('Need more speed for wheelie');
            }
            return this;
        },

        getInfo() {
            return `${vehicle.getInfo.call(this)} (${this.engineSize}cc)`;
        }
    };
}

// Factory with mixin pattern
function createMixins() {
    return {
        eventEmitter: {
            on(event, callback) {
                this._events = this._events || {};
                this._events[event] = this._events[event] || [];
                this._events[event].push(callback);
                return this;
            },

            emit(event, ...args) {
                if (this._events && this._events[event]) {
                    this._events[event].forEach(callback => callback(...args));
                }
                return this;
            },

            off(event, callback) {
                if (this._events && this._events[event]) {
                    this._events[event] = this._events[event].filter(cb => cb !== callback);
                }
                return this;
            }
        },

        observable: {
            subscribe(observer) {
                this._observers = this._observers || [];
                this._observers.push(observer);
                return this;
            },

            unsubscribe(observer) {
                if (this._observers) {
                    this._observers = this._observers.filter(obs => obs !== observer);
                }
                return this;
            },

            notify(data) {
                if (this._observers) {
                    this._observers.forEach(observer => observer(data));
                }
                return this;
            }
        }
    };
}

function createObservableModel(initialData = {}) {
    const mixins = createMixins();

    return {
        ...mixins.eventEmitter,
        ...mixins.observable,
        data: { ...initialData },

        set(key, value) {
            const oldValue = this.data[key];
            this.data[key] = value;
            this.emit('change', { key, value, oldValue });
            this.notify({ key, value, oldValue });
            return this;
        },

        get(key) {
            return this.data[key];
        },

        has(key) {
            return key in this.data;
        },

        delete(key) {
            const oldValue = this.data[key];
            delete this.data[key];
            this.emit('delete', { key, oldValue });
            this.notify({ type: 'delete', key, oldValue });
            return this;
        },

        toJSON() {
            return { ...this.data };
        }
    };
}

// Using factory functions
const user1 = createUser('Alice', 'alice@example.com');
const user2 = userFactory.createAdmin('Bob', 'bob@example.com');

console.log(user1.getProfile());
console.log(user2.hasRole('admin')); // true

const counter = createCounter(10);
counter.increment(5).decrement(2);
console.log(counter.value); // 13

const apiClient = createApiClient({
    baseURL: 'https://api.example.com',
    headers: { 'Authorization': 'Bearer token123' }
});

apiClient.setHeader('User-Agent', 'MyApp/1.0');

const car = createCar('Toyota', 'Camry');
const motorcycle = createMotorcycle('Honda', 'CBR600RR', 600);

car.start().accelerate(30).honk();
motorcycle.start().accelerate(50).wheelie();

console.log(car.getInfo());
console.log(motorcycle.getInfo());

const model = createObservableModel({ name: 'Test', count: 0 });

model.on('change', (data) => {
    console.log('Model changed:', data);
});

model.subscribe((data) => {
    console.log('Observer notified:', data);
});

model.set('name', 'Updated').set('count', 42);

// Batch user creation
const users = userFactory.createBatch([
    { name: 'User1', email: 'user1@test.com', role: 'user' },
    { name: 'User2', email: 'user2@test.com', role: 'admin' }
]);

console.log(users.map(u => u.getProfile()));
""",
    )

    run_updater(javascript_object_patterns_project, mock_ingestor)

    project_name = javascript_object_patterns_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_factories = [
        f"{project_name}.factory_functions.createUser",
        f"{project_name}.factory_functions.createCounter",
        f"{project_name}.factory_functions.createApiClient",
        f"{project_name}.factory_functions.createVehicle",
        f"{project_name}.factory_functions.createCar",
        f"{project_name}.factory_functions.createMotorcycle",
        f"{project_name}.factory_functions.createMixins",
        f"{project_name}.factory_functions.createObservableModel",
    ]

    found_factories = [func for func in expected_factories if func in created_functions]

    assert len(found_factories) >= 6, (
        f"Expected at least 6 factory functions, found {len(found_factories)}"
    )

    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    factory_object_nodes = [
        call
        for call in all_nodes
        if "factory_functions" in call[0][1].get("qualified_name", "")
        and "userFactory" in call[0][1].get("qualified_name", "")
    ]

    assert len(factory_object_nodes) >= 1, (
        f"Expected userFactory object methods, found {len(factory_object_nodes)}"
    )


def test_constructor_patterns(
    javascript_object_patterns_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test constructor function patterns."""
    test_file = javascript_object_patterns_project / "constructor_patterns.js"
    test_file.write_text(
        encoding="utf-8",
        data=r"""
// Constructor function patterns

// Basic constructor
function Person(name, age) {
    this.name = name;
    this.age = age;
    this.id = Math.random().toString(36).substr(2, 9);

    // Method on instance (not recommended - creates copy for each instance)
    this.greet = function() {
        return `Hello, I'm ${this.name}`;
    };
}

// Methods on prototype (recommended)
Person.prototype.introduce = function() {
    return `Hi, my name is ${this.name} and I'm ${this.age} years old`;
};

Person.prototype.birthday = function() {
    this.age++;
    return this;
};

Person.prototype.rename = function(newName) {
    this.name = newName;
    return this;
};

// Static methods
Person.isValidAge = function(age) {
    return typeof age === 'number' && age >= 0 && age <= 150;
};

Person.fromString = function(str) {
    const [name, age] = str.split(',');
    return new Person(name.trim(), parseInt(age.trim()));
};

// Constructor with default parameters
function Vehicle(make, model, year = new Date().getFullYear()) {
    this.make = make;
    this.model = model;
    this.year = year;
    this.mileage = 0;
    this.isRunning = false;
}

Vehicle.prototype.start = function() {
    this.isRunning = true;
    console.log(`${this.make} ${this.model} started`);
    return this;
};

Vehicle.prototype.stop = function() {
    this.isRunning = false;
    console.log(`${this.make} ${this.model} stopped`);
    return this;
};

Vehicle.prototype.drive = function(miles) {
    if (this.isRunning) {
        this.mileage += miles;
        console.log(`Drove ${miles} miles. Total: ${this.mileage}`);
    } else {
        console.log('Start the vehicle first!');
    }
    return this;
};

Vehicle.prototype.getAge = function() {
    return new Date().getFullYear() - this.year;
};

Vehicle.prototype.toString = function() {
    return `${this.year} ${this.make} ${this.model}`;
};

// Constructor inheritance pattern
function Car(make, model, year, doors) {
    // Call parent constructor
    Vehicle.call(this, make, model, year);
    this.doors = doors || 4;
    this.type = 'car';
}

// Inherit from Vehicle
Car.prototype = Object.create(Vehicle.prototype);
Car.prototype.constructor = Car;

// Override methods
Car.prototype.start = function() {
    console.log('Car engine starting...');
    return Vehicle.prototype.start.call(this);
};

// Add new methods
Car.prototype.honk = function() {
    console.log('Beep beep!');
    return this;
};

Car.prototype.openDoor = function(doorNumber) {
    if (doorNumber >= 1 && doorNumber <= this.doors) {
        console.log(`Door ${doorNumber} opened`);
    } else {
        console.log('Invalid door number');
    }
    return this;
};

// Another inheritance example
function Truck(make, model, year, payload) {
    Vehicle.call(this, make, model, year);
    this.payload = payload;
    this.type = 'truck';
    this.cargoWeight = 0;
}

Truck.prototype = Object.create(Vehicle.prototype);
Truck.prototype.constructor = Truck;

Truck.prototype.loadCargo = function(weight) {
    if (this.cargoWeight + weight <= this.payload) {
        this.cargoWeight += weight;
        console.log(`Loaded ${weight}lbs. Current load: ${this.cargoWeight}lbs`);
    } else {
        console.log('Payload exceeded!');
    }
    return this;
};

Truck.prototype.unloadCargo = function(weight = this.cargoWeight) {
    this.cargoWeight = Math.max(0, this.cargoWeight - weight);
    console.log(`Unloaded ${weight}lbs. Current load: ${this.cargoWeight}lbs`);
    return this;
};

// Constructor with private-like variables using closures
function BankAccount(accountNumber, initialBalance = 0) {
    let balance = initialBalance;
    let transactions = [];

    this.accountNumber = accountNumber;

    // Public methods with access to private variables
    this.deposit = function(amount) {
        if (amount > 0) {
            balance += amount;
            transactions.push({
                type: 'deposit',
                amount,
                balance,
                timestamp: new Date()
            });
            return this;
        }
        throw new Error('Deposit amount must be positive');
    };

    this.withdraw = function(amount) {
        if (amount > 0 && amount <= balance) {
            balance -= amount;
            transactions.push({
                type: 'withdrawal',
                amount,
                balance,
                timestamp: new Date()
            });
            return this;
        }
        throw new Error('Invalid withdrawal amount');
    };

    this.getBalance = function() {
        return balance;
    };

    this.getTransactions = function() {
        return [...transactions]; // Return copy
    };

    this.getStatement = function() {
        return {
            accountNumber: this.accountNumber,
            balance,
            transactionCount: transactions.length,
            lastTransaction: transactions[transactions.length - 1] || null
        };
    };
}

// Constructor with configuration object
function HttpClient(config = {}) {
    this.baseURL = config.baseURL || '';
    this.timeout = config.timeout || 5000;
    this.headers = { ...config.headers } || {};
    this.interceptors = {
        request: [],
        response: []
    };

    // Private method using arrow function to preserve 'this'
    this._makeRequest = async (url, options = {}) => {
        const fullURL = this.baseURL + url;
        const requestOptions = {
            timeout: this.timeout,
            headers: { ...this.headers, ...options.headers },
            ...options
        };

        // Apply request interceptors
        for (const interceptor of this.interceptors.request) {
            await interceptor(requestOptions);
        }

        try {
            const response = await fetch(fullURL, requestOptions);

            // Apply response interceptors
            for (const interceptor of this.interceptors.response) {
                await interceptor(response);
            }

            return response;
        } catch (error) {
            console.error('Request failed:', error);
            throw error;
        }
    };
}

HttpClient.prototype.get = function(url, options = {}) {
    return this._makeRequest(url, { method: 'GET', ...options });
};

HttpClient.prototype.post = function(url, data, options = {}) {
    return this._makeRequest(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        ...options
    });
};

HttpClient.prototype.addRequestInterceptor = function(interceptor) {
    this.interceptors.request.push(interceptor);
    return this;
};

HttpClient.prototype.addResponseInterceptor = function(interceptor) {
    this.interceptors.response.push(interceptor);
    return this;
};

// Constructor with validation
function Email(address) {
    if (!Email.isValid(address)) {
        throw new Error('Invalid email address');
    }

    this.address = address.toLowerCase();
    this.domain = address.split('@')[1];
    this.localPart = address.split('@')[0];
    this.createdAt = new Date();
}

Email.isValid = function(address) {
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    return emailRegex.test(address);
};

Email.prototype.toString = function() {
    return this.address;
};

Email.prototype.getDomain = function() {
    return this.domain;
};

Email.prototype.getLocalPart = function() {
    return this.localPart;
};

Email.prototype.equals = function(other) {
    return other instanceof Email && this.address === other.address;
};

// Using constructor patterns
const person1 = new Person('Alice', 25);
const person2 = Person.fromString('Bob, 30'); // Static method

console.log(person1.introduce());
person1.birthday().rename('Alice Smith');

const car = new Car('Toyota', 'Camry', 2020, 4);
const truck = new Truck('Ford', 'F-150', 2019, 2000);

car.start().drive(100).honk();
truck.start().loadCargo(500).drive(50);

console.log(car instanceof Vehicle); // true
console.log(car instanceof Car); // true
console.log(truck instanceof Vehicle); // true

const account = new BankAccount('ACC123', 1000);
account.deposit(500).withdraw(200);
console.log(account.getBalance()); // 1300
console.log(account.getStatement());

const httpClient = new HttpClient({
    baseURL: 'https://api.example.com',
    headers: { 'Authorization': 'Bearer token' }
});

httpClient.addRequestInterceptor(async (options) => {
    console.log('Request intercepted:', options);
});

try {
    const email = new Email('user@example.com');
    console.log(email.toString());
    console.log(email.getDomain());
} catch (error) {
    console.error(error.message);
}

// Constructor validation
console.log(Person.isValidAge(25)); // true
console.log(Person.isValidAge(200)); // false
console.log(Email.isValid('test@example.com')); // true
""",
    )

    run_updater(javascript_object_patterns_project, mock_ingestor)

    project_name = javascript_object_patterns_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_constructors = [
        f"{project_name}.constructor_patterns.Person",
        f"{project_name}.constructor_patterns.Vehicle",
        f"{project_name}.constructor_patterns.Car",
        f"{project_name}.constructor_patterns.Truck",
        f"{project_name}.constructor_patterns.BankAccount",
        f"{project_name}.constructor_patterns.HttpClient",
        f"{project_name}.constructor_patterns.Email",
    ]

    found_constructors = [
        func for func in expected_constructors if func in created_functions
    ]

    assert len(found_constructors) >= 5, (
        f"Expected at least 5 constructor functions, found {len(found_constructors)}"
    )

    inheritance_relationships = get_relationships(mock_ingestor, "INHERITS")

    constructor_inheritance = [
        call
        for call in inheritance_relationships
        if "constructor_patterns" in call.args[0][2]
    ]

    assert len(constructor_inheritance) >= 1, (
        f"Expected constructor inheritance relationships, found {len(constructor_inheritance)}"
    )


def test_object_composition(
    javascript_object_patterns_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test object composition and mixin patterns."""
    test_file = javascript_object_patterns_project / "object_composition.js"
    test_file.write_text(
        encoding="utf-8",
        data=r"""
// Object composition and mixin patterns

// Basic composition
const canWalk = {
    walk() {
        console.log(`${this.name} is walking`);
        return this;
    }
};

const canRun = {
    run() {
        console.log(`${this.name} is running`);
        return this;
    }
};

const canSwim = {
    swim() {
        console.log(`${this.name} is swimming`);
        return this;
    }
};

const canFly = {
    fly() {
        console.log(`${this.name} is flying`);
        return this;
    }
};

// Factory function using composition
function createAnimal(name, abilities = []) {
    const animal = { name };

    // Compose abilities
    abilities.forEach(ability => {
        Object.assign(animal, ability);
    });

    return animal;
}

function createHuman(name) {
    return createAnimal(name, [canWalk, canRun, canSwim]);
}

function createBird(name) {
    return createAnimal(name, [canWalk, canFly]);
}

function createFish(name) {
    return createAnimal(name, [canSwim]);
}

function createSuperhero(name) {
    const hero = createAnimal(name, [canWalk, canRun, canSwim, canFly]);

    // Add superhero-specific abilities
    return {
        ...hero,

        usePower(power) {
            console.log(`${this.name} uses ${power}!`);
            return this;
        },

        saveDay() {
            console.log(`${this.name} saves the day!`);
            return this;
        }
    };
}

// Mixin pattern
const EventEmitterMixin = {
    on(event, callback) {
        this._events = this._events || {};
        this._events[event] = this._events[event] || [];
        this._events[event].push(callback);
        return this;
    },

    emit(event, ...args) {
        if (this._events && this._events[event]) {
            this._events[event].forEach(callback => callback.apply(this, args));
        }
        return this;
    },

    off(event, callback) {
        if (this._events && this._events[event]) {
            this._events[event] = this._events[event].filter(cb => cb !== callback);
        }
        return this;
    }
};

const TimestampMixin = {
    touch() {
        this.lastModified = new Date();
        return this;
    },

    getAge() {
        if (!this.createdAt) return null;
        return Date.now() - this.createdAt.getTime();
    }
};

const ValidationMixin = {
    addValidator(field, validator) {
        this._validators = this._validators || {};
        this._validators[field] = this._validators[field] || [];
        this._validators[field].push(validator);
        return this;
    },

    validate(data = this) {
        if (!this._validators) return { valid: true, errors: [] };

        const errors = [];

        for (const [field, validators] of Object.entries(this._validators)) {
            const value = data[field];
            for (const validator of validators) {
                const result = validator(value, data);
                if (result !== true) {
                    errors.push({ field, message: result });
                }
            }
        }

        return {
            valid: errors.length === 0,
            errors
        };
    }
};

// Factory with multiple mixins
function createModel(data = {}) {
    const model = {
        ...data,
        createdAt: new Date(),

        set(key, value) {
            const oldValue = this[key];
            this[key] = value;
            this.touch();
            this.emit('change', { key, value, oldValue });
            return this;
        },

        get(key) {
            return this[key];
        },

        toJSON() {
            const result = {};
            for (const key in this) {
                if (typeof this[key] !== 'function' && !key.startsWith('_')) {
                    result[key] = this[key];
                }
            }
            return result;
        }
    };

    // Apply mixins
    Object.assign(model, EventEmitterMixin, TimestampMixin, ValidationMixin);

    return model;
}

// Advanced composition with method chaining
const ChainableMixin = {
    chain(method, ...args) {
        const result = this[method].apply(this, args);
        return result === undefined ? this : result;
    }
};

const LoggingMixin = {
    log(message, level = 'info') {
        const timestamp = new Date().toISOString();
        console.log(`[${timestamp}] [${level.toUpperCase()}] ${this.name || 'Unknown'}: ${message}`);
        return this;
    },

    debug(message) {
        return this.log(message, 'debug');
    },

    warn(message) {
        return this.log(message, 'warn');
    },

    error(message) {
        return this.log(message, 'error');
    }
};

// Service composition
function createService(name, config = {}) {
    const service = {
        name,
        config: { ...config },
        status: 'stopped',

        start() {
            this.status = 'running';
            this.log(`Service started with config: ${JSON.stringify(this.config)}`);
            this.emit('start');
            return this;
        },

        stop() {
            this.status = 'stopped';
            this.log('Service stopped');
            this.emit('stop');
            return this;
        },

        restart() {
            return this.stop().start();
        },

        configure(newConfig) {
            this.config = { ...this.config, ...newConfig };
            this.log('Service reconfigured');
            this.emit('configure', this.config);
            return this;
        }
    };

    // Apply mixins
    return Object.assign(service, EventEmitterMixin, LoggingMixin, ChainableMixin);
}

// Multiple inheritance simulation
function createAdvancedModel(data = {}) {
    const base = createModel(data);

    // Add caching capability
    const CachingMixin = {
        _cache: new Map(),

        cached(key, computeFn) {
            if (this._cache.has(key)) {
                return this._cache.get(key);
            }

            const value = computeFn();
            this._cache.set(key, value);
            return value;
        },

        clearCache(key) {
            if (key) {
                this._cache.delete(key);
            } else {
                this._cache.clear();
            }
            return this;
        }
    };

    // Add serialization capability
    const SerializationMixin = {
        serialize() {
            return JSON.stringify(this.toJSON());
        },

        deserialize(json) {
            const data = JSON.parse(json);
            Object.assign(this, data);
            this.touch();
            return this;
        },

        clone() {
            const cloned = createAdvancedModel();
            return cloned.deserialize(this.serialize());
        }
    };

    return Object.assign(base, CachingMixin, SerializationMixin);
}

// Functional composition
const pipe = (...functions) => (value) => functions.reduce((acc, fn) => fn(acc), value);

const addTimestamp = (obj) => ({ ...obj, timestamp: new Date() });
const addId = (obj) => ({ ...obj, id: Math.random().toString(36).substr(2, 9) });
const addVersion = (obj) => ({ ...obj, version: 1 });

const enhanceObject = pipe(addTimestamp, addId, addVersion);

function createEnhancedUser(name, email) {
    return enhanceObject({
        name,
        email,

        getProfile() {
            return {
                id: this.id,
                name: this.name,
                email: this.email,
                version: this.version,
                timestamp: this.timestamp
            };
        }
    });
}

// Using composition patterns
const human = createHuman('Alice');
const bird = createBird('Eagle');
const fish = createFish('Salmon');
const hero = createSuperhero('Superman');

human.walk().run().swim();
bird.walk().fly();
fish.swim();
hero.walk().run().swim().fly().usePower('laser vision').saveDay();

// Model with validation
const userModel = createModel({ name: 'John', email: 'john@example.com', age: 25 });

userModel.addValidator('email', (value) => {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) || 'Invalid email format';
});

userModel.addValidator('age', (value) => {
    return (value >= 0 && value <= 150) || 'Age must be between 0 and 150';
});

userModel.on('change', (data) => {
    console.log('User model changed:', data);
});

userModel.set('name', 'John Doe');
console.log(userModel.validate());

// Service with logging
const apiService = createService('API Service', { port: 3000, debug: true });

apiService.on('start', () => {
    console.log('API Service event: started');
});

apiService.start().configure({ port: 4000 }).log('Configuration updated');

// Advanced model with caching
const advancedModel = createAdvancedModel({ name: 'Test', data: [1, 2, 3, 4, 5] });

const expensiveComputation = advancedModel.cached('sum', () => {
    console.log('Computing sum...');
    return advancedModel.data.reduce((a, b) => a + b, 0);
});

console.log(expensiveComputation); // Computed
console.log(advancedModel.cached('sum', () => 'not called')); // From cache

// Enhanced user
const enhancedUser = createEnhancedUser('Jane', 'jane@example.com');
console.log(enhancedUser.getProfile());

// Clone advanced model
const cloned = advancedModel.clone();
console.log('Original:', advancedModel.toJSON());
console.log('Cloned:', cloned.toJSON());
""",
    )

    run_updater(javascript_object_patterns_project, mock_ingestor)

    project_name = javascript_object_patterns_project.name

    created_functions = get_node_names(mock_ingestor, "Function")

    expected_composition_functions = [
        f"{project_name}.object_composition.createAnimal",
        f"{project_name}.object_composition.createHuman",
        f"{project_name}.object_composition.createBird",
        f"{project_name}.object_composition.createModel",
        f"{project_name}.object_composition.createService",
        f"{project_name}.object_composition.createAdvancedModel",
        f"{project_name}.object_composition.createEnhancedUser",
    ]

    found_composition_functions = [
        func for func in expected_composition_functions if func in created_functions
    ]

    assert len(found_composition_functions) >= 5, (
        f"Expected at least 5 composition functions, found {len(found_composition_functions)}"
    )

    all_nodes = mock_ingestor.ensure_node_batch.call_args_list

    mixin_nodes = [
        call
        for call in all_nodes
        if "object_composition" in call[0][1].get("qualified_name", "")
        and any(
            pattern in call[0][1].get("qualified_name", "")
            for pattern in ["Mixin", "canWalk", "canRun", "canSwim", "canFly"]
        )
    ]

    assert len(mixin_nodes) >= 2, (
        f"Expected at least 2 mixin-related nodes, found {len(mixin_nodes)}"
    )


def test_object_patterns_comprehensive(
    javascript_object_patterns_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all object patterns are covered."""
    test_file = javascript_object_patterns_project / "comprehensive_objects.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every JavaScript object pattern in one file

// Object literal
const literal = {
    prop: 'value',
    method() {
        return this.prop;
    }
};

// Factory function
function createFactory(name) {
    return {
        name,
        getName() {
            return this.name;
        }
    };
}

// Constructor function
function Constructor(value) {
    this.value = value;
}

Constructor.prototype.getValue = function() {
    return this.value;
};

// Object composition
const mixin = {
    mixinMethod() {
        return 'mixed';
    }
};

function createComposed(data) {
    return Object.assign({ data }, mixin);
}

// Class (modern constructor)
class ModernClass {
    constructor(name) {
        this.name = name;
    }

    getName() {
        return this.name;
    }
}

// Using all patterns
const obj1 = literal;
const obj2 = createFactory('factory');
const obj3 = new Constructor('constructor');
const obj4 = createComposed('composed');
const obj5 = new ModernClass('modern');

console.log(obj1.method());
console.log(obj2.getName());
console.log(obj3.getValue());
console.log(obj4.mixinMethod());
console.log(obj5.getName());

// Advanced patterns
const advanced = {
    ...mixin,
    data: 'advanced',

    process() {
        return this.mixinMethod() + ' ' + this.data;
    }
};

console.log(advanced.process());
""",
    )

    run_updater(javascript_object_patterns_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    calls_relationships = get_relationships(mock_ingestor, "CALLS")
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    comprehensive_calls = [
        call
        for call in calls_relationships
        if "comprehensive_objects" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 3, (
        f"Expected at least 3 comprehensive object calls, found {len(comprehensive_calls)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    comprehensive_functions = [
        call
        for call in function_calls
        if "comprehensive_objects" in call[0][1]["qualified_name"]
    ]

    assert len(comprehensive_functions) >= 3, (
        f"Expected at least 3 functions in comprehensive test, found {len(comprehensive_functions)}"
    )
