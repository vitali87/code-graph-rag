from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def cpp_inheritance_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with inheritance patterns."""
    project_path = temp_repo / "cpp_inheritance_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    (project_path / "include" / "shapes.h").write_text(
        encoding="utf-8", data="#pragma once\nclass Shape {};"
    )

    return project_path


def test_single_inheritance(
    cpp_inheritance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test single inheritance patterns and virtual functions."""
    test_file = cpp_inheritance_project / "single_inheritance.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
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
""",
    )

    run_updater(cpp_inheritance_project, mock_ingestor)

    project_name = cpp_inheritance_project.name

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

    call_relationships = get_relationships(mock_ingestor, "CALLS")

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
        encoding="utf-8",
        data="""
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
""",
    )

    run_updater(cpp_inheritance_project, mock_ingestor)

    project_name = cpp_inheritance_project.name

    expected_multiple_inherits = [
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
        (
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Pet"),
            ("Class", "qualified_name", f"{project_name}.multiple_inheritance.Duck"),
        ),
    ]

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
        encoding="utf-8",
        data="""
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
""",
    )

    run_updater(cpp_inheritance_project, mock_ingestor)

    project_name = cpp_inheritance_project.name

    expected_abstract_inherits = [
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

    call_relationships = get_relationships(mock_ingestor, "CALLS")

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
        encoding="utf-8",
        data="""
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
""",
    )

    run_updater(cpp_inheritance_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")
    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    comprehensive_inherits = [
        call
        for call in inherits_relationships
        if "comprehensive_inheritance" in call.args[0][2]
    ]

    assert len(comprehensive_inherits) >= 6, (
        f"Expected at least 6 comprehensive inheritance relationships, found {len(comprehensive_inherits)}"
    )

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_inheritance" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 15, (
        f"Expected at least 15 comprehensive virtual calls, found {len(comprehensive_calls)}"
    )

    for relationship in comprehensive_inherits:
        assert len(relationship.args) == 3, (
            "Inheritance relationship should have 3 args"
        )
        assert relationship.args[1] == "INHERITS", "Second arg should be 'INHERITS'"

        source_class = relationship.args[0][2]
        target_class = relationship.args[2][2]

        assert "comprehensive_inheritance" in source_class, (
            f"Source class should contain test file name: {source_class}"
        )

        assert isinstance(target_class, str) and target_class, (
            f"Target should be non-empty string: {target_class}"
        )

    assert defines_relationships, "Should still have DEFINES relationships"


def test_cpp_inheritance_edge_cases(
    cpp_inheritance_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test edge cases in C++ inheritance parsing including complex templates and namespaces."""
    test_file = cpp_inheritance_project / "edge_case_inheritance.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Edge cases for C++ inheritance parsing
#include <vector>
#include <memory>

// Test complex template inheritance with multiple template parameters
template<typename T, typename U = int>
class ComplexBase {
public:
    virtual ~ComplexBase() = default;
    virtual void process(const T& data, const U& meta) = 0;
};

// Test nested namespace template inheritance
namespace outer {
    namespace inner {
        template<typename DataType>
        class NestedTemplate {
        public:
            virtual void handle(const DataType& data) = 0;
        };

        class ConcreteNested : public NestedTemplate<std::string> {
        public:
            void handle(const std::string& data) override {}
        };
    }
}

// Test multiple template inheritance
template<typename T>
class Processor : public ComplexBase<T, double>,
                  public outer::inner::NestedTemplate<T> {
public:
    void process(const T& data, const double& meta) override {}
    void handle(const T& data) override {}
};

// Test template specialization with inheritance
template<>
class Processor<std::vector<int>> : public ComplexBase<std::vector<int>>,
                                    public outer::inner::NestedTemplate<int> {
public:
    void process(const std::vector<int>& data, const double& meta) override {}
    void handle(const int& data) override {}
};

// Test CRTP (Curiously Recurring Template Pattern)
template<typename Derived>
class CRTP_Base {
public:
    void interface() {
        static_cast<Derived*>(this)->implementation();
    }

protected:
    virtual ~CRTP_Base() = default;
};

class CRTP_Derived : public CRTP_Base<CRTP_Derived> {
public:
    void implementation() {
        // CRTP implementation
    }
};

// Test complex virtual inheritance with templates
template<typename T>
class VirtualBase {
protected:
    T data_;
public:
    VirtualBase(const T& data) : data_(data) {}
    virtual ~VirtualBase() = default;
};

template<typename T>
class LeftMixin : public virtual VirtualBase<T> {
public:
    LeftMixin(const T& data) : VirtualBase<T>(data) {}
    virtual void leftMethod() {}
};

