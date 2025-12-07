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

    (project_path / "__init__.py").write_text("")

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

    expected_inherits = [
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

    expected_super_calls = [
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
            f"{project_name}.inheritance.Mammal.__init__",
        ),
        (
            f"{project_name}.inheritance.SmartCar.__init__",
            f"{project_name}.inheritance.Vehicle.__init__",
        ),
        (
            f"{project_name}.inheritance.SmartCar.start_engine",
            f"{project_name}.inheritance.Car.start_engine",
        ),
    ]

    call_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "CALLS"
    ]

    for caller_qn, callee_qn in expected_super_calls:
        found = any(
            call[0][0][2] == caller_qn and call[0][2][2] == callee_qn
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

    expected_overrides = [
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
        (
            f"{project_name}.inheritance.Car.start_engine",
            f"{project_name}.inheritance.Vehicle.start_engine",
        ),
        (
            f"{project_name}.inheritance.SmartCar.start_engine",
            f"{project_name}.inheritance.Car.start_engine",
        ),
    ]

    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    for child_method, parent_method in expected_overrides:
        found = any(
            call[0][0][2] == child_method and call[0][2][2] == parent_method
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

    expected_inherited_calls = [
        (
            f"{project_name}.inheritance.use_inheritance",
            f"{project_name}.inheritance.Animal.eat",
        ),
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
            call[0][0][2] == caller_qn and call[0][2][2] == callee_qn
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


@pytest.fixture
def mro_diamond_project(tmp_path: Path) -> Path:
    """Create a project with diamond inheritance patterns for MRO testing."""
    project_path = tmp_path / "mro_test"
    project_path.mkdir()

    (project_path / "__init__.py").write_text("")

    diamond_file = project_path / "diamond_mro.py"
    diamond_file.write_text(
        '''"""Diamond inheritance patterns for MRO testing."""

# Classic Diamond Inheritance Pattern
class A:
    """Base class."""

    def method(self) -> str:
        """Base method to be overridden."""
        return "A.method"

    def base_only(self) -> str:
        """Method only in base class."""
        return "A.base_only"

    def deep_method(self) -> str:
        """Base deep method for testing deep chains."""
        return "A.deep_method"

    def asym_method(self) -> str:
        """Base asymmetric method."""
        return "A.asym_method"

class B(A):
    """Left branch of diamond."""

    def method(self) -> str:
        """Override in left branch."""
        return "B.method"

    def left_only(self) -> str:
        """Method only in left branch."""
        return "B.left_only"

class C(A):
    """Right branch of diamond."""

    def method(self) -> str:
        """Override in right branch."""
        return "C.method"

    def right_only(self) -> str:
        """Method only in right branch."""
        return "C.right_only"

class D(B, C):
    """Diamond point - inherits from both B and C."""

    def diamond_method(self) -> str:
        """Method unique to diamond point."""
        return "D.diamond_method"

# Complex Diamond with Override at Bottom
class E(B, C):
    """Another diamond point that overrides the conflicted method."""

    def method(self) -> str:
        """Override the conflicted method at diamond point."""
        return "E.method"

# Multiple Diamonds Pattern
class F:
    """Another base class."""

    def f_method(self) -> str:
        """Method from F."""
        return "F.f_method"

    def shared_method(self) -> str:
        """Method that will be overridden."""
        return "F.shared_method"

class G(A, F):
    """Mix of two hierarchies."""

    def shared_method(self) -> str:
        """Override shared method."""
        return "G.shared_method"

class H(B, F):
    """Another mix."""

    def shared_method(self) -> str:
        """Override shared method."""
        return "H.shared_method"

class I(G, H):
    """Complex multiple inheritance from mixed hierarchies."""

    def complex_method(self) -> str:
        """Method unique to complex class."""
        return "I.complex_method"

# Deep Diamond Chain
class J(A):
    """Intermediate class in deep chain."""

    def deep_method(self) -> str:
        """Method for deep testing."""
        return "J.deep_method"

class K(J):
    """Another intermediate class."""

    def deep_method(self) -> str:
        """Override deep method."""
        return "K.deep_method"

class L(A):
    """Parallel branch."""

    def deep_method(self) -> str:
        """Override deep method in parallel branch."""
        return "L.deep_method"

class M(K, L):
    """Deep diamond point."""

    def final_method(self) -> str:
        """Final method."""
        return "M.final_method"

# Asymmetric Diamond (different depth branches)
class N(A):
    """Shallow branch."""

    def asym_method(self) -> str:
        """Asymmetric method."""
        return "N.asym_method"

class O(B):
    """Deeper branch (A -> B -> O)."""

    def asym_method(self) -> str:
        """Override in deeper branch."""
        return "O.asym_method"

class P(N, O):
    """Asymmetric diamond - left is 1 level deep, right is 2 levels deep."""

    def asym_final(self) -> str:
        """Final asymmetric method."""
        return "P.asym_final"
'''
    )

    return project_path


def test_diamond_inheritance_mro_basic(
    mro_diamond_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test basic diamond inheritance MRO - should follow left-to-right, depth-first."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=mro_diamond_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = mro_diamond_project.name

    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    d_method_overrides = [
        call
        for call in override_relationships
        if call[0][0][2] == f"{project_name}.diamond_mro.D.method"
    ]

    assert len(d_method_overrides) == 0, (
        f"D class should not have method() overrides, but found: {d_method_overrides}"
    )

    expected_overrides = [
        (
            f"{project_name}.diamond_mro.B.method",
            f"{project_name}.diamond_mro.A.method",
        ),
        (
            f"{project_name}.diamond_mro.C.method",
            f"{project_name}.diamond_mro.A.method",
        ),
        (
            f"{project_name}.diamond_mro.E.method",
            f"{project_name}.diamond_mro.B.method",
        ),
    ]

    for child_method, parent_method in expected_overrides:
        found = any(
            call[0][0][2] == child_method and call[0][2][2] == parent_method
            for call in override_relationships
        )
        assert found, (
            f"Missing OVERRIDES relationship: {child_method} OVERRIDES {parent_method}"
        )


def test_diamond_inheritance_mro_override_at_point(
    mro_diamond_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test diamond where the diamond point overrides the conflicted method."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=mro_diamond_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = mro_diamond_project.name

    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    e_override_found = any(
        (
            call[0][0][2] == f"{project_name}.diamond_mro.E.method"
            and call[0][2][2] == f"{project_name}.diamond_mro.B.method"
        )
        for call in override_relationships
    )

    assert e_override_found, (
        "E.method should override B.method (nearest parent in MRO), "
        f"but override relationships are: {[(call[0][0][2], call[0][2][2]) for call in override_relationships if 'E.method' in call[0][0][2]]}"
    )


def test_complex_multiple_inheritance_mro(
    mro_diamond_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test complex multiple inheritance patterns with mixed hierarchies."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=mro_diamond_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = mro_diamond_project.name

    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    i_shared_overrides = [
        call
        for call in override_relationships
        if call[0][0][2] == f"{project_name}.diamond_mro.I.shared_method"
    ]

    assert len(i_shared_overrides) == 0, (
        f"I class should not have shared_method overrides, but found: {i_shared_overrides}"
    )

    expected_complex_overrides = [
        (
            f"{project_name}.diamond_mro.G.shared_method",
            f"{project_name}.diamond_mro.F.shared_method",
        ),
        (
            f"{project_name}.diamond_mro.H.shared_method",
            f"{project_name}.diamond_mro.F.shared_method",
        ),
    ]

    for child_method, parent_method in expected_complex_overrides:
        found = any(
            call[0][0][2] == child_method and call[0][2][2] == parent_method
            for call in override_relationships
        )
        assert found, (
            f"Missing complex MRO override: {child_method} OVERRIDES {parent_method}"
        )


def test_deep_diamond_chain_mro(
    mro_diamond_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test deep diamond chains with multiple levels."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=mro_diamond_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = mro_diamond_project.name

    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    expected_deep_overrides = [
        (
            f"{project_name}.diamond_mro.J.deep_method",
            f"{project_name}.diamond_mro.A.deep_method",
        ),
        (
            f"{project_name}.diamond_mro.K.deep_method",
            f"{project_name}.diamond_mro.J.deep_method",
        ),
        (
            f"{project_name}.diamond_mro.L.deep_method",
            f"{project_name}.diamond_mro.A.deep_method",
        ),
    ]

    for child_method, parent_method in expected_deep_overrides:
        found = any(
            call[0][0][2] == child_method and call[0][2][2] == parent_method
            for call in override_relationships
        )
        assert found, (
            f"Missing deep chain override: {child_method} OVERRIDES {parent_method}"
        )

    m_deep_overrides = [
        call
        for call in override_relationships
        if call[0][0][2] == f"{project_name}.diamond_mro.M.deep_method"
    ]

    assert len(m_deep_overrides) == 0, (
        f"M class should not have deep_method overrides, but found: {m_deep_overrides}"
    )


def test_asymmetric_diamond_mro(
    mro_diamond_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test asymmetric diamond inheritance with different branch depths."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=mro_diamond_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = mro_diamond_project.name

    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    expected_asym_overrides = [
        (
            f"{project_name}.diamond_mro.N.asym_method",
            f"{project_name}.diamond_mro.A.asym_method",
        ),
        (
            f"{project_name}.diamond_mro.O.asym_method",
            f"{project_name}.diamond_mro.A.asym_method",
        ),
    ]

    for child_method, parent_method in expected_asym_overrides:
        found = any(
            call[0][0][2] == child_method and call[0][2][2] == parent_method
            for call in override_relationships
        )
        assert found, (
            f"Missing asymmetric override: {child_method} OVERRIDES {parent_method}"
        )

    p_asym_overrides = [
        call
        for call in override_relationships
        if call[0][0][2] == f"{project_name}.diamond_mro.P.asym_method"
    ]

    assert len(p_asym_overrides) == 0, (
        f"P class should not have asym_method overrides, but found: {p_asym_overrides}"
    )


def test_mro_nearest_override_selection(
    mro_diamond_project: Path, mock_ingestor: MagicMock
) -> None:
    """Test that BFS correctly selects the nearest override, not just any override."""
    parsers, queries = load_parsers()

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=mro_diamond_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = mro_diamond_project.name

    override_relationships = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "OVERRIDES"
    ]

    e_method_overrides = [
        (call[0][0][2], call[0][2][2])
        for call in override_relationships
        if call[0][0][2] == f"{project_name}.diamond_mro.E.method"
    ]

    assert len(e_method_overrides) == 1, (
        f"E.method should have exactly one override, but found: {e_method_overrides}"
    )

    _, parent_method = e_method_overrides[0]
    assert parent_method == f"{project_name}.diamond_mro.B.method", (
        f"E.method should override B.method (nearest parent), but overrides {parent_method}"
    )

    e_to_a_override = any(
        (
            call[0][0][2] == f"{project_name}.diamond_mro.E.method"
            and call[0][2][2] == f"{project_name}.diamond_mro.A.method"
        )
        for call in override_relationships
    )

    assert not e_to_a_override, (
        "E.method should not directly override A.method since B.method is closer"
    )
