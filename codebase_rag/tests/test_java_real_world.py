"""
Real-world Java patterns and enterprise application testing.
Tests Spring Framework annotations, design patterns, DAO patterns,
configuration classes, and enterprise application patterns.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_real_world_project(temp_repo: Path) -> Path:
    """Create a Java project with real-world enterprise patterns."""
    project_path = temp_repo / "java_real_world_test"
    project_path.mkdir()

    # Create standard Java project structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_spring_framework_annotations(
    java_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Spring Framework annotation patterns."""
    test_file = (
        java_real_world_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "SpringExample.java"
    )
    test_file.write_text(
        """
package com.example;

import org.springframework.stereotype.*;
import org.springframework.beans.factory.annotation.*;
import org.springframework.web.bind.annotation.*;
import org.springframework.context.annotation.*;

@Component
public class MessageService {
    @Value("${app.message:Hello World}")
    private String message;

    public String getMessage() {
        return message;
    }
}

@Service
public class UserService {
    @Autowired
    private UserRepository userRepository;

    @Autowired
    private MessageService messageService;

    public User findById(Long id) {
        return userRepository.findById(id);
    }

    public String getWelcomeMessage() {
        return messageService.getMessage();
    }
}

@Repository
public class UserRepository {
    @Autowired
    private DatabaseConnection connection;

    public User findById(Long id) {
        // Database query logic
        return new User(id, "User" + id);
    }

    public void save(User user) {
        // Save logic
    }
}

@RestController
@RequestMapping("/api/users")
public class UserController {
    @Autowired
    private UserService userService;

    @GetMapping("/{id}")
    public User getUser(@PathVariable Long id) {
        return userService.findById(id);
    }

    @PostMapping
    public User createUser(@RequestBody User user) {
        // Create user logic
        return user;
    }

    @GetMapping("/welcome")
    public String welcome() {
        return userService.getWelcomeMessage();
    }
}

@Controller
public class WebController {
    @Autowired
    private UserService userService;

    @RequestMapping("/users/{id}")
    public String showUser(@PathVariable Long id, Model model) {
        User user = userService.findById(id);
        model.addAttribute("user", user);
        return "user-detail";
    }
}

public class User {
    private Long id;
    private String name;

    public User(Long id, String name) {
        this.id = id;
        this.name = name;
    }

    public Long getId() { return id; }
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
}

@Component
public class DatabaseConnection {
    @Value("${db.url}")
    private String url;

    @PostConstruct
    public void init() {
        // Initialize connection
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_real_world_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify Spring components were detected
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_real_world_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.SpringExample.MessageService",
        f"{project_name}.src.main.java.com.example.SpringExample.UserService",
        f"{project_name}.src.main.java.com.example.SpringExample.UserRepository",
        f"{project_name}.src.main.java.com.example.SpringExample.UserController",
        f"{project_name}.src.main.java.com.example.SpringExample.WebController",
        f"{project_name}.src.main.java.com.example.SpringExample.User",
        f"{project_name}.src.main.java.com.example.SpringExample.DatabaseConnection",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing Spring components: {sorted(list(missing_classes))}"
    )


def test_design_patterns_singleton_factory(
    java_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Singleton and Factory design patterns."""
    test_file = (
        java_real_world_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "DesignPatterns.java"
    )
    test_file.write_text(
        """
package com.example;

// Singleton Pattern
public class DatabaseManager {
    private static volatile DatabaseManager instance;
    private String connectionString;

    private DatabaseManager() {
        this.connectionString = "jdbc:mysql://localhost:3306/app";
    }

    public static DatabaseManager getInstance() {
        if (instance == null) {
            synchronized (DatabaseManager.class) {
                if (instance == null) {
                    instance = new DatabaseManager();
                }
            }
        }
        return instance;
    }

    public String getConnectionString() {
        return connectionString;
    }
}

// Factory Pattern
interface Vehicle {
    void start();
    void stop();
    String getType();
}

class Car implements Vehicle {
    private String model;

    public Car(String model) {
        this.model = model;
    }

    @Override
    public void start() {
        System.out.println("Car " + model + " starting engine");
    }

    @Override
    public void stop() {
        System.out.println("Car " + model + " stopping engine");
    }

    @Override
    public String getType() {
        return "Car: " + model;
    }
}

class Motorcycle implements Vehicle {
    private String brand;

    public Motorcycle(String brand) {
        this.brand = brand;
    }

    @Override
    public void start() {
        System.out.println("Motorcycle " + brand + " starting engine");
    }

    @Override
    public void stop() {
        System.out.println("Motorcycle " + brand + " stopping engine");
    }

    @Override
    public String getType() {
        return "Motorcycle: " + brand;
    }
}

public class VehicleFactory {
    public static Vehicle createVehicle(String type, String model) {
        switch (type.toLowerCase()) {
            case "car":
                return new Car(model);
            case "motorcycle":
                return new Motorcycle(model);
            default:
                throw new IllegalArgumentException("Unknown vehicle type: " + type);
        }
    }
}

// Abstract Factory Pattern
abstract class UIFactory {
    public abstract Button createButton();
    public abstract TextField createTextField();
}

interface Button {
    void click();
    void render();
}

interface TextField {
    void setText(String text);
    String getText();
}

class WindowsUIFactory extends UIFactory {
    @Override
    public Button createButton() {
        return new WindowsButton();
    }

    @Override
    public TextField createTextField() {
        return new WindowsTextField();
    }
}

class WindowsButton implements Button {
    @Override
    public void click() {
        System.out.println("Windows button clicked");
    }

    @Override
    public void render() {
        System.out.println("Rendering Windows button");
    }
}

class WindowsTextField implements TextField {
    private String text = "";

    @Override
    public void setText(String text) {
        this.text = text;
    }

    @Override
    public String getText() {
        return text;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_real_world_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    project_name = java_real_world_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.DesignPatterns.DatabaseManager",
        f"{project_name}.src.main.java.com.example.DesignPatterns.Car",
        f"{project_name}.src.main.java.com.example.DesignPatterns.Motorcycle",
        f"{project_name}.src.main.java.com.example.DesignPatterns.VehicleFactory",
        f"{project_name}.src.main.java.com.example.DesignPatterns.UIFactory",
        f"{project_name}.src.main.java.com.example.DesignPatterns.WindowsUIFactory",
        f"{project_name}.src.main.java.com.example.DesignPatterns.WindowsButton",
        f"{project_name}.src.main.java.com.example.DesignPatterns.WindowsTextField",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.DesignPatterns.Vehicle",
        f"{project_name}.src.main.java.com.example.DesignPatterns.Button",
        f"{project_name}.src.main.java.com.example.DesignPatterns.TextField",
    }

    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing design pattern classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing design pattern interfaces: {sorted(list(missing_interfaces))}"
    )


def test_builder_observer_patterns(
    java_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Builder and Observer design patterns."""
    test_file = (
        java_real_world_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "BuilderObserver.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

// Builder Pattern
public class HttpRequest {
    private final String url;
    private final String method;
    private final Map<String, String> headers;
    private final String body;

    private HttpRequest(Builder builder) {
        this.url = builder.url;
        this.method = builder.method;
        this.headers = new HashMap<>(builder.headers);
        this.body = builder.body;
    }

    public static class Builder {
        private String url;
        private String method = "GET";
        private Map<String, String> headers = new HashMap<>();
        private String body;

        public Builder url(String url) {
            this.url = url;
            return this;
        }

        public Builder method(String method) {
            this.method = method;
            return this;
        }

        public Builder header(String key, String value) {
            this.headers.put(key, value);
            return this;
        }

        public Builder body(String body) {
            this.body = body;
            return this;
        }

        public HttpRequest build() {
            if (url == null) {
                throw new IllegalStateException("URL is required");
            }
            return new HttpRequest(this);
        }
    }

    public String getUrl() { return url; }
    public String getMethod() { return method; }
    public Map<String, String> getHeaders() { return headers; }
    public String getBody() { return body; }
}

// Observer Pattern
interface EventListener {
    void onEvent(Event event);
}

class Event {
    private final String type;
    private final Object data;
    private final long timestamp;

    public Event(String type, Object data) {
        this.type = type;
        this.data = data;
        this.timestamp = System.currentTimeMillis();
    }

    public String getType() { return type; }
    public Object getData() { return data; }
    public long getTimestamp() { return timestamp; }
}

class EventPublisher {
    private List<EventListener> listeners = new ArrayList<>();

    public void addListener(EventListener listener) {
        listeners.add(listener);
    }

    public void removeListener(EventListener listener) {
        listeners.remove(listener);
    }

    public void publishEvent(Event event) {
        for (EventListener listener : listeners) {
            listener.onEvent(event);
        }
    }
}

class UserActivityListener implements EventListener {
    @Override
    public void onEvent(Event event) {
        if ("USER_LOGIN".equals(event.getType())) {
            System.out.println("User logged in: " + event.getData());
        }
    }
}

class AuditListener implements EventListener {
    @Override
    public void onEvent(Event event) {
        System.out.println("Audit log: " + event.getType() + " at " + event.getTimestamp());
    }
}

class EmailNotificationListener implements EventListener {
    @Override
    public void onEvent(Event event) {
        if ("USER_REGISTRATION".equals(event.getType())) {
            System.out.println("Sending welcome email to: " + event.getData());
        }
    }
}

// Strategy Pattern
interface PaymentStrategy {
    void pay(double amount);
}

class CreditCardPayment implements PaymentStrategy {
    private String cardNumber;
    private String cvv;

    public CreditCardPayment(String cardNumber, String cvv) {
        this.cardNumber = cardNumber;
        this.cvv = cvv;
    }

    @Override
    public void pay(double amount) {
        System.out.println("Paid $" + amount + " with credit card ending in " +
                          cardNumber.substring(cardNumber.length() - 4));
    }
}

class PayPalPayment implements PaymentStrategy {
    private String email;

    public PayPalPayment(String email) {
        this.email = email;
    }

    @Override
    public void pay(double amount) {
        System.out.println("Paid $" + amount + " with PayPal account: " + email);
    }
}

class PaymentContext {
    private PaymentStrategy strategy;

    public void setPaymentStrategy(PaymentStrategy strategy) {
        this.strategy = strategy;
    }

    public void executePayment(double amount) {
        if (strategy == null) {
            throw new IllegalStateException("Payment strategy not set");
        }
        strategy.pay(amount);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_real_world_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    project_name = java_real_world_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.BuilderObserver.HttpRequest",
        f"{project_name}.src.main.java.com.example.BuilderObserver.HttpRequest.Builder",
        f"{project_name}.src.main.java.com.example.BuilderObserver.Event",
        f"{project_name}.src.main.java.com.example.BuilderObserver.EventPublisher",
        f"{project_name}.src.main.java.com.example.BuilderObserver.UserActivityListener",
        f"{project_name}.src.main.java.com.example.BuilderObserver.AuditListener",
        f"{project_name}.src.main.java.com.example.BuilderObserver.EmailNotificationListener",
        f"{project_name}.src.main.java.com.example.BuilderObserver.CreditCardPayment",
        f"{project_name}.src.main.java.com.example.BuilderObserver.PayPalPayment",
        f"{project_name}.src.main.java.com.example.BuilderObserver.PaymentContext",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.BuilderObserver.EventListener",
        f"{project_name}.src.main.java.com.example.BuilderObserver.PaymentStrategy",
    }

    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing pattern classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing pattern interfaces: {sorted(list(missing_interfaces))}"
    )


def test_dao_repository_patterns(
    java_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test DAO and Repository pattern implementations."""
    test_file = (
        java_real_world_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "DataAccess.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

// Entity class
class Customer {
    private Long id;
    private String name;
    private String email;
    private Date createdAt;

    public Customer() {}

    public Customer(Long id, String name, String email) {
        this.id = id;
        this.name = name;
        this.email = email;
        this.createdAt = new Date();
    }

    // Getters and setters
    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }
    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public String getEmail() { return email; }
    public void setEmail(String email) { this.email = email; }
    public Date getCreatedAt() { return createdAt; }
    public void setCreatedAt(Date createdAt) { this.createdAt = createdAt; }
}

// Generic DAO interface
interface BaseDao<T, ID> {
    T save(T entity);
    T findById(ID id);
    List<T> findAll();
    void update(T entity);
    void delete(ID id);
    boolean exists(ID id);
}

// Customer DAO interface
interface CustomerDao extends BaseDao<Customer, Long> {
    List<Customer> findByName(String name);
    Customer findByEmail(String email);
    List<Customer> findByEmailDomain(String domain);
    List<Customer> findCreatedAfter(Date date);
}

// JPA-style Customer DAO implementation
class JpaCustomerDao implements CustomerDao {
    private Map<Long, Customer> database = new HashMap<>();
    private Long nextId = 1L;

    @Override
    public Customer save(Customer customer) {
        if (customer.getId() == null) {
            customer.setId(nextId++);
        }
        database.put(customer.getId(), customer);
        return customer;
    }

    @Override
    public Customer findById(Long id) {
        return database.get(id);
    }

    @Override
    public List<Customer> findAll() {
        return new ArrayList<>(database.values());
    }

    @Override
    public void update(Customer customer) {
        if (customer.getId() != null && database.containsKey(customer.getId())) {
            database.put(customer.getId(), customer);
        }
    }

    @Override
    public void delete(Long id) {
        database.remove(id);
    }

    @Override
    public boolean exists(Long id) {
        return database.containsKey(id);
    }

    @Override
    public List<Customer> findByName(String name) {
        return database.values().stream()
            .filter(c -> name.equals(c.getName()))
            .collect(Collectors.toList());
    }

    @Override
    public Customer findByEmail(String email) {
        return database.values().stream()
            .filter(c -> email.equals(c.getEmail()))
            .findFirst()
            .orElse(null);
    }

    @Override
    public List<Customer> findByEmailDomain(String domain) {
        return database.values().stream()
            .filter(c -> c.getEmail() != null && c.getEmail().endsWith("@" + domain))
            .collect(Collectors.toList());
    }

    @Override
    public List<Customer> findCreatedAfter(Date date) {
        return database.values().stream()
            .filter(c -> c.getCreatedAt().after(date))
            .collect(Collectors.toList());
    }
}

// Repository pattern with business logic
class CustomerRepository {
    private CustomerDao customerDao;

    public CustomerRepository(CustomerDao customerDao) {
        this.customerDao = customerDao;
    }

    public Customer createCustomer(String name, String email) {
        // Business validation
        if (name == null || name.trim().isEmpty()) {
            throw new IllegalArgumentException("Customer name cannot be empty");
        }

        if (email == null || !email.contains("@")) {
            throw new IllegalArgumentException("Invalid email address");
        }

        // Check for duplicate email
        Customer existing = customerDao.findByEmail(email);
        if (existing != null) {
            throw new IllegalArgumentException("Customer with email already exists");
        }

        Customer customer = new Customer(null, name.trim(), email.toLowerCase());
        return customerDao.save(customer);
    }

    public Customer getCustomerById(Long id) {
        if (id == null) {
            throw new IllegalArgumentException("Customer ID cannot be null");
        }
        return customerDao.findById(id);
    }

    public List<Customer> searchCustomers(String searchTerm) {
        if (searchTerm == null || searchTerm.trim().isEmpty()) {
            return customerDao.findAll();
        }

        List<Customer> results = new ArrayList<>();

        // Search by name
        results.addAll(customerDao.findByName(searchTerm));

        // Search by email if it looks like an email
        if (searchTerm.contains("@")) {
            Customer emailResult = customerDao.findByEmail(searchTerm);
            if (emailResult != null && !results.contains(emailResult)) {
                results.add(emailResult);
            }
        }

        return results;
    }

    public void updateCustomer(Customer customer) {
        if (customer == null || customer.getId() == null) {
            throw new IllegalArgumentException("Customer and ID cannot be null");
        }

        if (!customerDao.exists(customer.getId())) {
            throw new IllegalArgumentException("Customer not found");
        }

        customerDao.update(customer);
    }

    public void deleteCustomer(Long id) {
        if (id == null) {
            throw new IllegalArgumentException("Customer ID cannot be null");
        }

        if (!customerDao.exists(id)) {
            throw new IllegalArgumentException("Customer not found");
        }

        customerDao.delete(id);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_real_world_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    project_name = java_real_world_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.DataAccess.Customer",
        f"{project_name}.src.main.java.com.example.DataAccess.JpaCustomerDao",
        f"{project_name}.src.main.java.com.example.DataAccess.CustomerRepository",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.DataAccess.BaseDao",
        f"{project_name}.src.main.java.com.example.DataAccess.CustomerDao",
    }

    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, f"Missing DAO classes: {sorted(list(missing_classes))}"
    assert not missing_interfaces, (
        f"Missing DAO interfaces: {sorted(list(missing_interfaces))}"
    )


def test_configuration_classes(
    java_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Spring configuration classes and bean definitions."""
    test_file = (
        java_real_world_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "AppConfiguration.java"
    )
    test_file.write_text(
        """
package com.example;

import org.springframework.context.annotation.*;
import org.springframework.beans.factory.annotation.*;

@Configuration
@EnableAutoConfiguration
@ComponentScan(basePackages = "com.example")
public class AppConfiguration {

    @Bean
    @Primary
    public DataSource primaryDataSource() {
        HikariDataSource dataSource = new HikariDataSource();
        dataSource.setJdbcUrl("jdbc:mysql://localhost:3306/primary");
        dataSource.setUsername("admin");
        dataSource.setPassword("password");
        dataSource.setMaximumPoolSize(20);
        return dataSource;
    }

    @Bean("secondaryDb")
    public DataSource secondaryDataSource() {
        HikariDataSource dataSource = new HikariDataSource();
        dataSource.setJdbcUrl("jdbc:mysql://localhost:3306/secondary");
        dataSource.setUsername("readonly");
        dataSource.setPassword("readonly");
        dataSource.setMaximumPoolSize(10);
        return dataSource;
    }

    @Bean
    @ConditionalOnProperty(name = "cache.enabled", havingValue = "true")
    public CacheManager cacheManager() {
        return new ConcurrentMapCacheManager("users", "products");
    }

    @Bean
    @Profile("development")
    public EmailService developmentEmailService() {
        return new ConsoleEmailService();
    }

    @Bean
    @Profile("production")
    public EmailService productionEmailService() {
        return new SmtpEmailService();
    }
}

interface DataSource {
    Connection getConnection();
}

class HikariDataSource implements DataSource {
    private String jdbcUrl;
    private String username;
    private String password;
    private int maximumPoolSize;

    public void setJdbcUrl(String jdbcUrl) { this.jdbcUrl = jdbcUrl; }
    public void setUsername(String username) { this.username = username; }
    public void setPassword(String password) { this.password = password; }
    public void setMaximumPoolSize(int size) { this.maximumPoolSize = size; }

    @Override
    public Connection getConnection() {
        return new MockConnection(jdbcUrl);
    }
}

interface Connection {
    void execute(String sql);
}

class MockConnection implements Connection {
    private String url;

    public MockConnection(String url) {
        this.url = url;
    }

    @Override
    public void execute(String sql) {
        System.out.println("Executing on " + url + ": " + sql);
    }
}

interface CacheManager {
    Cache getCache(String name);
}

class ConcurrentMapCacheManager implements CacheManager {
    private String[] cacheNames;

    public ConcurrentMapCacheManager(String... cacheNames) {
        this.cacheNames = cacheNames;
    }

    @Override
    public Cache getCache(String name) {
        return new MapCache(name);
    }
}

interface Cache {
    void put(Object key, Object value);
    Object get(Object key);
}

class MapCache implements Cache {
    private String name;
    private Map<Object, Object> store = new HashMap<>();

    public MapCache(String name) {
        this.name = name;
    }

    @Override
    public void put(Object key, Object value) {
        store.put(key, value);
    }

    @Override
    public Object get(Object key) {
        return store.get(key);
    }
}

interface EmailService {
    void sendEmail(String to, String subject, String body);
}

class ConsoleEmailService implements EmailService {
    @Override
    public void sendEmail(String to, String subject, String body) {
        System.out.println("EMAIL TO: " + to);
        System.out.println("SUBJECT: " + subject);
        System.out.println("BODY: " + body);
    }
}

class SmtpEmailService implements EmailService {
    @Override
    public void sendEmail(String to, String subject, String body) {
        // SMTP implementation
        System.out.println("Sending via SMTP to: " + to);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_real_world_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    project_name = java_real_world_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.AppConfiguration.AppConfiguration",
        f"{project_name}.src.main.java.com.example.AppConfiguration.HikariDataSource",
        f"{project_name}.src.main.java.com.example.AppConfiguration.MockConnection",
        f"{project_name}.src.main.java.com.example.AppConfiguration.ConcurrentMapCacheManager",
        f"{project_name}.src.main.java.com.example.AppConfiguration.MapCache",
        f"{project_name}.src.main.java.com.example.AppConfiguration.ConsoleEmailService",
        f"{project_name}.src.main.java.com.example.AppConfiguration.SmtpEmailService",
    }

    expected_interfaces = {
        f"{project_name}.src.main.java.com.example.AppConfiguration.DataSource",
        f"{project_name}.src.main.java.com.example.AppConfiguration.Connection",
        f"{project_name}.src.main.java.com.example.AppConfiguration.CacheManager",
        f"{project_name}.src.main.java.com.example.AppConfiguration.Cache",
        f"{project_name}.src.main.java.com.example.AppConfiguration.EmailService",
    }

    missing_classes = expected_classes - created_classes
    missing_interfaces = expected_interfaces - created_interfaces

    assert not missing_classes, (
        f"Missing configuration classes: {sorted(list(missing_classes))}"
    )
    assert not missing_interfaces, (
        f"Missing configuration interfaces: {sorted(list(missing_interfaces))}"
    )


def test_utility_helper_classes(
    java_real_world_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test utility and helper class patterns."""
    test_file = (
        java_real_world_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "Utilities.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.text.*;

// String utilities
public final class StringUtils {
    private StringUtils() {
        // Prevent instantiation
    }

    public static boolean isEmpty(String str) {
        return str == null || str.trim().isEmpty();
    }

    public static boolean isNotEmpty(String str) {
        return !isEmpty(str);
    }

    public static String capitalize(String str) {
        if (isEmpty(str)) {
            return str;
        }
        return str.substring(0, 1).toUpperCase() + str.substring(1).toLowerCase();
    }

    public static String join(Collection<String> strings, String delimiter) {
        if (strings == null || strings.isEmpty()) {
            return "";
        }
        return String.join(delimiter, strings);
    }

    public static List<String> split(String str, String delimiter) {
        if (isEmpty(str)) {
            return new ArrayList<>();
        }
        return Arrays.asList(str.split(delimiter));
    }
}

// Date utilities
public final class DateUtils {
    private static final SimpleDateFormat ISO_FORMAT = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'");
    private static final SimpleDateFormat DISPLAY_FORMAT = new SimpleDateFormat("MMM dd, yyyy");

    private DateUtils() {
        // Prevent instantiation
    }

    public static String formatForDisplay(Date date) {
        if (date == null) {
            return "";
        }
        return DISPLAY_FORMAT.format(date);
    }

    public static String formatISO(Date date) {
        if (date == null) {
            return "";
        }
        return ISO_FORMAT.format(date);
    }

    public static Date parseISO(String dateString) {
        if (StringUtils.isEmpty(dateString)) {
            return null;
        }
        try {
            return ISO_FORMAT.parse(dateString);
        } catch (ParseException e) {
            throw new IllegalArgumentException("Invalid date format: " + dateString, e);
        }
    }

    public static boolean isWeekend(Date date) {
        if (date == null) {
            return false;
        }
        Calendar cal = Calendar.getInstance();
        cal.setTime(date);
        int dayOfWeek = cal.get(Calendar.DAY_OF_WEEK);
        return dayOfWeek == Calendar.SATURDAY || dayOfWeek == Calendar.SUNDAY;
    }

    public static Date addDays(Date date, int days) {
        if (date == null) {
            return null;
        }
        Calendar cal = Calendar.getInstance();
        cal.setTime(date);
        cal.add(Calendar.DAY_OF_MONTH, days);
        return cal.getTime();
    }
}

// Collection utilities
public final class CollectionUtils {
    private CollectionUtils() {
        // Prevent instantiation
    }

    public static <T> boolean isEmpty(Collection<T> collection) {
        return collection == null || collection.isEmpty();
    }

    public static <T> boolean isNotEmpty(Collection<T> collection) {
        return !isEmpty(collection);
    }

    public static <T> List<T> safe(List<T> list) {
        return list != null ? list : new ArrayList<>();
    }

    public static <K, V> Map<K, V> safe(Map<K, V> map) {
        return map != null ? map : new HashMap<>();
    }

    public static <T> List<List<T>> partition(List<T> list, int size) {
        if (isEmpty(list) || size <= 0) {
            return new ArrayList<>();
        }

        List<List<T>> partitions = new ArrayList<>();
        for (int i = 0; i < list.size(); i += size) {
            partitions.add(new ArrayList<>(
                list.subList(i, Math.min(i + size, list.size()))
            ));
        }
        return partitions;
    }

    public static <T> T getFirst(Collection<T> collection) {
        if (isEmpty(collection)) {
            return null;
        }
        return collection.iterator().next();
    }

    public static <T> T getLast(List<T> list) {
        if (isEmpty(list)) {
            return null;
        }
        return list.get(list.size() - 1);
    }
}

// Validation utilities
public final class ValidationUtils {
    private static final String EMAIL_REGEX = "^[A-Za-z0-9+_.-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$";

    private ValidationUtils() {
        // Prevent instantiation
    }

    public static void requireNonNull(Object obj, String message) {
        if (obj == null) {
            throw new IllegalArgumentException(message);
        }
    }

    public static void requireNonEmpty(String str, String message) {
        if (StringUtils.isEmpty(str)) {
            throw new IllegalArgumentException(message);
        }
    }

    public static void requireNonEmpty(Collection<?> collection, String message) {
        if (CollectionUtils.isEmpty(collection)) {
            throw new IllegalArgumentException(message);
        }
    }

    public static boolean isValidEmail(String email) {
        return StringUtils.isNotEmpty(email) && email.matches(EMAIL_REGEX);
    }

    public static void requireValidEmail(String email, String message) {
        if (!isValidEmail(email)) {
            throw new IllegalArgumentException(message);
        }
    }

    public static boolean isPositive(Number number) {
        return number != null && number.doubleValue() > 0;
    }

    public static void requirePositive(Number number, String message) {
        if (!isPositive(number)) {
            throw new IllegalArgumentException(message);
        }
    }
}

// Math utilities
public final class MathUtils {
    private MathUtils() {
        // Prevent instantiation
    }

    public static double round(double value, int places) {
        if (places < 0) throw new IllegalArgumentException();

        long factor = (long) Math.pow(10, places);
        value = value * factor;
        long tmp = Math.round(value);
        return (double) tmp / factor;
    }

    public static boolean isEven(int number) {
        return number % 2 == 0;
    }

    public static boolean isOdd(int number) {
        return !isEven(number);
    }

    public static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }

    public static double clamp(double value, double min, double max) {
        return Math.max(min, Math.min(max, value));
    }

    public static boolean inRange(int value, int min, int max) {
        return value >= min && value <= max;
    }

    public static boolean inRange(double value, double min, double max) {
        return value >= min && value <= max;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_real_world_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_real_world_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.Utilities.StringUtils",
        f"{project_name}.src.main.java.com.example.Utilities.DateUtils",
        f"{project_name}.src.main.java.com.example.Utilities.CollectionUtils",
        f"{project_name}.src.main.java.com.example.Utilities.ValidationUtils",
        f"{project_name}.src.main.java.com.example.Utilities.MathUtils",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing utility classes: {sorted(list(missing_classes))}"
    )