template<typename T>
class RightMixin : public virtual VirtualBase<T> {
public:
    RightMixin(const T& data) : VirtualBase<T>(data) {}
    virtual void rightMethod() {}
};

// Diamond inheritance with templates
class DiamondDerived : public LeftMixin<std::string>,
                      public RightMixin<std::string> {
public:
    DiamondDerived(const std::string& data)
        : VirtualBase<std::string>(data), LeftMixin<std::string>(data), RightMixin<std::string>(data) {}

    void combinedMethod() {
        leftMethod();
        rightMethod();
    }
};

// Test variadic template inheritance
template<typename... Args>
class VariadicBase {
public:
    virtual ~VariadicBase() = default;
    virtual void process(Args... args) = 0;
};

class VariadicDerived : public VariadicBase<int, std::string, double> {
public:
    void process(int i, std::string s, double d) override {}
};

// Test private/protected inheritance
class PrivateInheritance : private ComplexBase<int> {
public:
    void publicMethod() {}
};

class ProtectedInheritance : protected outer::inner::NestedTemplate<float> {
public:
    void handle(const float& data) override {}
};

// Test inheritance with nested classes
class OuterWithNested {
public:
    class NestedBase {
    public:
        virtual ~NestedBase() = default;
        virtual void nestedMethod() = 0;
    };

    class NestedDerived : public NestedBase {
    public:
        void nestedMethod() override {}
    };
};

void demonstrateEdgeCases() {
    // Test instantiation of complex inheritance hierarchies
    Processor<std::string> stringProcessor;
    stringProcessor.process("test", 3.14);
    stringProcessor.handle("test");

    // Test template specialization
    Processor<std::vector<int>> vectorProcessor;
    std::vector<int> vec{1, 2, 3};
    vectorProcessor.process(vec, 2.71);
    vectorProcessor.handle(42);

    // Test CRTP
    CRTP_Derived crtp;
    crtp.interface();

    // Test diamond inheritance
    DiamondDerived diamond("diamond_data");
    diamond.combinedMethod();
    diamond.leftMethod();
    diamond.rightMethod();

    // Test variadic templates
    VariadicDerived variadic;
    variadic.process(42, "hello", 3.14159);

    // Test private inheritance (can access through public methods)
    PrivateInheritance privateObj;
    privateObj.publicMethod();

    // Test protected inheritance
    ProtectedInheritance protectedObj;
    protectedObj.handle(2.5f);

    // Test nested class inheritance
    OuterWithNested::NestedDerived nested;
    nested.nestedMethod();
}
""",
    )

    run_updater(cpp_inheritance_project, mock_ingestor)

    relationship_calls = [
        call
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if len(call[0]) >= 3 and call[0][1] == "INHERITS"
    ]

    edge_case_inherits = [
        call for call in relationship_calls if "edge_case_inheritance" in call[0][0][2]
    ]

    assert len(edge_case_inherits) >= 10, (
        f"Expected at least 10 edge case inheritance relationships, found {len(edge_case_inherits)}"
    )

    specialization_inherits = [
        call for call in edge_case_inherits if "std::vector<int>" in str(call[0][0])
    ]

    assert len(specialization_inherits) >= 1, (
        f"Expected template specialization inheritance, found {len(specialization_inherits)}"
    )

    crtp_inherits = [
        call
        for call in edge_case_inherits
        if "CRTP" in call[0][0][2] and "CRTP" in call[0][2][2]
    ]

    assert len(crtp_inherits) >= 1, (
        f"Expected CRTP inheritance pattern, found {len(crtp_inherits)}"
    )

    diamond_inherits = [
        call
        for call in edge_case_inherits
        if ("LeftMixin" in call[0][0][2] and "VirtualBase" in call[0][2][2])
        or ("RightMixin" in call[0][0][2] and "VirtualBase" in call[0][2][2])
        or ("DiamondDerived" in call[0][0][2])
    ]

    assert len(diamond_inherits) >= 3, (
        f"Expected diamond inheritance with virtual bases, found {len(diamond_inherits)}"
    )

    nested_ns_inherits = [
        call
        for call in edge_case_inherits
        if "NestedTemplate" in call[0][2][2] or "ComplexBase" in call[0][2][2]
    ]

    assert len(nested_ns_inherits) >= 2, (
        f"Expected nested namespace template inheritance, found {len(nested_ns_inherits)}"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    edge_case_calls = [
        call
        for call in call_relationships
        if "edge_case_inheritance" in call.args[0][2]
    ]

    assert len(edge_case_calls) >= 5, (
        f"Expected complex inheritance to preserve function calls, found {len(edge_case_calls)}"
    )
