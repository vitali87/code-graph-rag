from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater


@pytest.fixture
def java_complex_project(temp_repo: Path) -> Path:
    """Create a Java project structure for complex relationship testing."""
    project_path = temp_repo / "java_complex_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_builder_pattern_relationships(
    java_complex_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that builder pattern relationships are correctly captured."""
    test_file = (
        java_complex_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "BuilderPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

public class Computer {
    private final String cpu;
    private final String ram;
    private final String storage;
    private final String gpu;
    private final boolean hasWifi;

    private Computer(Builder builder) {
        this.cpu = builder.cpu;
        this.ram = builder.ram;
        this.storage = builder.storage;
        this.gpu = builder.gpu;
        this.hasWifi = builder.hasWifi;
    }

    public String getCpu() { return cpu; }
    public String getRam() { return ram; }
    public String getStorage() { return storage; }
    public String getGpu() { return gpu; }
    public boolean hasWifi() { return hasWifi; }

    public String getSpecs() {
        return "CPU: " + cpu + ", RAM: " + ram + ", Storage: " + storage +
               ", GPU: " + gpu + ", WiFi: " + hasWifi;
    }

    // Static inner builder class
    public static class Builder {
        private String cpu;
        private String ram;
        private String storage;
        private String gpu;
        private boolean hasWifi;

        public Builder setCpu(String cpu) {
            this.cpu = cpu;
            return this; // Method chaining
        }

        public Builder setRam(String ram) {
            this.ram = ram;
            return this; // Method chaining
        }

        public Builder setStorage(String storage) {
            this.storage = storage;
            return this; // Method chaining
        }

        public Builder setGpu(String gpu) {
            this.gpu = gpu;
            return this; // Method chaining
        }

        public Builder setWifi(boolean hasWifi) {
            this.hasWifi = hasWifi;
            return this; // Method chaining
        }

        public Computer build() {
            return new Computer(this); // CALLS Computer constructor
        }

        public Builder reset() {
            this.cpu = null;
            this.ram = null;
            this.storage = null;
            this.gpu = null;
            this.hasWifi = false;
            return this; // Method chaining
        }
    }

    public static Builder builder() {
        return new Builder(); // CALLS Builder constructor
    }
}

class ComputerFactory {

    public Computer createGamingComputer() {
        return Computer.builder() // CALLS Computer.builder()
            .setCpu("Intel i9") // CALLS Builder.setCpu()
            .setRam("32GB DDR4") // CALLS Builder.setRam()
            .setStorage("1TB NVMe SSD") // CALLS Builder.setStorage()
            .setGpu("RTX 4080") // CALLS Builder.setGpu()
            .setWifi(true) // CALLS Builder.setWifi()
            .build(); // CALLS Builder.build()
    }

    public Computer createOfficeComputer() {
        Computer.Builder builder = Computer.builder(); // CALLS Computer.builder()

        builder.setCpu("Intel i5"); // CALLS Builder.setCpu()
        builder.setRam("16GB DDR4"); // CALLS Builder.setRam()
        builder.setStorage("512GB SSD"); // CALLS Builder.setStorage()
        builder.setWifi(true); // CALLS Builder.setWifi()

        return builder.build(); // CALLS Builder.build()
    }

    public Computer createBudgetComputer() {
        return new Computer.Builder() // CALLS Builder constructor
            .setCpu("AMD Ryzen 3") // CALLS Builder.setCpu()
            .setRam("8GB DDR4") // CALLS Builder.setRam()
            .setStorage("256GB SSD") // CALLS Builder.setStorage()
            .setWifi(false) // CALLS Builder.setWifi()
            .build(); // CALLS Builder.build()
    }
}

class ComputerService {
    private ComputerFactory factory;

    public ComputerService() {
        this.factory = new ComputerFactory(); // CALLS ComputerFactory constructor
    }

    public void demonstrateBuilderPattern() {
        Computer gaming = factory.createGamingComputer(); // CALLS ComputerFactory.createGamingComputer()
        Computer office = factory.createOfficeComputer(); // CALLS ComputerFactory.createOfficeComputer()
        Computer budget = factory.createBudgetComputer(); // CALLS ComputerFactory.createBudgetComputer()

        System.out.println("Gaming: " + gaming.getSpecs()); // CALLS Computer.getSpecs()
        System.out.println("Office: " + office.getSpecs()); // CALLS Computer.getSpecs()
        System.out.println("Budget: " + budget.getSpecs()); // CALLS Computer.getSpecs()
    }
}
""",
    )

    run_updater(java_complex_project, mock_ingestor, skip_if_missing="java")

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for builder pattern"
    )


