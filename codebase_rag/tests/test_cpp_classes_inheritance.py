"""
Comprehensive C++ class inheritance and polymorphism testing.
Tests complex inheritance hierarchies, virtual functions, multiple inheritance, and polymorphic relationships.
"""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def cpp_inheritance_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with inheritance patterns."""
    project_path = temp_repo / "cpp_inheritance_test"
    project_path.mkdir()

    # Create basic structure
    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    # Create base files
    (project_path / "include" / "shapes.h").write_text("#pragma once\nclass Shape {};")

    return project_path


def test_single_inheritance(
    cpp_inheritance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test single inheritance patterns and virtual functions."""
    test_file = cpp_inheritance_project / "single_inheritance.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <string>
#include <memory>

// Base class with virtual functions
class Animal {
protected:
    std::string name_;
    int age_;

public:
    Animal(const std::string& name, int age) : name_(name), age_(age) {}
    virtual ~Animal() = default;  // Virtual destructor

    // Pure virtual function (abstract)
    virtual void speak() const = 0;

    // Virtual functions with default implementation
    virtual void move() const {
        std::cout << name_ << " moves" << std::endl;
    }

    virtual void eat() const {
        std::cout << name_ << " eats" << std::endl;
    }

    // Non-virtual functions
    std::string getName() const { return name_; }
    int getAge() const { return age_; }
    void setAge(int age) { age_ = age; }

    // Virtual function with different access levels
    virtual void sleep() const {
        std::cout << name_ << " sleeps" << std::endl;
    }
};

// Single inheritance - Dog inherits from Animal
class Dog : public Animal {
private:
    std::string breed_;

public:
    Dog(const std::string& name, int age, const std::string& breed)
        : Animal(name, age), breed_(breed) {}

    // Override pure virtual function
    void speak() const override {
        std::cout << name_ << " barks: Woof!" << std::endl;
    }

    // Override virtual function
    void move() const override {
        std::cout << name_ << " runs on four legs" << std::endl;
    }

    // New methods specific to Dog
    void fetch() const {
        std::cout << name_ << " fetches the ball" << std::endl;
    }

    void wagTail() const {
        std::cout << name_ << " wags tail happily" << std::endl;
    }

    std::string getBreed() const { return breed_; }
};

// Another single inheritance - Cat inherits from Animal
class Cat : public Animal {
private:
    bool isIndoor_;

public:
    Cat(const std::string& name, int age, bool indoor)
        : Animal(name, age), isIndoor_(indoor) {}

    // Override pure virtual function
    void speak() const override {
        std::cout << name_ << " meows: Meow!" << std::endl;
    }

    // Override virtual function with different behavior
    void move() const override {
        std::cout << name_ << " stalks silently" << std::endl;
    }

    // Override virtual function
    void sleep() const override {
        std::cout << name_ << " sleeps 16 hours a day" << std::endl;
    }

    // Cat-specific methods
    void climb() const {
        std::cout << name_ << " climbs the tree" << std::endl;
    }

    void purr() const {
        std::cout << name_ << " purrs contentedly" << std::endl;
    }

    bool isIndoor() const { return isIndoor_; }
};

// Inheritance chain - Puppy inherits from Dog
class Puppy : public Dog {
private:
    bool isHouseTrained_;

public:
    Puppy(const std::string& name, const std::string& breed, bool houseTrained)
        : Dog(name, 0, breed), isHouseTrained_(houseTrained) {}  // Puppies start at age 0

    // Override speak with puppy-specific behavior
    void speak() const override {
        std::cout << name_ << " yips excitedly: Yip yip!" << std::endl;
    }

    // Override move with puppy behavior
    void move() const override {
        std::cout << name_ << " bounces around playfully" << std::endl;
    }

    // Puppy-specific methods
    void playWithToy() const {
        std::cout << name_ << " plays with squeaky toy" << std::endl;
    }

    void learnTrick(const std::string& trick) const {
        std::cout << name_ << " is learning to " << trick << std::endl;
    }

    bool isHouseTrained() const { return isHouseTrained_; }
    void setHouseTrained(bool trained) { isHouseTrained_ = trained; }
};

