"""Test class inheritance parsing for Python code."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def inheritance_project(tmp_path: Path) -> Path:
    """Create a temporary project with comprehensive inheritance patterns."""
    project_path = tmp_path / "inheritance_test"
    project_path.mkdir()

    # Create __init__.py
    (project_path / "__init__.py").write_text("")

    # Create inheritance.py with comprehensive inheritance examples
    inheritance_file = project_path / "inheritance.py"
    inheritance_file.write_text(
        '''"""Module with various inheritance patterns."""

from abc import ABC, abstractmethod
from typing import Protocol


# Base classes
class Animal:
    """Base animal class."""

    def __init__(self, name: str) -> None:
        self.name = name

    def speak(self) -> str:
        """Basic speak method to be overridden."""
        return "Some generic animal sound"

    def eat(self) -> str:
        """Common method."""
        return f"{self.name} is eating"


class Mammal(Animal):
    """Mammal inherits from Animal."""

    def __init__(self, name: str, fur_color: str) -> None:
        super().__init__(name)  # super() call!
        self.fur_color = fur_color

    def speak(self) -> str:
        """Override parent method."""
        return f"{self.name} makes mammal sounds"

    def give_birth(self) -> str:
        """Mammal-specific method."""
        return f"{self.name} gives birth"


class Flyable(Protocol):
    """Protocol for flying capability."""

    def fly(self) -> str:
        """Flying method."""
        ...


class Bird(Animal):
    """Bird inherits from Animal."""

    def __init__(self, name: str, wingspan: float) -> None:
        super().__init__(name)  # Another super() call
        self.wingspan = wingspan

    def speak(self) -> str:
        """Override parent method."""
        return f"{self.name} chirps"

    def fly(self) -> str:
        """Bird-specific method."""
        return f"{self.name} flies with {self.wingspan}m wingspan"


# Multiple inheritance
class Bat(Mammal, Flyable):
    """Bat inherits from both Mammal and Flyable (multiple inheritance)."""

    def __init__(self, name: str, fur_color: str, wing_membrane: str) -> None:
        super().__init__(name, fur_color)  # Complex super() call
        self.wing_membrane = wing_membrane

    def speak(self) -> str:
        """Override method from Mammal."""
        return f"{self.name} screeches"

    def fly(self) -> str:
        """Implement Flyable protocol."""
        return f"{self.name} flies using {self.wing_membrane} wings"

    def echolocate(self) -> str:
        """Bat-specific method."""
        return f"{self.name} uses echolocation"


# Deep inheritance chain
class Dog(Mammal):
    """Dog inherits from Mammal."""

    def speak(self) -> str:
        """Override method."""
        return f"{self.name} barks"

    def fetch(self) -> str:
        """Dog-specific method."""
        return f"{self.name} fetches the ball"


class Poodle(Dog):
    """Poodle inherits from Dog (3-level inheritance)."""

    def __init__(self, name: str, fur_color: str, cut_style: str) -> None:
        super().__init__(name, fur_color)  # Deep super() call
        self.cut_style = cut_style

    def speak(self) -> str:
        """Override method from Dog."""
        return f"{self.name} yips elegantly"

    def get_groomed(self) -> str:
        """Poodle-specific method."""
        return f"{self.name} gets a {self.cut_style} cut"


# Abstract base class
class Vehicle(ABC):
    """Abstract vehicle class."""

    def __init__(self, brand: str) -> None:
        self.brand = brand

    @abstractmethod
    def start_engine(self) -> str:
        """Abstract method must be implemented."""
        pass

    def honk(self) -> str:
        """Concrete method."""
        return f"{self.brand} vehicle honks"


class Car(Vehicle):
    """Car inherits from abstract Vehicle."""

    def start_engine(self) -> str:
        """Implement abstract method."""
        return f"{self.brand} car engine starts"

    def drive(self) -> str:
        """Car-specific method."""
        return f"Driving the {self.brand} car"


# Method chaining and super calls
class SmartCar(Car):
    """Smart car with advanced features."""

    def __init__(self, brand: str, ai_level: int) -> None:
        super().__init__(brand)  # Call parent constructor
        self.ai_level = ai_level

    def start_engine(self) -> str:
        """Override with super() call."""
        base_start = super().start_engine()  # Call parent method
        return f"{base_start} with AI level {self.ai_level}"

    def autonomous_drive(self) -> str:
        """Smart car specific method."""
        return f"{self.brand} drives itself (AI level {self.ai_level})"


def use_inheritance() -> None:
    """Function that demonstrates inheritance usage."""
    # Create instances and call methods
    dog = Dog("Buddy", "brown")
    print(dog.speak())  # Calls Dog.speak()
    print(dog.eat())    # Calls inherited Animal.eat()

    poodle = Poodle("Fifi", "white", "poodle cut")
    print(poodle.speak())      # Calls Poodle.speak() (override)
    print(poodle.give_birth()) # Calls inherited Mammal.give_birth()

    bat = Bat("Bruce", "black", "leather")
    print(bat.fly())     # Calls Bat.fly() (from Flyable)
    print(bat.speak())   # Calls Bat.speak() (override)

    smart_car = SmartCar("Tesla", 5)
    print(smart_car.start_engine())  # Calls SmartCar.start_engine() with super()
'''
    )

    return project_path


def test_inheritance_relationships_are_created(
    inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that INHERITS relationships are created between child and parent classes."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = inheritance_project.name

    # Expected INHERITS relationships
    expected_inherits = [
        # Single inheritance
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Mammal"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Bird"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Dog"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Mammal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Poodle"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Dog"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Car"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Vehicle"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.SmartCar"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Car"),
        ),
        # Multiple inheritance (Bat inherits from Mammal and Flyable)
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Bat"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Mammal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Bat"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Flyable"),
        ),
    ]

    # Verify INHERITS relationships are created
    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    for expected_child, expected_parent in expected_inherits:
        found = any(
            call[0][0] == expected_child and call[0][2] == expected_parent
            for call in relationship_calls
        )
        assert found, (
            f"Missing INHERITS relationship: "
            f"{expected_child[2]} INHERITS {expected_parent[2]}"
        )


def test_super_calls_are_tracked(
    inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that super() calls are tracked as CALLS relationships."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = inheritance_project.name

    # Expected super() calls to track
    expected_super_calls = [
        # Constructor super() calls
        (
            f"{project_name}.inheritance.Mammal.__init__",
            f"{project_name}.inheritance.Animal.__init__",
        ),
        (
            f"{project_name}.inheritance.Bird.__init__",
            f"{project_name}.inheritance.Animal.__init__",
        ),
        (
            f"{project_name}.inheritance.Bat.__init__",
            f"{project_name}.inheritance.Mammal.__init__",
        ),
        (
            f"{project_name}.inheritance.Poodle.__init__",
            f"{project_name}.inheritance.Dog.__init__",
        ),
        (
            f"{project_name}.inheritance.SmartCar.__init__",
            f"{project_name}.inheritance.Car.__init__",
        ),
        # Method super() calls
        (
            f"{project_name}.inheritance.SmartCar.start_engine",
            f"{project_name}.inheritance.Car.start_engine",
        ),
    ]

    # Get all CALLS relationships
    call_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "CALLS"
    ]

    for caller_qn, callee_qn in expected_super_calls:
        found = any(
            call[0][0][1] == caller_qn and call[0][2][1] == callee_qn
            for call in call_relationships
        )
        assert found, f"Missing super() call: {caller_qn} CALLS {callee_qn}"


def test_method_overrides_are_detected(
    inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that method overrides are properly detected and tracked."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = inheritance_project.name

    # Expected method overrides (child method overrides parent method)
    expected_overrides = [
        # speak() method overrides
        (
            f"{project_name}.inheritance.Mammal.speak",
            f"{project_name}.inheritance.Animal.speak",
        ),
        (
            f"{project_name}.inheritance.Bird.speak",
            f"{project_name}.inheritance.Animal.speak",
        ),
        (
            f"{project_name}.inheritance.Dog.speak",
            f"{project_name}.inheritance.Mammal.speak",
        ),
        (
            f"{project_name}.inheritance.Poodle.speak",
            f"{project_name}.inheritance.Dog.speak",
        ),
        (
            f"{project_name}.inheritance.Bat.speak",
            f"{project_name}.inheritance.Mammal.speak",
        ),
        # start_engine() overrides
        (
            f"{project_name}.inheritance.Car.start_engine",
            f"{project_name}.inheritance.Vehicle.start_engine",
        ),
        (
            f"{project_name}.inheritance.SmartCar.start_engine",
            f"{project_name}.inheritance.Car.start_engine",
        ),
    ]

    # Check for OVERRIDES relationships
    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    for child_method, parent_method in expected_overrides:
        found = any(
            call[0][0][1] == child_method and call[0][2][1] == parent_method
            for call in override_relationships
        )
        assert found, (
            f"Missing OVERRIDES relationship: {child_method} OVERRIDES {parent_method}"
        )


def test_multiple_inheritance_is_handled(
    inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that multiple inheritance is properly handled."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = inheritance_project.name

    # Bat should inherit from both Mammal and Flyable
    expected_multiple_inheritance = [
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Bat"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Mammal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Bat"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Flyable"),
        ),
    ]

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    for expected_child, expected_parent in expected_multiple_inheritance:
        found = any(
            call[0][0] == expected_child and call[0][2] == expected_parent
            for call in relationship_calls
        )
        assert found, (
            f"Missing multiple inheritance: "
            f"{expected_child[1]} INHERITS {expected_parent[1]}"
        )


def test_inherited_method_calls_are_resolved(
    inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that calls to inherited methods are properly resolved."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = inheritance_project.name

    # Expected calls to inherited methods
    expected_inherited_calls = [
        # dog.eat() calls Animal.eat() (inherited through Mammal)
        (
            f"{project_name}.inheritance.use_inheritance",
            f"{project_name}.inheritance.Animal.eat",
        ),
        # poodle.give_birth() calls Mammal.give_birth() (inherited)
        (
            f"{project_name}.inheritance.use_inheritance",
            f"{project_name}.inheritance.Mammal.give_birth",
        ),
    ]

    call_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "CALLS"
    ]

    for caller_qn, callee_qn in expected_inherited_calls:
        found = any(
            call[0][0][1] == caller_qn and call[0][2][1] == callee_qn
            for call in call_relationships
        )
        assert found, f"Missing inherited method call: {caller_qn} CALLS {callee_qn}"


def test_deep_inheritance_chain(
    inheritance_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that deep inheritance chains are properly handled."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = inheritance_project.name

    # Test 3-level inheritance: Animal -> Mammal -> Dog -> Poodle
    expected_chain = [
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Mammal"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Dog"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Mammal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.inheritance.Poodle"),
            ("Class", "qualified_name", f"{project_name}.inheritance.Dog"),
        ),
    ]

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    for expected_child, expected_parent in expected_chain:
        found = any(
            call[0][0] == expected_child and call[0][2] == expected_parent
            for call in relationship_calls
        )
        assert found, (
            f"Missing inheritance in chain: "
            f"{expected_child[1]} INHERITS {expected_parent[1]}"
        )