def test_observer_pattern_relationships(
    java_complex_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that observer pattern relationships are correctly captured."""
    test_file = (
        java_complex_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ObserverPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.List;
import java.util.ArrayList;

interface Observer {
    void update(String message);
}

interface Subject {
    void addObserver(Observer observer);
    void removeObserver(Observer observer);
    void notifyObservers();
}

class NewsAgency implements Subject {
    private List<Observer> observers;
    private String news;

    public NewsAgency() {
        this.observers = new ArrayList<>(); // CALLS ArrayList constructor
    }

    @Override
    public void addObserver(Observer observer) {
        observers.add(observer); // CALLS List.add()
    }

    @Override
    public void removeObserver(Observer observer) {
        observers.remove(observer); // CALLS List.remove()
    }

    @Override
    public void notifyObservers() {
        for (Observer observer : observers) {
            observer.update(news); // CALLS Observer.update()
        }
    }

    public void setNews(String news) {
        this.news = news;
        notifyObservers(); // CALLS this.notifyObservers()
    }

    public String getNews() {
        return news;
    }
}

class NewsChannel implements Observer {
    private String name;
    private String latestNews;

    public NewsChannel(String name) {
        this.name = name;
    }

    @Override
    public void update(String message) {
        this.latestNews = message;
        displayNews(); // CALLS this.displayNews()
    }

    public void displayNews() {
        System.out.println(name + " reports: " + latestNews);
    }

    public String getName() {
        return name;
    }

    public String getLatestNews() {
        return latestNews;
    }
}

class EmailNotifier implements Observer {
    private String emailAddress;

    public EmailNotifier(String emailAddress) {
        this.emailAddress = emailAddress;
    }

    @Override
    public void update(String message) {
        sendEmail(message); // CALLS this.sendEmail()
    }

    private void sendEmail(String news) {
        System.out.println("Sending email to " + emailAddress + ": " + news);
    }

    public String getEmailAddress() {
        return emailAddress;
    }
}

class SMSNotifier implements Observer {
    private String phoneNumber;

    public SMSNotifier(String phoneNumber) {
        this.phoneNumber = phoneNumber;
    }

    @Override
    public void update(String message) {
        sendSMS(message); // CALLS this.sendSMS()
    }

    private void sendSMS(String news) {
        System.out.println("Sending SMS to " + phoneNumber + ": " + news);
    }

    public String getPhoneNumber() {
        return phoneNumber;
    }
}

class NewsSystem {

    public void demonstrateObserverPattern() {
        // Create the subject (news agency)
        NewsAgency agency = new NewsAgency(); // CALLS NewsAgency constructor

        // Create observers
        NewsChannel cnn = new NewsChannel("CNN"); // CALLS NewsChannel constructor
        NewsChannel bbc = new NewsChannel("BBC"); // CALLS NewsChannel constructor
        EmailNotifier emailNotifier = new EmailNotifier("user@example.com"); // CALLS EmailNotifier constructor
        SMSNotifier smsNotifier = new SMSNotifier("123-456-7890"); // CALLS SMSNotifier constructor

        // Register observers with subject
        agency.addObserver(cnn); // CALLS NewsAgency.addObserver()
        agency.addObserver(bbc); // CALLS NewsAgency.addObserver()
        agency.addObserver(emailNotifier); // CALLS NewsAgency.addObserver()
        agency.addObserver(smsNotifier); // CALLS NewsAgency.addObserver()

        // Publish news (will notify all observers)
        agency.setNews("Breaking: Major technological breakthrough!"); // CALLS NewsAgency.setNews()

        // Add another observer dynamically
        NewsChannel fox = new NewsChannel("Fox News"); // CALLS NewsChannel constructor
        agency.addObserver(fox); // CALLS NewsAgency.addObserver()

        // Publish more news
        agency.setNews("Update: Details about the breakthrough revealed"); // CALLS NewsAgency.setNews()

        // Remove an observer
        agency.removeObserver(emailNotifier); // CALLS NewsAgency.removeObserver()

        // Final news update (emailNotifier won't receive this)
        agency.setNews("Final: Breakthrough applications announced"); // CALLS NewsAgency.setNews()

        // Get latest news from channels
        System.out.println("CNN latest: " + cnn.getLatestNews()); // CALLS NewsChannel.getLatestNews()
        System.out.println("BBC latest: " + bbc.getLatestNews()); // CALLS NewsChannel.getLatestNews()
        System.out.println("Fox latest: " + fox.getLatestNews()); // CALLS NewsChannel.getLatestNews()
    }
}
""",
    )

    run_updater(java_complex_project, mock_ingestor, skip_if_missing="java")

    implements_relationships = get_relationships(mock_ingestor, "IMPLEMENTS")

    assert len(implements_relationships) > 0, (
        "No interface implementation relationships found for observer pattern"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for observer pattern"
    )