// Function demonstrating polymorphism
void demonstratePolymorphism() {
    // Create objects
    Dog dog("Buddy", 5, "Golden Retriever");
    Cat cat("Whiskers", 3, true);
    Puppy puppy("Max", "Labrador", false);

    // Direct method calls
    dog.speak();
    dog.fetch();

    cat.speak();
    cat.climb();

    puppy.speak();  // Calls Puppy's override
    puppy.playWithToy();
    puppy.learnTrick("sit");

    // Polymorphic calls through base class pointers
    std::unique_ptr<Animal> animals[] = {
        std::make_unique<Dog>("Rex", 4, "German Shepherd"),
        std::make_unique<Cat>("Luna", 2, false),
        std::make_unique<Puppy>("Bella", "Beagle", true)
    };

    for (const auto& animal : animals) {
        animal->speak();  // Virtual function call
        animal->move();   // Virtual function call
        animal->eat();    // Virtual function call
        animal->sleep();  // Virtual function call

        // Type-specific operations using dynamic_cast
        if (auto* dog_ptr = dynamic_cast<Dog*>(animal.get())) {
            dog_ptr->fetch();
        }

        if (auto* cat_ptr = dynamic_cast<Cat*>(animal.get())) {
            cat_ptr->purr();
        }

        if (auto* puppy_ptr = dynamic_cast<Puppy*>(animal.get())) {
            puppy_ptr->playWithToy();
        }
    }
}

// Base class calling virtual functions
class AnimalTrainer {
public:
    static void trainAnimal(Animal* animal) {
        std::cout << "Training " << animal->getName() << std::endl;

        // These calls will use the most derived implementation
        animal->speak();  // Virtual dispatch
        animal->move();   // Virtual dispatch

        // Non-virtual call
        animal->setAge(animal->getAge() + 1);
    }

    static void feedAnimals(const std::vector<std::unique_ptr<Animal>>& animals) {
        for (const auto& animal : animals) {
            animal->eat();  // Virtual function call
        }
    }
};