def test_factory_pattern_relationships(
    java_complex_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that factory pattern relationships are correctly captured."""
    test_file = (
        java_complex_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "FactoryPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

// Product interface
interface Vehicle {
    void start();
    void stop();
    String getType();
    double getMaxSpeed();
}

// Concrete products
class Car implements Vehicle {
    private String model;
    private double maxSpeed;

    public Car(String model, double maxSpeed) {
        this.model = model;
        this.maxSpeed = maxSpeed;
    }

    @Override
    public void start() {
        System.out.println("Car " + model + " is starting engine");
    }

    @Override
    public void stop() {
        System.out.println("Car " + model + " engine stopped");
    }

    @Override
    public String getType() {
        return "Car (" + model + ")";
    }

    @Override
    public double getMaxSpeed() {
        return maxSpeed;
    }
}

class Motorcycle implements Vehicle {
    private String brand;
    private double maxSpeed;

    public Motorcycle(String brand, double maxSpeed) {
        this.brand = brand;
        this.maxSpeed = maxSpeed;
    }

    @Override
    public void start() {
        System.out.println("Motorcycle " + brand + " engine started");
    }

    @Override
    public void stop() {
        System.out.println("Motorcycle " + brand + " engine stopped");
    }

    @Override
    public String getType() {
        return "Motorcycle (" + brand + ")";
    }

    @Override
    public double getMaxSpeed() {
        return maxSpeed;
    }
}

class Truck implements Vehicle {
    private String capacity;
    private double maxSpeed;

    public Truck(String capacity, double maxSpeed) {
        this.capacity = capacity;
        this.maxSpeed = maxSpeed;
    }

    @Override
    public void start() {
        System.out.println("Truck with " + capacity + " capacity starting");
    }

    @Override
    public void stop() {
        System.out.println("Truck stopped");
    }

    @Override
    public String getType() {
        return "Truck (" + capacity + ")";
    }

    @Override
    public double getMaxSpeed() {
        return maxSpeed;
    }
}

// Abstract factory
abstract class VehicleFactory {
    public abstract Vehicle createVehicle(String specification);

    // Template method that uses factory method
    public Vehicle createAndTest(String specification) {
        Vehicle vehicle = createVehicle(specification); // CALLS createVehicle()

        // Test the vehicle
        vehicle.start(); // CALLS Vehicle.start()
        System.out.println("Testing " + vehicle.getType() + " - Max speed: " + vehicle.getMaxSpeed()); // CALLS Vehicle.getType() and getMaxSpeed()
        vehicle.stop(); // CALLS Vehicle.stop()

        return vehicle;
    }
}

// Concrete factories
class CarFactory extends VehicleFactory {
    @Override
    public Vehicle createVehicle(String model) {
        switch (model.toLowerCase()) {
            case "sedan":
                return new Car("Sedan", 180.0); // CALLS Car constructor
            case "suv":
                return new Car("SUV", 160.0); // CALLS Car constructor
            case "sports":
                return new Car("Sports Car", 250.0); // CALLS Car constructor
            default:
                return new Car("Standard Car", 150.0); // CALLS Car constructor
        }
    }
}

class MotorcycleFactory extends VehicleFactory {
    @Override
    public Vehicle createVehicle(String brand) {
        switch (brand.toLowerCase()) {
            case "harley":
                return new Motorcycle("Harley Davidson", 200.0); // CALLS Motorcycle constructor
            case "honda":
                return new Motorcycle("Honda", 180.0); // CALLS Motorcycle constructor
            case "yamaha":
                return new Motorcycle("Yamaha", 220.0); // CALLS Motorcycle constructor
            default:
                return new Motorcycle("Generic", 160.0); // CALLS Motorcycle constructor
        }
    }
}

class TruckFactory extends VehicleFactory {
    @Override
    public Vehicle createVehicle(String capacity) {
        switch (capacity.toLowerCase()) {
            case "small":
                return new Truck("2 tons", 120.0); // CALLS Truck constructor
            case "medium":
                return new Truck("5 tons", 100.0); // CALLS Truck constructor
            case "large":
                return new Truck("10 tons", 90.0); // CALLS Truck constructor
            default:
                return new Truck("3 tons", 110.0); // CALLS Truck constructor
        }
    }
}

// Factory producer
class VehicleFactoryProducer {

    public static VehicleFactory getFactory(String vehicleType) {
        switch (vehicleType.toLowerCase()) {
            case "car":
                return new CarFactory(); // CALLS CarFactory constructor
            case "motorcycle":
                return new MotorcycleFactory(); // CALLS MotorcycleFactory constructor
            case "truck":
                return new TruckFactory(); // CALLS TruckFactory constructor
            default:
                throw new IllegalArgumentException("Unknown vehicle type: " + vehicleType);
        }
    }
}

class VehicleProduction {

    public void demonstrateFactoryPattern() {
        // Get different factories
        VehicleFactory carFactory = VehicleFactoryProducer.getFactory("car"); // CALLS VehicleFactoryProducer.getFactory()
        VehicleFactory motorcycleFactory = VehicleFactoryProducer.getFactory("motorcycle"); // CALLS VehicleFactoryProducer.getFactory()
        VehicleFactory truckFactory = VehicleFactoryProducer.getFactory("truck"); // CALLS VehicleFactoryProducer.getFactory()

        // Create and test vehicles
        Vehicle sedan = carFactory.createAndTest("sedan"); // CALLS VehicleFactory.createAndTest()
        Vehicle suv = carFactory.createAndTest("suv"); // CALLS VehicleFactory.createAndTest()

        Vehicle harley = motorcycleFactory.createAndTest("harley"); // CALLS VehicleFactory.createAndTest()
        Vehicle honda = motorcycleFactory.createAndTest("honda"); // CALLS VehicleFactory.createAndTest()

        Vehicle smallTruck = truckFactory.createAndTest("small"); // CALLS VehicleFactory.createAndTest()
        Vehicle largeTruck = truckFactory.createAndTest("large"); // CALLS VehicleFactory.createAndTest()

        // Use vehicles polymorphically
        Vehicle[] fleet = {sedan, suv, harley, honda, smallTruck, largeTruck};

        for (Vehicle vehicle : fleet) {
            System.out.println("Fleet vehicle: " + vehicle.getType() + " - " + vehicle.getMaxSpeed() + " km/h"); // CALLS Vehicle methods
        }
    }
}
""",
    )

    run_updater(java_complex_project, mock_ingestor, skip_if_missing="java")

    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    assert len(inherits_relationships) > 0, (
        "No inheritance relationships found for factory pattern"
    )

    implements_relationships = get_relationships(mock_ingestor, "IMPLEMENTS")

    assert len(implements_relationships) > 0, (
        "No interface implementation relationships found for factory pattern"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for factory pattern"
    )


def test_decorator_pattern_relationships(
    java_complex_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that decorator pattern relationships are correctly captured."""
    test_file = (
        java_complex_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "DecoratorPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

// Component interface
interface Coffee {
    String getDescription();
    double getCost();
}

// Concrete component
class SimpleCoffee implements Coffee {
    @Override
    public String getDescription() {
        return "Simple coffee";
    }

    @Override
    public double getCost() {
        return 2.0;
    }
}

// Base decorator
abstract class CoffeeDecorator implements Coffee {
    protected Coffee coffee;

    public CoffeeDecorator(Coffee coffee) {
        this.coffee = coffee;
    }

    @Override
    public String getDescription() {
        return coffee.getDescription(); // CALLS wrapped Coffee.getDescription()
    }

    @Override
    public double getCost() {
        return coffee.getCost(); // CALLS wrapped Coffee.getCost()
    }
}

// Concrete decorators
class MilkDecorator extends CoffeeDecorator {
    public MilkDecorator(Coffee coffee) {
        super(coffee); // CALLS CoffeeDecorator constructor
    }

    @Override
    public String getDescription() {
        return super.getDescription() + ", with milk"; // CALLS CoffeeDecorator.getDescription()
    }

    @Override
    public double getCost() {
        return super.getCost() + 0.5; // CALLS CoffeeDecorator.getCost()
    }
}

class SugarDecorator extends CoffeeDecorator {
    public SugarDecorator(Coffee coffee) {
        super(coffee); // CALLS CoffeeDecorator constructor
    }

    @Override
    public String getDescription() {
        return super.getDescription() + ", with sugar"; // CALLS CoffeeDecorator.getDescription()
    }

    @Override
    public double getCost() {
        return super.getCost() + 0.3; // CALLS CoffeeDecorator.getCost()
    }
}

class VanillaDecorator extends CoffeeDecorator {
    public VanillaDecorator(Coffee coffee) {
        super(coffee); // CALLS CoffeeDecorator constructor
    }

    @Override
    public String getDescription() {
        return super.getDescription() + ", with vanilla"; // CALLS CoffeeDecorator.getDescription()
    }

    @Override
    public double getCost() {
        return super.getCost() + 0.7; // CALLS CoffeeDecorator.getCost()
    }
}

class WhippedCreamDecorator extends CoffeeDecorator {
    public WhippedCreamDecorator(Coffee coffee) {
        super(coffee); // CALLS CoffeeDecorator constructor
    }

    @Override
    public String getDescription() {
        return super.getDescription() + ", with whipped cream"; // CALLS CoffeeDecorator.getDescription()
    }

    @Override
    public double getCost() {
        return super.getCost() + 1.0; // CALLS CoffeeDecorator.getCost()
    }
}

class CoffeeShop {

    public void demonstrateDecoratorPattern() {
        // Start with simple coffee
        Coffee coffee = new SimpleCoffee(); // CALLS SimpleCoffee constructor
        System.out.println(coffee.getDescription() + " - $" + coffee.getCost()); // CALLS Coffee methods

        // Add milk
        coffee = new MilkDecorator(coffee); // CALLS MilkDecorator constructor
        System.out.println(coffee.getDescription() + " - $" + coffee.getCost()); // CALLS Coffee methods

        // Add sugar
        coffee = new SugarDecorator(coffee); // CALLS SugarDecorator constructor
        System.out.println(coffee.getDescription() + " - $" + coffee.getCost()); // CALLS Coffee methods

        // Add vanilla
        coffee = new VanillaDecorator(coffee); // CALLS VanillaDecorator constructor
        System.out.println(coffee.getDescription() + " - $" + coffee.getCost()); // CALLS Coffee methods

        // Add whipped cream
        coffee = new WhippedCreamDecorator(coffee); // CALLS WhippedCreamDecorator constructor
        System.out.println(coffee.getDescription() + " - $" + coffee.getCost()); // CALLS Coffee methods
    }

    public Coffee createCustomCoffee(boolean milk, boolean sugar, boolean vanilla, boolean whippedCream) {
        Coffee coffee = new SimpleCoffee(); // CALLS SimpleCoffee constructor

        if (milk) {
            coffee = new MilkDecorator(coffee); // CALLS MilkDecorator constructor
        }

        if (sugar) {
            coffee = new SugarDecorator(coffee); // CALLS SugarDecorator constructor
        }

        if (vanilla) {
            coffee = new VanillaDecorator(coffee); // CALLS VanillaDecorator constructor
        }

        if (whippedCream) {
            coffee = new WhippedCreamDecorator(coffee); // CALLS WhippedCreamDecorator constructor
        }

        return coffee;
    }

    public void testDifferentCombinations() {
        // Test various combinations
        Coffee[] orders = {
            createCustomCoffee(true, false, false, false), // CALLS createCustomCoffee()
            createCustomCoffee(true, true, false, false), // CALLS createCustomCoffee()
            createCustomCoffee(false, false, true, true), // CALLS createCustomCoffee()
            createCustomCoffee(true, true, true, true) // CALLS createCustomCoffee()
        };

        for (int i = 0; i < orders.length; i++) {
            Coffee order = orders[i];
            System.out.println("Order " + (i + 1) + ": " + order.getDescription() + " - $" + order.getCost()); // CALLS Coffee methods
        }
    }
}
""",
    )

    run_updater(java_complex_project, mock_ingestor, skip_if_missing="java")

    inherits_relationships = get_relationships(mock_ingestor, "INHERITS")

    assert len(inherits_relationships) > 0, (
        "No inheritance relationships found for decorator pattern"
    )

    implements_relationships = get_relationships(mock_ingestor, "IMPLEMENTS")

    assert len(implements_relationships) > 0, (
        "No interface implementation relationships found for decorator pattern"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for decorator pattern"
    )