void demonstrateSingleInheritance() {
    // Create various animals
    auto dog = std::make_unique<Dog>("Charlie", 6, "Border Collie");
    auto cat = std::make_unique<Cat>("Shadow", 4, true);
    auto puppy = std::make_unique<Puppy>("Daisy", "Poodle", true);

    // Train each animal (demonstrates virtual function calls)
    AnimalTrainer::trainAnimal(dog.get());
    AnimalTrainer::trainAnimal(cat.get());
    AnimalTrainer::trainAnimal(puppy.get());

    // Create a collection of animals
    std::vector<std::unique_ptr<Animal>> animals;
    animals.push_back(std::move(dog));
    animals.push_back(std::move(cat));
    animals.push_back(std::move(puppy));

    // Feed all animals (polymorphic behavior)
    AnimalTrainer::feedAnimals(animals);

    // Individual behavior
    Dog specificDog("Rocky", 7, "Bulldog");
    specificDog.wagTail();  // Dog-specific method

    Cat specificCat("Mittens", 5, false);
    specificCat.purr();  // Cat-specific method

    // Method chaining and inheritance
    Puppy specificPuppy("Lucy", "Shih Tzu", false);
    specificPuppy.setHouseTrained(true);
    specificPuppy.speak();  // Uses Puppy's override
    specificPuppy.fetch();  // Inherited from Dog
    specificPuppy.eat();    // Inherited from Animal
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_inheritance_project.name

    # Expected inheritance relationships
    expected_inherits = [
        (
            ("Class", "qualified_name", f"{project_name}.single_inheritance.Dog"),
            ("Class", "qualified_name", f"{project_name}.single_inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.single_inheritance.Cat"),
            ("Class", "qualified_name", f"{project_name}.single_inheritance.Animal"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.single_inheritance.Puppy"),
            ("Class", "qualified_name", f"{project_name}.single_inheritance.Dog"),
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

    # Verify virtual function calls are tracked
    call_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    virtual_calls = [
        call
        for call in call_relationships
        if "single_inheritance" in call.args[0][2]
        and any(
            method in call.args[2][2]
            for method in ["speak", "move", "eat", "sleep", "fetch", "purr"]
        )
    ]

    assert len(virtual_calls) >= 10, (
        f"Expected at least 10 virtual function calls, found {len(virtual_calls)}"
    )


def test_multiple_inheritance(
    cpp_inheritance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test multiple inheritance patterns and virtual base classes."""
    test_file = cpp_inheritance_project / "multiple_inheritance.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <string>

// Multiple base classes
class Flyable {
public:
    virtual ~Flyable() = default;
    virtual void fly() const = 0;
    virtual double getMaxAltitude() const = 0;

    void takeOff() const {
        std::cout << "Taking off..." << std::endl;
    }

    void land() const {
        std::cout << "Landing..." << std::endl;
    }
};

class Swimmable {
public:
    virtual ~Swimmable() = default;
    virtual void swim() const = 0;
    virtual double getMaxDepth() const = 0;

    void dive() const {
        std::cout << "Diving..." << std::endl;
    }

    void surface() const {
        std::cout << "Surfacing..." << std::endl;
    }
};

class Walkable {
public:
    virtual ~Walkable() = default;
    virtual void walk() const = 0;
    virtual double getMaxSpeed() const = 0;

    void run() const {
        std::cout << "Running..." << std::endl;
    }

    void rest() const {
        std::cout << "Resting..." << std::endl;
    }
};

// Base class for the diamond problem demonstration
class LivingBeing {
protected:
    std::string name_;
    double energy_;

public:
    LivingBeing(const std::string& name) : name_(name), energy_(100.0) {}
    virtual ~LivingBeing() = default;

    std::string getName() const { return name_; }
    double getEnergy() const { return energy_; }
    void consumeEnergy(double amount) { energy_ -= amount; }

    virtual void breathe() const {
        std::cout << name_ << " is breathing" << std::endl;
    }
};

// Virtual inheritance to solve diamond problem
class Bird : public virtual LivingBeing, public Flyable, public Walkable {
protected:
    double wingSpan_;

public:
    Bird(const std::string& name, double wingSpan)
        : LivingBeing(name), wingSpan_(wingSpan) {}

    void fly() const override {
        consumeEnergy(10.0);
        std::cout << name_ << " flies with wingspan " << wingSpan_ << "m" << std::endl;
    }

    void walk() const override {
        consumeEnergy(2.0);
        std::cout << name_ << " walks on the ground" << std::endl;
    }

    double getMaxAltitude() const override {
        return 1000.0 * wingSpan_;  // Larger wings, higher altitude
    }

    double getMaxSpeed() const override {
        return 10.0 + wingSpan_ * 5.0;  // Wing span affects walking speed too
    }

    double getWingSpan() const { return wingSpan_; }
};

class Fish : public virtual LivingBeing, public Swimmable {
protected:
    double finSize_;

public:
    Fish(const std::string& name, double finSize)
        : LivingBeing(name), finSize_(finSize) {}

    void swim() const override {
        consumeEnergy(5.0);
        std::cout << name_ << " swims with fin size " << finSize_ << "cm" << std::endl;
    }

    double getMaxDepth() const override {
        return 100.0 * finSize_;  // Larger fins, deeper diving
    }

    void breathe() const override {
        std::cout << name_ << " breathes through gills" << std::endl;
    }

    double getFinSize() const { return finSize_; }
};

// Multiple inheritance with virtual base - Diamond problem solution
class Duck : public Bird, public Swimmable {
private:
    bool isWaterproof_;

public:
    Duck(const std::string& name, double wingSpan, bool waterproof)
        : LivingBeing(name), Bird(name, wingSpan), isWaterproof_(waterproof) {}

    // Implement Swimmable interface
    void swim() const override {
        consumeEnergy(3.0);
        std::cout << name_ << " swims on water surface" << std::endl;
    }

    double getMaxDepth() const override {
        return isWaterproof_ ? 5.0 : 1.0;  // Limited diving ability
    }

    // Override Bird's fly method
    void fly() const override {
        std::cout << name_ << " flies low over water" << std::endl;
        Bird::fly();  // Call parent implementation
    }

    void quack() const {
        std::cout << name_ << " quacks loudly" << std::endl;
    }

    bool isWaterproof() const { return isWaterproof_; }
};

// Complex multiple inheritance
class Penguin : public Bird, public Swimmable {
public:
    Penguin(const std::string& name)
        : LivingBeing(name), Bird(name, 0.5) {}  // Small wings

    // Override Bird's fly (penguins can't fly)
    void fly() const override {
        std::cout << name_ << " cannot fly, but flaps wings for balance" << std::endl;
    }

    double getMaxAltitude() const override {
        return 0.0;  // Cannot achieve altitude
    }

    // Implement swimming
    void swim() const override {
        consumeEnergy(4.0);
        std::cout << name_ << " swims underwater like a torpedo" << std::endl;
    }

    double getMaxDepth() const override {
        return 500.0;  // Excellent divers
    }

    // Override walking for penguin waddle
    void walk() const override {
        std::cout << name_ << " waddles awkwardly on land" << std::endl;
    }

    void slideOnBelly() const {
        std::cout << name_ << " slides on belly across ice" << std::endl;
    }
};

// Mixin-style multiple inheritance
class Domesticated {
public:
    virtual ~Domesticated() = default;

    virtual void respondToName() const = 0;
    virtual void followCommands() const = 0;

    void showAffection() const {
        std::cout << "Shows affection to humans" << std::endl;
    }
};

class Pet : public Duck, public Domesticated {
private:
    std::string owner_;

public:
    Pet(const std::string& name, const std::string& owner)
        : LivingBeing(name), Bird(name, 0.3), Duck(name, 0.3, true), owner_(owner) {}

    void respondToName() const override {
        std::cout << name_ << " responds to " << owner_ << "'s call" << std::endl;
    }

    void followCommands() const override {
        std::cout << name_ << " follows " << owner_ << "'s commands" << std::endl;
    }

    std::string getOwner() const { return owner_; }
};

void demonstrateMultipleInheritance() {
    // Test basic multiple inheritance
    Duck mallard("Mallard", 0.8, true);
    mallard.fly();           // From Bird
    mallard.swim();          // From Swimmable
    mallard.walk();          // From Bird -> Walkable
    mallard.quack();         // Duck-specific
    mallard.takeOff();       // From Flyable
    mallard.dive();          // From Swimmable

    std::cout << "Energy: " << mallard.getEnergy() << std::endl;

    // Test penguin (flightless bird that swims)
    Penguin emperor("Emperor");
    emperor.fly();           // Overridden - cannot fly
    emperor.swim();          // Excellent swimmer
    emperor.walk();          // Waddles
    emperor.slideOnBelly();  // Penguin-specific
    emperor.breathe();       // From LivingBeing

    // Test pet (multiple inheritance chain)
    Pet ducky("Ducky", "Alice");
    ducky.respondToName();   // From Domesticated
    ducky.followCommands();  // From Domesticated
    ducky.showAffection();   // From Domesticated
    ducky.fly();             // From Duck -> Bird
    ducky.swim();            // From Duck
    ducky.quack();           // From Duck

    // Polymorphic behavior with multiple interfaces
    Flyable* flyers[] = {&mallard, &emperor};
    for (Flyable* flyer : flyers) {
        flyer->fly();
        flyer->takeOff();
        std::cout << "Max altitude: " << flyer->getMaxAltitude() << "m" << std::endl;
    }

    Swimmable* swimmers[] = {&mallard, &emperor, &ducky};
    for (Swimmable* swimmer : swimmers) {
        swimmer->swim();
        swimmer->dive();
        std::cout << "Max depth: " << swimmer->getMaxDepth() << "m" << std::endl;
    }

    // Test virtual base class - should be only one LivingBeing instance
    LivingBeing* beings[] = {&mallard, &emperor, &ducky};
    for (LivingBeing* being : beings) {
        being->breathe();
        std::cout << being->getName() << " has energy: " << being->getEnergy() << std::endl;
    }
}

// Function to test interface segregation
void testAnimalAbilities() {
    Duck versatileDuck("Versatile", 0.7, true);

    // Test each interface separately
    Flyable* flyable = &versatileDuck;
    Swimmable* swimmable = &versatileDuck;
    Walkable* walkable = &versatileDuck;
    LivingBeing* being = &versatileDuck;

    flyable->fly();
    swimmable->swim();
    walkable->walk();
    being->breathe();

    // Demonstrate that the same object can be accessed through multiple interfaces
    std::cout << "Same object through different interfaces:" << std::endl;
    std::cout << "  As Flyable: max altitude = " << flyable->getMaxAltitude() << std::endl;
    std::cout << "  As Swimmable: max depth = " << swimmable->getMaxDepth() << std::endl;
    std::cout << "  As Walkable: max speed = " << walkable->getMaxSpeed() << std::endl;
    std::cout << "  As LivingBeing: energy = " << being->getEnergy() << std::endl;
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_inheritance_project.name

    # Expected multiple inheritance relationships
    expected_multiple_inherits = [
        # Bird inherits from LivingBeing, Flyable, Walkable
        (
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Bird"),
            (
                "Class",
                "qualified_name",
                f"{project_name}.multiple_inheritance.LivingBeing",
            ),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Bird"),
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Flyable"),
        ),
        # Duck inherits from Bird and Swimmable
        (
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Duck"),
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Bird"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Duck"),
            (
                "Class",
                "qualified_name",
                f"{project_name}.multiple_inheritance.Swimmable",
            ),
        ),
        # Pet inherits from Duck and Domesticated
        (
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Pet"),
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Duck"),
        ),
    ]

    # Verify INHERITS relationships are created
    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    found_relationships = 0
    for expected_child, expected_parent in expected_multiple_inherits:
        found = any(
            call[0][0] == expected_child and call[0][2] == expected_parent
            for call in relationship_calls
        )
        if found:
            found_relationships += 1

    assert found_relationships >= 4, (
        f"Expected at least 4 multiple inheritance relationships, found {found_relationships}"
    )


def test_abstract_classes_and_interfaces(
    cpp_inheritance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test abstract classes and interface-like patterns."""
    test_file = cpp_inheritance_project / "abstract_interfaces.cpp"
    test_file.write_text(
        """
#include <iostream>
#include <vector>
#include <memory>
#include <string>

// Pure abstract base class (interface-like)
class IDrawable {
public:
    virtual ~IDrawable() = default;
    virtual void draw() const = 0;
    virtual void setColor(const std::string& color) = 0;
    virtual std::string getColor() const = 0;
};

class IMovable {
public:
    virtual ~IMovable() = default;
    virtual void move(double x, double y) = 0;
    virtual std::pair<double, double> getPosition() const = 0;
};

class IResizable {
public:
    virtual ~IResizable() = default;
    virtual void resize(double factor) = 0;
    virtual double getSize() const = 0;
};

// Abstract base class with some implementation
class Shape : public IDrawable, public IMovable {
protected:
    double x_, y_;
    std::string color_;

public:
    Shape(double x, double y, const std::string& color)
        : x_(x), y_(y), color_(color) {}

    virtual ~Shape() = default;

    // Implemented interface methods
    void move(double x, double y) override {
        x_ = x;
        y_ = y;
    }

    std::pair<double, double> getPosition() const override {
        return {x_, y_};
    }

    void setColor(const std::string& color) override {
        color_ = color;
    }

    std::string getColor() const override {
        return color_;
    }

    // Pure virtual methods - must be implemented by derived classes
    virtual double area() const = 0;
    virtual double perimeter() const = 0;

    // Virtual method with default implementation
    virtual void describe() const {
        std::cout << "Shape at (" << x_ << ", " << y_ << ") with color " << color_ << std::endl;
    }
};

// Concrete implementation of abstract class
class Circle : public Shape, public IResizable {
private:
    double radius_;

public:
    Circle(double x, double y, double radius, const std::string& color)
        : Shape(x, y, color), radius_(radius) {}

    // Implement pure virtual methods from Shape
    double area() const override {
        return 3.14159 * radius_ * radius_;
    }

    double perimeter() const override {
        return 2 * 3.14159 * radius_;
    }

    // Implement IDrawable
    void draw() const override {
        std::cout << "Drawing circle with radius " << radius_
                  << " at (" << x_ << ", " << y_ << ") in " << color_ << std::endl;
    }

    // Implement IResizable
    void resize(double factor) override {
        radius_ *= factor;
    }

    double getSize() const override {
        return radius_;
    }

    // Override virtual method
    void describe() const override {
        std::cout << "Circle with radius " << radius_;
        Shape::describe();  // Call base implementation
    }

    double getRadius() const { return radius_; }
};

class Rectangle : public Shape, public IResizable {
private:
    double width_, height_;

public:
    Rectangle(double x, double y, double width, double height, const std::string& color)
        : Shape(x, y, color), width_(width), height_(height) {}

    // Implement pure virtual methods from Shape
    double area() const override {
        return width_ * height_;
    }

    double perimeter() const override {
        return 2 * (width_ + height_);
    }

    // Implement IDrawable
    void draw() const override {
        std::cout << "Drawing rectangle " << width_ << "x" << height_
                  << " at (" << x_ << ", " << y_ << ") in " << color_ << std::endl;
    }

    // Implement IResizable
    void resize(double factor) override {
        width_ *= factor;
        height_ *= factor;
    }

    double getSize() const override {
        return width_ * height_;  // Use area as size
    }

    // Override virtual method
    void describe() const override {
        std::cout << "Rectangle " << width_ << "x" << height_;
        Shape::describe();  // Call base implementation
    }

    double getWidth() const { return width_; }
    double getHeight() const { return height_; }
};

// Another concrete class implementing multiple interfaces
class Triangle : public Shape {
private:
    double base_, height_;

public:
    Triangle(double x, double y, double base, double height, const std::string& color)
        : Shape(x, y, color), base_(base), height_(height) {}

    // Implement pure virtual methods from Shape
    double area() const override {
        return 0.5 * base_ * height_;
    }

    double perimeter() const override {
        // Simplified - assuming right triangle
        double hypotenuse = sqrt(base_ * base_ + height_ * height_);
        return base_ + height_ + hypotenuse;
    }

    // Implement IDrawable
    void draw() const override {
        std::cout << "Drawing triangle with base " << base_ << " and height " << height_
                  << " at (" << x_ << ", " << y_ << ") in " << color_ << std::endl;
    }

    double getBase() const { return base_; }
    double getHeight() const { return height_; }
};

// Template class that works with any IDrawable
template<typename T>
class Canvas {
private:
    std::vector<std::unique_ptr<T>> shapes_;

public:
    void addShape(std::unique_ptr<T> shape) {
        shapes_.push_back(std::move(shape));
    }

    void drawAll() const {
        for (const auto& shape : shapes_) {
            shape->draw();
        }
    }

    void moveAll(double deltaX, double deltaY) {
        for (auto& shape : shapes_) {
            auto pos = shape->getPosition();
            shape->move(pos.first + deltaX, pos.second + deltaY);
        }
    }

    size_t getShapeCount() const {
        return shapes_.size();
    }
};

// Factory pattern using abstract classes
class ShapeFactory {
public:
    enum ShapeType { CIRCLE, RECTANGLE, TRIANGLE };

    static std::unique_ptr<Shape> createShape(
        ShapeType type,
        double x, double y,
        const std::string& color,
        const std::vector<double>& parameters) {

        switch (type) {
            case CIRCLE:
                if (parameters.size() >= 1) {
                    return std::make_unique<Circle>(x, y, parameters[0], color);
                }
                break;
            case RECTANGLE:
                if (parameters.size() >= 2) {
                    return std::make_unique<Rectangle>(x, y, parameters[0], parameters[1], color);
                }
                break;
            case TRIANGLE:
                if (parameters.size() >= 2) {
                    return std::make_unique<Triangle>(x, y, parameters[0], parameters[1], color);
                }
                break;
        }
        return nullptr;
    }
};

void demonstrateAbstractClasses() {
    // Create concrete objects
    Circle circle(10, 20, 5, "red");
    Rectangle rect(30, 40, 15, 10, "blue");
    Triangle tri(50, 60, 8, 12, "green");

    // Use through abstract interface
    Shape* shapes[] = {&circle, &rect, &tri};

    for (Shape* shape : shapes) {
        shape->describe();  // Virtual function call
        shape->draw();      // Pure virtual function call
        std::cout << "Area: " << shape->area() << std::endl;
        std::cout << "Perimeter: " << shape->perimeter() << std::endl;
        std::cout << std::endl;
    }

    // Test multiple interfaces
    IDrawable* drawables[] = {&circle, &rect, &tri};
    for (IDrawable* drawable : drawables) {
        drawable->draw();
        drawable->setColor("purple");
        std::cout << "Color changed to: " << drawable->getColor() << std::endl;
    }

    // Test resizable interface
    IResizable* resizables[] = {&circle, &rect};  // Triangle doesn't implement IResizable
    for (IResizable* resizable : resizables) {
        std::cout << "Original size: " << resizable->getSize() << std::endl;
        resizable->resize(1.5);
        std::cout << "Resized to: " << resizable->getSize() << std::endl;
    }

    // Test Canvas with polymorphic shapes
    Canvas<Shape> canvas;
    canvas.addShape(ShapeFactory::createShape(ShapeFactory::CIRCLE, 0, 0, "yellow", {3.0}));
    canvas.addShape(ShapeFactory::createShape(ShapeFactory::RECTANGLE, 10, 10, "cyan", {4.0, 6.0}));
    canvas.addShape(ShapeFactory::createShape(ShapeFactory::TRIANGLE, 20, 20, "magenta", {5.0, 7.0}));

    std::cout << "Canvas contains " << canvas.getShapeCount() << " shapes:" << std::endl;
    canvas.drawAll();

    std::cout << "Moving all shapes by (5, 5):" << std::endl;
    canvas.moveAll(5, 5);
    canvas.drawAll();
}

// Test pure virtual destructors and cleanup
class AbstractBase {
public:
    virtual ~AbstractBase() = 0;  // Pure virtual destructor
    virtual void process() = 0;
};

// Even pure virtual destructors need implementation
AbstractBase::~AbstractBase() {
    std::cout << "AbstractBase destructor called" << std::endl;
}

class ConcreteImplementation : public AbstractBase {
public:
    ~ConcreteImplementation() override {
        std::cout << "ConcreteImplementation destructor called" << std::endl;
    }

    void process() override {
        std::cout << "ConcreteImplementation::process() called" << std::endl;
    }
};

void testAbstractDestructors() {
    std::unique_ptr<AbstractBase> ptr = std::make_unique<ConcreteImplementation>();
    ptr->process();
    // Destructor chain will be called automatically when ptr goes out of scope
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_inheritance_project,
        parsers=parsers,
        queries=queries,
    )
    updater.run()

    project_name = cpp_inheritance_project.name

    # Expected abstract class inheritance relationships
    expected_abstract_inherits = [
        # Shape implements IDrawable and IMovable
        (
            ("Class", "qualified_name", f"{project_name}.abstract_interfaces.Shape"),
            (
                "Class",
                "qualified_name",
                f"{project_name}.abstract_interfaces.IDrawable",
            ),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.abstract_interfaces.Shape"),
            ("Class", "qualified_name", f"{project_name}.abstract_interfaces.IMovable"),
        ),
        # Circle inherits from Shape and implements IResizable
        (
            ("Class", "qualified_name", f"{project_name}.abstract_interfaces.Circle"),
            ("Class", "qualified_name", f"{project_name}.abstract_interfaces.Shape"),
        ),
        (
            ("Class", "qualified_name", f"{project_name}.abstract_interfaces.Circle"),
            (
                "Class",
                "qualified_name",
                f"{project_name}.abstract_interfaces.IResizable",
            ),
        ),
    ]

    # Verify INHERITS relationships are created
    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    found_abstract_relationships = 0
    for expected_child, expected_parent in expected_abstract_inherits:
        found = any(
            call[0][0] == expected_child and call[0][2] == expected_parent
            for call in relationship_calls
        )
        if found:
            found_abstract_relationships += 1

    assert found_abstract_relationships >= 3, (
        f"Expected at least 3 abstract inheritance relationships, found {found_abstract_relationships}"
    )

    # Verify virtual function calls
    call_relationships = [
        c
        for c in cast(MagicMock, mock_ingestor.ensure_relationship_batch).call_args_list
        if c.args[1] == "CALLS"
    ]

    virtual_interface_calls = [
        call
        for call in call_relationships
        if "abstract_interfaces" in call.args[0][2]
        and any(
            method in call.args[2][2]
            for method in ["draw", "move", "resize", "area", "perimeter", "describe"]
        )
    ]

    assert len(virtual_interface_calls) >= 8, (
        f"Expected at least 8 virtual interface calls, found {len(virtual_interface_calls)}"
    )


def test_cpp_inheritance_comprehensive(
    cpp_inheritance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all inheritance patterns create proper relationships."""
    test_file = cpp_inheritance_project / "comprehensive_inheritance.cpp"
    test_file.write_text(
        """
// Every C++ inheritance pattern in one file
#include <iostream>
#include <memory>
#include <vector>

// Base classes for different inheritance patterns
class Base {
public:
    virtual ~Base() = default;
    virtual void virtualMethod() const = 0;
    void nonVirtualMethod() const { std::cout << "Base::nonVirtualMethod" << std::endl; }
};

class Interface1 {
public:
    virtual ~Interface1() = default;
    virtual void interface1Method() = 0;
};

class Interface2 {
public:
    virtual ~Interface2() = default;
    virtual void interface2Method() = 0;
};

// Single inheritance
class SingleDerived : public Base {
public:
    void virtualMethod() const override {
        std::cout << "SingleDerived::virtualMethod" << std::endl;
    }

    void derivedMethod() const {
        std::cout << "SingleDerived::derivedMethod" << std::endl;
    }
};

// Multiple inheritance
class MultipleDerived : public Base, public Interface1, public Interface2 {
public:
    void virtualMethod() const override {
        std::cout << "MultipleDerived::virtualMethod" << std::endl;
    }

    void interface1Method() override {
        std::cout << "MultipleDerived::interface1Method" << std::endl;
    }

    void interface2Method() override {
        std::cout << "MultipleDerived::interface2Method" << std::endl;
    }
};

// Virtual inheritance for diamond problem
class VirtualBase {
protected:
    int value_;
public:
    VirtualBase(int value) : value_(value) {}
    virtual ~VirtualBase() = default;
    int getValue() const { return value_; }
};

class Left : public virtual VirtualBase {
public:
    Left(int value) : VirtualBase(value) {}
    void leftMethod() { std::cout << "Left::leftMethod" << std::endl; }
};

class Right : public virtual VirtualBase {
public:
    Right(int value) : VirtualBase(value) {}
    void rightMethod() { std::cout << "Right::rightMethod" << std::endl; }
};

class Diamond : public Left, public Right {
public:
    Diamond(int value) : VirtualBase(value), Left(value), Right(value) {}

    void diamondMethod() {
        std::cout << "Diamond::diamondMethod, value: " << getValue() << std::endl;
        leftMethod();
        rightMethod();
    }
};

// Inheritance chain
class GrandParent {
public:
    virtual ~GrandParent() = default;
    virtual void grandParentMethod() const {
        std::cout << "GrandParent::grandParentMethod" << std::endl;
    }
};

class Parent : public GrandParent {
public:
    void grandParentMethod() const override {
        std::cout << "Parent::grandParentMethod" << std::endl;
        GrandParent::grandParentMethod();  // Call base implementation
    }

    virtual void parentMethod() const {
        std::cout << "Parent::parentMethod" << std::endl;
    }
};

class Child : public Parent {
public:
    void parentMethod() const override {
        std::cout << "Child::parentMethod" << std::endl;
        Parent::parentMethod();  // Call base implementation
    }

    void childMethod() const {
        std::cout << "Child::childMethod" << std::endl;
    }
};

void demonstrateComprehensiveInheritance() {
    // Single inheritance
    SingleDerived single;
    single.virtualMethod();      // Virtual call
    single.nonVirtualMethod();   // Non-virtual call
    single.derivedMethod();      // Derived-specific method

    // Polymorphic calls
    Base* basePtr = &single;
    basePtr->virtualMethod();    // Virtual dispatch to SingleDerived
    basePtr->nonVirtualMethod(); // Non-virtual call to Base

    // Multiple inheritance
    MultipleDerived multiple;
    multiple.virtualMethod();    // Base virtual method
    multiple.interface1Method(); // Interface1 method
    multiple.interface2Method(); // Interface2 method

    // Polymorphic calls through different interfaces
    Base* base = &multiple;
    Interface1* iface1 = &multiple;
    Interface2* iface2 = &multiple;

    base->virtualMethod();
    iface1->interface1Method();
    iface2->interface2Method();

    // Virtual inheritance (diamond problem solution)
    Diamond diamond(42);
    diamond.diamondMethod();

    // Only one VirtualBase instance should exist
    VirtualBase* vb = &diamond;
    std::cout << "Diamond value through VirtualBase*: " << vb->getValue() << std::endl;

    // Inheritance chain
    Child child;
    child.grandParentMethod();   // Calls Parent's override
    child.parentMethod();        // Calls Child's override
    child.childMethod();         // Child-specific method

    // Polymorphic calls through inheritance chain
    GrandParent* gp = &child;
    Parent* p = &child;

    gp->grandParentMethod();     // Virtual dispatch to Parent's implementation
    p->parentMethod();           // Virtual dispatch to Child's implementation

    // Collection of polymorphic objects
    std::vector<std::unique_ptr<Base>> objects;
    objects.push_back(std::make_unique<SingleDerived>());
    objects.push_back(std::make_unique<MultipleDerived>());

    for (const auto& obj : objects) {
        obj->virtualMethod();    // Virtual dispatch to correct implementation
        obj->nonVirtualMethod(); // Always calls Base implementation
    }

    // Test inheritance with dynamic_cast
    MultipleDerived multiObj;
    Base* multiBase = &multiObj;

    if (auto* iface1Ptr = dynamic_cast<Interface1*>(multiBase)) {
        iface1Ptr->interface1Method();
    }

    if (auto* iface2Ptr = dynamic_cast<Interface2*>(multiBase)) {
        iface2Ptr->interface2Method();
    }

    // Demonstrate virtual destructor chain
    {
        std::unique_ptr<Parent> parentPtr = std::make_unique<Child>();
        // When parentPtr goes out of scope, Child destructor will be called first,
        // then Parent destructor, then GrandParent destructor
    }
}

// Template inheritance
template<typename T>
class TemplateBase {
protected:
    T value_;
public:
    TemplateBase(T value) : value_(value) {}
    virtual ~TemplateBase() = default;
    virtual void process() = 0;
    T getValue() const { return value_; }
};

class IntDerived : public TemplateBase<int> {
public:
    IntDerived(int value) : TemplateBase<int>(value) {}

    void process() override {
        std::cout << "IntDerived processing value: " << value_ << std::endl;
    }
};

class StringDerived : public TemplateBase<std::string> {
public:
    StringDerived(const std::string& value) : TemplateBase<std::string>(value) {}

    void process() override {
        std::cout << "StringDerived processing value: " << value_ << std::endl;
    }
};

void testTemplateInheritance() {
    IntDerived intObj(42);
    StringDerived stringObj("Hello");

    intObj.process();
    stringObj.process();

    // Cannot use polymorphism directly with template base classes
    // TemplateBase<int>* ptr = &intObj;  // This works
    // TemplateBase<std::string>* ptr2 = &stringObj;  // This works
}
"""
    )

    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=cpp_inheritance_project,
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
    inherits_relationships = [c for c in all_relationships if c.args[1] == "INHERITS"]

    # Should have comprehensive inheritance coverage
    comprehensive_inherits = [
        call
        for call in inherits_relationships
        if "comprehensive_inheritance" in call.args[0][2]
    ]

    assert len(comprehensive_inherits) >= 6, (
        f"Expected at least 6 comprehensive inheritance relationships, found {len(comprehensive_inherits)}"
    )

    # Should have many virtual function calls
    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_inheritance" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 15, (
        f"Expected at least 15 comprehensive virtual calls, found {len(comprehensive_calls)}"
    )

    # Verify relationship structure
    for relationship in comprehensive_inherits:
        assert len(relationship.args) == 3, (
            "Inheritance relationship should have 3 args"
        )
        assert relationship.args[1] == "INHERITS", "Second arg should be 'INHERITS'"

        source_class = relationship.args[0][2]
        target_class = relationship.args[2][2]

        # Source should be our test module
        assert "comprehensive_inheritance" in source_class, (
            f"Source class should contain test file name: {source_class}"
        )

        # Target should be a valid class name
        assert isinstance(target_class, str) and target_class, (
            f"Target should be non-empty string: {target_class}"
        )

    # Test that inheritance parsing doesn't interfere with other relationships
    assert defines_relationships, "Should still have DEFINES relationships"