def test_strategy_pattern_relationships(
    java_complex_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that strategy pattern relationships are correctly captured."""
    test_file = (
        java_complex_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StrategyPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

// Strategy interface
interface PaymentStrategy {
    boolean pay(double amount);
    String getPaymentType();
}

// Concrete strategies
class CreditCardPayment implements PaymentStrategy {
    private String cardNumber;
    private String holderName;

    public CreditCardPayment(String cardNumber, String holderName) {
        this.cardNumber = cardNumber;
        this.holderName = holderName;
    }

    @Override
    public boolean pay(double amount) {
        System.out.println("Paid $" + amount + " using credit card ending in " + cardNumber.substring(cardNumber.length() - 4));
        return validateCard() && amount > 0; // CALLS this.validateCard()
    }

    @Override
    public String getPaymentType() {
        return "Credit Card";
    }

    private boolean validateCard() {
        // Simulate card validation
        return cardNumber.length() == 16;
    }
}

class PayPalPayment implements PaymentStrategy {
    private String email;

    public PayPalPayment(String email) {
        this.email = email;
    }

    @Override
    public boolean pay(double amount) {
        System.out.println("Paid $" + amount + " using PayPal account: " + email);
        return validateEmail() && amount > 0; // CALLS this.validateEmail()
    }

    @Override
    public String getPaymentType() {
        return "PayPal";
    }

    private boolean validateEmail() {
        return email.contains("@");
    }
}

class BankTransferPayment implements PaymentStrategy {
    private String accountNumber;
    private String bankCode;

    public BankTransferPayment(String accountNumber, String bankCode) {
        this.accountNumber = accountNumber;
        this.bankCode = bankCode;
    }

    @Override
    public boolean pay(double amount) {
        System.out.println("Paid $" + amount + " via bank transfer to account " + accountNumber);
        return validateAccount() && amount > 0; // CALLS this.validateAccount()
    }

    @Override
    public String getPaymentType() {
        return "Bank Transfer";
    }

    private boolean validateAccount() {
        return accountNumber.length() >= 8 && bankCode.length() >= 3;
    }
}

// Context class
class ShoppingCart {
    private PaymentStrategy paymentStrategy;
    private double totalAmount;

    public void setPaymentStrategy(PaymentStrategy paymentStrategy) {
        this.paymentStrategy = paymentStrategy;
    }

    public void addItem(double price) {
        totalAmount += price;
    }

    public boolean checkout() {
        if (paymentStrategy == null) {
            System.out.println("No payment method selected");
            return false;
        }

        System.out.println("Processing payment of $" + totalAmount + " using " + paymentStrategy.getPaymentType()); // CALLS PaymentStrategy.getPaymentType()
        boolean success = paymentStrategy.pay(totalAmount); // CALLS PaymentStrategy.pay()

        if (success) {
            totalAmount = 0; // Reset cart after successful payment
        }

        return success;
    }

    public double getTotalAmount() {
        return totalAmount;
    }

    public String getCurrentPaymentMethod() {
        return paymentStrategy != null ? paymentStrategy.getPaymentType() : "None"; // CALLS PaymentStrategy.getPaymentType()
    }
}

class PaymentProcessor {

    public void demonstrateStrategyPattern() {
        ShoppingCart cart = new ShoppingCart(); // CALLS ShoppingCart constructor

        // Add items to cart
        cart.addItem(25.99); // CALLS ShoppingCart.addItem()
        cart.addItem(15.50); // CALLS ShoppingCart.addItem()
        cart.addItem(8.75); // CALLS ShoppingCart.addItem()

        System.out.println("Total amount: $" + cart.getTotalAmount()); // CALLS ShoppingCart.getTotalAmount()

        // Try checkout without payment method
        cart.checkout(); // CALLS ShoppingCart.checkout()

        // Set credit card payment strategy
        PaymentStrategy creditCard = new CreditCardPayment("1234567890123456", "John Doe"); // CALLS CreditCardPayment constructor
        cart.setPaymentStrategy(creditCard); // CALLS ShoppingCart.setPaymentStrategy()

        System.out.println("Current payment method: " + cart.getCurrentPaymentMethod()); // CALLS ShoppingCart.getCurrentPaymentMethod()
        cart.checkout(); // CALLS ShoppingCart.checkout()

        // Add more items and try PayPal
        cart.addItem(12.99); // CALLS ShoppingCart.addItem()
        cart.addItem(7.25); // CALLS ShoppingCart.addItem()

        PaymentStrategy paypal = new PayPalPayment("user@example.com"); // CALLS PayPalPayment constructor
        cart.setPaymentStrategy(paypal); // CALLS ShoppingCart.setPaymentStrategy()
        cart.checkout(); // CALLS ShoppingCart.checkout()

        // Add items and try bank transfer
        cart.addItem(99.99); // CALLS ShoppingCart.addItem()

        PaymentStrategy bankTransfer = new BankTransferPayment("12345678", "ABC"); // CALLS BankTransferPayment constructor
        cart.setPaymentStrategy(bankTransfer); // CALLS ShoppingCart.setPaymentStrategy()
        cart.checkout(); // CALLS ShoppingCart.checkout()
    }
}
""",
    )

    run_updater(java_complex_project, mock_ingestor, skip_if_missing="java")

    implements_relationships = get_relationships(mock_ingestor, "IMPLEMENTS")

    assert len(implements_relationships) > 0, (
        "No interface implementation relationships found for strategy pattern"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for strategy pattern"
    )


def test_command_pattern_relationships(
    java_complex_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test that command pattern relationships are correctly captured."""
    test_file = (
        java_complex_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "CommandPattern.java"
    )
    test_file.write_text(
        encoding="utf-8",
        data="""
package com.example;

import java.util.Stack;

// Command interface
interface Command {
    void execute();
    void undo();
    String getDescription();
}

// Receiver classes
class Light {
    private boolean isOn;
    private int brightness;

    public void turnOn() {
        isOn = true;
        brightness = 100;
        System.out.println("Light is ON at " + brightness + "% brightness");
    }

    public void turnOff() {
        isOn = false;
        brightness = 0;
        System.out.println("Light is OFF");
    }

    public void setBrightness(int brightness) {
        if (isOn) {
            this.brightness = brightness;
            System.out.println("Light brightness set to " + brightness + "%");
        }
    }

    public boolean isOn() {
        return isOn;
    }

    public int getBrightness() {
        return brightness;
    }
}

class Fan {
    private boolean isRunning;
    private int speed;

    public void turnOn() {
        isRunning = true;
        speed = 1;
        System.out.println("Fan is ON at speed " + speed);
    }

    public void turnOff() {
        isRunning = false;
        speed = 0;
        System.out.println("Fan is OFF");
    }

    public void setSpeed(int speed) {
        if (isRunning && speed >= 1 && speed <= 5) {
            this.speed = speed;
            System.out.println("Fan speed set to " + speed);
        }
    }

    public boolean isRunning() {
        return isRunning;
    }

    public int getSpeed() {
        return speed;
    }
}

// Concrete commands
class LightOnCommand implements Command {
    private Light light;

    public LightOnCommand(Light light) {
        this.light = light;
    }

    @Override
    public void execute() {
        light.turnOn(); // CALLS Light.turnOn()
    }

    @Override
    public void undo() {
        light.turnOff(); // CALLS Light.turnOff()
    }

    @Override
    public String getDescription() {
        return "Turn light ON";
    }
}

class LightOffCommand implements Command {
    private Light light;

    public LightOffCommand(Light light) {
        this.light = light;
    }

    @Override
    public void execute() {
        light.turnOff(); // CALLS Light.turnOff()
    }

    @Override
    public void undo() {
        light.turnOn(); // CALLS Light.turnOn()
    }

    @Override
    public String getDescription() {
        return "Turn light OFF";
    }
}

class FanOnCommand implements Command {
    private Fan fan;

    public FanOnCommand(Fan fan) {
        this.fan = fan;
    }

    @Override
    public void execute() {
        fan.turnOn(); // CALLS Fan.turnOn()
    }

    @Override
    public void undo() {
        fan.turnOff(); // CALLS Fan.turnOff()
    }

    @Override
    public String getDescription() {
        return "Turn fan ON";
    }
}

class FanOffCommand implements Command {
    private Fan fan;

    public FanOffCommand(Fan fan) {
        this.fan = fan;
    }

    @Override
    public void execute() {
        fan.turnOff(); // CALLS Fan.turnOff()
    }

    @Override
    public void undo() {
        fan.turnOn(); // CALLS Fan.turnOn()
    }

    @Override
    public String getDescription() {
        return "Turn fan OFF";
    }
}

// Invoker
class RemoteControl {
    private Command[] commands;
    private Stack<Command> commandHistory;

    public RemoteControl() {
        commands = new Command[4];
        commandHistory = new Stack<>(); // CALLS Stack constructor
    }

    public void setCommand(int slot, Command command) {
        commands[slot] = command;
    }

    public void pressButton(int slot) {
        if (commands[slot] != null) {
            Command command = commands[slot];
            command.execute(); // CALLS Command.execute()
            commandHistory.push(command); // CALLS Stack.push()
            System.out.println("Executed: " + command.getDescription()); // CALLS Command.getDescription()
        }
    }

    public void pressUndo() {
        if (!commandHistory.isEmpty()) { // CALLS Stack.isEmpty()
            Command lastCommand = commandHistory.pop(); // CALLS Stack.pop()
            lastCommand.undo(); // CALLS Command.undo()
            System.out.println("Undone: " + lastCommand.getDescription()); // CALLS Command.getDescription()
        }
    }

    public void showCommands() {
        for (int i = 0; i < commands.length; i++) {
            if (commands[i] != null) {
                System.out.println("Slot " + i + ": " + commands[i].getDescription()); // CALLS Command.getDescription()
            } else {
                System.out.println("Slot " + i + ": Empty");
            }
        }
    }
}

class SmartHome {

    public void demonstrateCommandPattern() {
        // Create receivers
        Light livingRoomLight = new Light(); // CALLS Light constructor
        Light bedroomLight = new Light(); // CALLS Light constructor
        Fan ceilingFan = new Fan(); // CALLS Fan constructor

        // Create commands
        Command livingRoomLightOn = new LightOnCommand(livingRoomLight); // CALLS LightOnCommand constructor
        Command livingRoomLightOff = new LightOffCommand(livingRoomLight); // CALLS LightOffCommand constructor
        Command bedroomLightOn = new LightOnCommand(bedroomLight); // CALLS LightOnCommand constructor
        Command bedroomLightOff = new LightOffCommand(bedroomLight); // CALLS LightOffCommand constructor
        Command fanOn = new FanOnCommand(ceilingFan); // CALLS FanOnCommand constructor
        Command fanOff = new FanOffCommand(ceilingFan); // CALLS FanOffCommand constructor

        // Create invoker
        RemoteControl remote = new RemoteControl(); // CALLS RemoteControl constructor

        // Set up commands
        remote.setCommand(0, livingRoomLightOn); // CALLS RemoteControl.setCommand()
        remote.setCommand(1, livingRoomLightOff); // CALLS RemoteControl.setCommand()
        remote.setCommand(2, fanOn); // CALLS RemoteControl.setCommand()
        remote.setCommand(3, fanOff); // CALLS RemoteControl.setCommand()

        // Show available commands
        remote.showCommands(); // CALLS RemoteControl.showCommands()

        // Execute commands
        remote.pressButton(0); // CALLS RemoteControl.pressButton() - turn living room light on
        remote.pressButton(2); // CALLS RemoteControl.pressButton() - turn fan on
        remote.pressButton(1); // CALLS RemoteControl.pressButton() - turn living room light off

        // Undo commands
        remote.pressUndo(); // CALLS RemoteControl.pressUndo() - undo light off
        remote.pressUndo(); // CALLS RemoteControl.pressUndo() - undo fan on
        remote.pressUndo(); // CALLS RemoteControl.pressUndo() - undo light on
    }
}
""",
    )

    run_updater(java_complex_project, mock_ingestor, skip_if_missing="java")

    implements_relationships = get_relationships(mock_ingestor, "IMPLEMENTS")

    assert len(implements_relationships) > 0, (
        "No interface implementation relationships found for command pattern"
    )

    call_relationships = get_relationships(mock_ingestor, "CALLS")

    assert len(call_relationships) > 0, (
        "No method call relationships found for command pattern"
    )
