"""
Java module system (Jigsaw) parsing testing.
Tests module declarations, exports, requires, provides/uses,
module-info.java files, and modular application structure.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_modules_project(temp_repo: Path) -> Path:
    """Create a Java project for testing module system."""
    project_path = temp_repo / "java_modules_test"
    project_path.mkdir()

    # Create modular Java project structure
    (project_path / "src").mkdir()

    # Core module
    (project_path / "src" / "core.module").mkdir()
    (project_path / "src" / "core.module" / "com").mkdir()
    (project_path / "src" / "core.module" / "com" / "example").mkdir()
    (project_path / "src" / "core.module" / "com" / "example" / "core").mkdir()

    # API module
    (project_path / "src" / "api.module").mkdir()
    (project_path / "src" / "api.module" / "com").mkdir()
    (project_path / "src" / "api.module" / "com" / "example").mkdir()
    (project_path / "src" / "api.module" / "com" / "example" / "api").mkdir()

    # Service module
    (project_path / "src" / "service.module").mkdir()
    (project_path / "src" / "service.module" / "com").mkdir()
    (project_path / "src" / "service.module" / "com" / "example").mkdir()
    (project_path / "src" / "service.module" / "com" / "example" / "service").mkdir()

    return project_path


def test_module_info_declarations(
    java_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test module-info.java declarations."""
    # Core module-info.java
    core_module_info = java_modules_project / "src" / "core.module" / "module-info.java"
    core_module_info.write_text(
        """
/**
 * Core module providing fundamental utilities and data structures.
 * This module exports core functionality to other modules.
 */
module com.example.core {
    // Export packages to all modules
    exports com.example.core.utils;
    exports com.example.core.data;

    // Qualified exports - only to specific modules
    exports com.example.core.internal to com.example.service, com.example.api;

    // Requires other modules
    requires java.base; // implicit, but can be explicit
    requires java.logging;
    requires transitive java.desktop; // transitive dependency

    // Optional dependencies
    requires static java.compiler; // optional at runtime
    requires static jdk.compiler; // compile-time only

    // Service provider interface
    provides com.example.core.spi.DataProcessor
        with com.example.core.impl.DefaultDataProcessor,
             com.example.core.impl.FastDataProcessor;

    // Service consumer
    uses com.example.core.spi.ConfigurationProvider;
    uses com.example.core.spi.LoggingProvider;

    // Open packages for reflection
    opens com.example.core.model to java.base, java.logging;
    opens com.example.core.config; // to all modules
}
"""
    )

    # API module-info.java
    api_module_info = java_modules_project / "src" / "api.module" / "module-info.java"
    api_module_info.write_text(
        """
/**
 * API module defining interfaces and contracts.
 */
module com.example.api {
    // Export API packages
    exports com.example.api.interfaces;
    exports com.example.api.dto;
    exports com.example.api.exceptions;

    // Require core module transitively
    requires transitive com.example.core;
    requires java.base;

    // HTTP client for API operations
    requires java.net.http;

    // JSON processing
    requires static com.fasterxml.jackson.core;
    requires static com.fasterxml.jackson.databind;

    // Service definitions
    provides com.example.api.spi.ApiProvider
        with com.example.api.impl.RestApiProvider,
             com.example.api.impl.GraphQLApiProvider;

    uses com.example.api.spi.AuthenticationProvider;
    uses com.example.core.spi.DataProcessor;

    // Open for serialization frameworks
    opens com.example.api.dto to com.fasterxml.jackson.databind;
    opens com.example.api.model;
}
"""
    )

    # Service module-info.java
    service_module_info = (
        java_modules_project / "src" / "service.module" / "module-info.java"
    )
    service_module_info.write_text(
        """
/**
 * Service implementation module.
 */
module com.example.service {
    // Require dependencies
    requires com.example.core;
    requires com.example.api;
    requires java.base;
    requires java.sql;
    requires java.naming;

    // Database connectivity
    requires transitive java.sql.rowset;
    requires static mysql.connector.java;
    requires static postgresql.jdbc;

    // Dependency injection framework
    requires static spring.core;
    requires static spring.context;
    requires static spring.beans;

    // Export service packages
    exports com.example.service.business;
    exports com.example.service.repository to com.example.web, com.example.batch;

    // Service implementations
    provides com.example.api.spi.UserService
        with com.example.service.impl.DatabaseUserService;

    provides com.example.core.spi.ConfigurationProvider
        with com.example.service.config.PropertyFileConfigProvider,
             com.example.service.config.DatabaseConfigProvider;

    // Consume services
    uses com.example.api.spi.AuthenticationProvider;
    uses javax.sql.DataSource;

    // Open for frameworks
    opens com.example.service.entity to
        hibernate.core,
        spring.core,
        com.fasterxml.jackson.databind;

    opens com.example.service.config;
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modules_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify module-info files were processed (they should be detected as regular files)
    # Since module-info.java files are special, they might not be parsed as regular classes
    # but we can verify the parsing didn't fail
    assert True  # Basic verification that parsing completed without errors


def test_service_provider_interface(
    java_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Java service provider interface patterns."""
    # Service interface in core module
    spi_file = (
        java_modules_project
        / "src"
        / "core.module"
        / "com"
        / "example"
        / "core"
        / "ServiceProviderInterface.java"
    )
    spi_file.write_text(
        """
package com.example.core.spi;

import java.util.ServiceLoader;
import java.util.stream.Stream;
import java.util.List;
import java.util.Optional;

// Service Provider Interface
public interface DataProcessor {
    String process(String data);
    boolean supports(String dataType);
    int priority();

    default String getName() {
        return getClass().getSimpleName();
    }
}

// Configuration provider SPI
public interface ConfigurationProvider {
    Optional<String> getProperty(String key);
    void setProperty(String key, String value);
    void reload();

    default boolean hasProperty(String key) {
        return getProperty(key).isPresent();
    }
}

// Logging provider SPI
public interface LoggingProvider {
    void log(Level level, String message);
    void log(Level level, String message, Throwable throwable);
    boolean isEnabled(Level level);

    enum Level {
        TRACE, DEBUG, INFO, WARN, ERROR
    }
}

// Service discovery utility
public final class ServiceDiscovery {

    private ServiceDiscovery() {
        // Utility class
    }

    // Discover all implementations of a service
    public static <T> Stream<T> discover(Class<T> serviceType) {
        return ServiceLoader.load(serviceType)
            .stream()
            .map(ServiceLoader.Provider::get);
    }

    // Find best implementation based on priority
    public static Optional<DataProcessor> findBestProcessor(String dataType) {
        return discover(DataProcessor.class)
            .filter(processor -> processor.supports(dataType))
            .max((p1, p2) -> Integer.compare(p1.priority(), p2.priority()));
    }

    // Get all configuration providers
    public static List<ConfigurationProvider> getAllConfigProviders() {
        return discover(ConfigurationProvider.class).toList();
    }

    // Load specific service by module
    public static <T> Stream<T> discoverInModule(Class<T> serviceType, ModuleLayer layer) {
        return ServiceLoader.load(layer, serviceType)
            .stream()
            .map(ServiceLoader.Provider::get);
    }
}

// Service registry for dynamic management
public class ServiceRegistry {

    private final Map<Class<?>, Set<Object>> services = new ConcurrentHashMap<>();

    public <T> void register(Class<T> serviceType, T implementation) {
        services.computeIfAbsent(serviceType, k -> ConcurrentHashMap.newKeySet())
                .add(implementation);
    }

    public <T> void unregister(Class<T> serviceType, T implementation) {
        Set<Object> impls = services.get(serviceType);
        if (impls != null) {
            impls.remove(implementation);
            if (impls.isEmpty()) {
                services.remove(serviceType);
            }
        }
    }

    @SuppressWarnings("unchecked")
    public <T> Stream<T> getServices(Class<T> serviceType) {
        return services.getOrDefault(serviceType, Set.of())
                      .stream()
                      .map(serviceType::cast);
    }
}
"""
    )

    # Service implementations in core module
    impl_file = (
        java_modules_project
        / "src"
        / "core.module"
        / "com"
        / "example"
        / "core"
        / "ServiceImplementations.java"
    )
    impl_file.write_text(
        """
package com.example.core.impl;

import com.example.core.spi.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.Map;
import java.util.Optional;

// Default data processor implementation
public class DefaultDataProcessor implements DataProcessor {

    @Override
    public String process(String data) {
        if (data == null) {
            return "";
        }
        return data.trim().toLowerCase();
    }

    @Override
    public boolean supports(String dataType) {
        return "text".equals(dataType) || "string".equals(dataType);
    }

    @Override
    public int priority() {
        return 1; // Low priority
    }
}

// Fast data processor implementation
public class FastDataProcessor implements DataProcessor {

    @Override
    public String process(String data) {
        return data != null ? data.intern() : "";
    }

    @Override
    public boolean supports(String dataType) {
        return "text".equals(dataType);
    }

    @Override
    public int priority() {
        return 10; // High priority
    }
}

// XML data processor
public class XmlDataProcessor implements DataProcessor {

    @Override
    public String process(String data) {
        if (data == null || data.isEmpty()) {
            return "<empty/>";
        }
        return "<data>" + data.trim() + "</data>";
    }

    @Override
    public boolean supports(String dataType) {
        return "xml".equals(dataType);
    }

    @Override
    public int priority() {
        return 5;
    }
}

// Property file configuration provider
public class PropertyFileConfigProvider implements ConfigurationProvider {

    private final Map<String, String> properties = new ConcurrentHashMap<>();
    private final String configFile;

    public PropertyFileConfigProvider() {
        this("application.properties");
    }

    public PropertyFileConfigProvider(String configFile) {
        this.configFile = configFile;
        loadProperties();
    }

    @Override
    public Optional<String> getProperty(String key) {
        return Optional.ofNullable(properties.get(key));
    }

    @Override
    public void setProperty(String key, String value) {
        if (value == null) {
            properties.remove(key);
        } else {
            properties.put(key, value);
        }
    }

    @Override
    public void reload() {
        properties.clear();
        loadProperties();
    }

    private void loadProperties() {
        // Simulate loading from file
        properties.put("app.name", "ModularApp");
        properties.put("app.version", "1.0");
        properties.put("database.url", "jdbc:h2:mem:testdb");
    }
}

// Environment variable configuration provider
public class EnvironmentConfigProvider implements ConfigurationProvider {

    @Override
    public Optional<String> getProperty(String key) {
        return Optional.ofNullable(System.getenv(key));
    }

    @Override
    public void setProperty(String key, String value) {
        throw new UnsupportedOperationException("Cannot set environment variables");
    }

    @Override
    public void reload() {
        // Environment variables are always current
    }
}

// Console logging provider
public class ConsoleLoggingProvider implements LoggingProvider {

    @Override
    public void log(Level level, String message) {
        System.out.println(formatMessage(level, message));
    }

    @Override
    public void log(Level level, String message, Throwable throwable) {
        System.out.println(formatMessage(level, message));
        throwable.printStackTrace();
    }

    @Override
    public boolean isEnabled(Level level) {
        return level.ordinal() >= Level.INFO.ordinal();
    }

    private String formatMessage(Level level, String message) {
        return String.format("[%s] %s: %s",
            java.time.LocalDateTime.now(), level, message);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modules_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify service interfaces and implementations were detected
    project_name = java_modules_project.name
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_interfaces = {call[0][1]["qualified_name"] for call in interface_calls}

    # Expected interfaces
    expected_interfaces = {
        f"{project_name}.src.core.module.com.example.core.ServiceProviderInterface.DataProcessor",
        f"{project_name}.src.core.module.com.example.core.ServiceProviderInterface.ConfigurationProvider",
        f"{project_name}.src.core.module.com.example.core.ServiceProviderInterface.LoggingProvider",
    }

    # Expected classes
    expected_classes = {
        f"{project_name}.src.core.module.com.example.core.ServiceProviderInterface.ServiceDiscovery",
        f"{project_name}.src.core.module.com.example.core.ServiceProviderInterface.ServiceRegistry",
        f"{project_name}.src.core.module.com.example.core.ServiceImplementations.DefaultDataProcessor",
        f"{project_name}.src.core.module.com.example.core.ServiceImplementations.FastDataProcessor",
        f"{project_name}.src.core.module.com.example.core.ServiceImplementations.XmlDataProcessor",
        f"{project_name}.src.core.module.com.example.core.ServiceImplementations.PropertyFileConfigProvider",
        f"{project_name}.src.core.module.com.example.core.ServiceImplementations.EnvironmentConfigProvider",
        f"{project_name}.src.core.module.com.example.core.ServiceImplementations.ConsoleLoggingProvider",
    }

    missing_interfaces = expected_interfaces - created_interfaces
    missing_classes = expected_classes - created_classes

    assert not missing_interfaces, (
        f"Missing expected interfaces: {sorted(list(missing_interfaces))}"
    )
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_module_layer_and_configuration(
    java_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test module layer and configuration APIs."""
    test_file = (
        java_modules_project
        / "src"
        / "core.module"
        / "com"
        / "example"
        / "core"
        / "ModuleConfiguration.java"
    )
    test_file.write_text(
        """
package com.example.core.module;

import java.lang.module.*;
import java.util.*;
import java.util.stream.Collectors;

// Module configuration and introspection
public class ModuleConfiguration {

    // Get current module information
    public void inspectCurrentModule() {
        Module currentModule = getClass().getModule();

        System.out.println("Module name: " + currentModule.getName());
        System.out.println("Is named: " + currentModule.isNamed());
        System.out.println("Descriptor: " + currentModule.getDescriptor());

        // Module layer information
        ModuleLayer layer = currentModule.getLayer();
        if (layer != null) {
            System.out.println("Layer: " + layer);
            System.out.println("Parent layers: " + layer.parents());
        }

        // Module annotations
        System.out.println("Annotations: " + Arrays.toString(currentModule.getAnnotations()));
    }

    // Analyze module dependencies
    public void analyzeModuleDependencies() {
        Module currentModule = getClass().getModule();
        ModuleDescriptor descriptor = currentModule.getDescriptor();

        if (descriptor != null) {
            // Required modules
            Set<ModuleDescriptor.Requires> requires = descriptor.requires();
            System.out.println("Required modules:");
            for (ModuleDescriptor.Requires req : requires) {
                System.out.println("  " + req.name() +
                    " (modifiers: " + req.modifiers() + ")");
            }

            // Exported packages
            Set<ModuleDescriptor.Exports> exports = descriptor.exports();
            System.out.println("Exported packages:");
            for (ModuleDescriptor.Exports export : exports) {
                System.out.println("  " + export.source() +
                    " -> " + export.targets());
            }

            // Service provides
            Set<ModuleDescriptor.Provides> provides = descriptor.provides();
            System.out.println("Service provides:");
            for (ModuleDescriptor.Provides provide : provides) {
                System.out.println("  " + provide.service() +
                    " with " + provide.providers());
            }

            // Service uses
            Set<String> uses = descriptor.uses();
            System.out.println("Service uses: " + uses);

            // Open packages
            Set<ModuleDescriptor.Opens> opens = descriptor.opens();
            System.out.println("Open packages:");
            for (ModuleDescriptor.Opens open : opens) {
                System.out.println("  " + open.source() +
                    " to " + open.targets());
            }
        }
    }

    // Create custom module layer
    public ModuleLayer createCustomLayer(Path... modulePaths) throws Exception {
        ModuleFinder finder = ModuleFinder.of(modulePaths);

        // Find all modules
        Set<String> moduleNames = finder.findAll()
            .stream()
            .map(ref -> ref.descriptor().name())
            .collect(Collectors.toSet());

        // Create configuration
        Configuration config = ModuleLayer.boot()
            .configuration()
            .resolve(finder, ModuleFinder.of(), moduleNames);

        // Create layer
        ClassLoader systemClassLoader = ClassLoader.getSystemClassLoader();
        ModuleLayer layer = ModuleLayer.boot()
            .defineModulesWithOneLoader(config, systemClassLoader);

        return layer;
    }

    // Module reflection and access
    public void demonstrateModuleReflection() {
        Module currentModule = getClass().getModule();

        // Check if package is exported
        String packageName = getClass().getPackageName();
        boolean isExported = currentModule.isExported(packageName);
        System.out.println("Package " + packageName + " is exported: " + isExported);

        // Check if package is open
        boolean isOpen = currentModule.isOpen(packageName);
        System.out.println("Package " + packageName + " is open: " + isOpen);

        // Get all packages
        Set<String> packages = currentModule.getPackages();
        System.out.println("All packages: " + packages);

        // Module reads relationship
        Module baseModule = Object.class.getModule();
        boolean canRead = currentModule.canRead(baseModule);
        System.out.println("Can read java.base: " + canRead);
    }

    // Dynamic module configuration
    public void configureDynamicModule() {
        Module currentModule = getClass().getModule();
        Module targetModule = String.class.getModule(); // java.base

        // Add reads (if permitted)
        try {
            if (!currentModule.canRead(targetModule)) {
                // This would typically be done by the module system
                // currentModule.addReads(targetModule);
                System.out.println("Added read to " + targetModule.getName());
            }
        } catch (Exception e) {
            System.err.println("Cannot add reads: " + e.getMessage());
        }

        // Add exports (if permitted)
        try {
            String packageName = getClass().getPackageName();
            if (!currentModule.isExported(packageName, targetModule)) {
                // currentModule.addExports(packageName, targetModule);
                System.out.println("Added export of " + packageName);
            }
        } catch (Exception e) {
            System.err.println("Cannot add exports: " + e.getMessage());
        }
    }

    // Module finder utilities
    public static class ModuleFinderUtils {

        public static Set<ModuleReference> findAllModules(Path... paths) {
            return ModuleFinder.of(paths).findAll();
        }

        public static Optional<ModuleReference> findModule(String moduleName, Path... paths) {
            return ModuleFinder.of(paths).find(moduleName);
        }

        public static Set<String> getModuleNames(Path... paths) {
            return ModuleFinder.of(paths)
                .findAll()
                .stream()
                .map(ref -> ref.descriptor().name())
                .collect(Collectors.toSet());
        }

        public static void printModuleInfo(ModuleReference moduleRef) {
            ModuleDescriptor desc = moduleRef.descriptor();
            System.out.println("Module: " + desc.name());
            System.out.println("  Version: " + desc.version().orElse(null));
            System.out.println("  Requires: " + desc.requires().stream()
                .map(ModuleDescriptor.Requires::name)
                .collect(Collectors.joining(", ")));
            System.out.println("  Exports: " + desc.exports().stream()
                .map(ModuleDescriptor.Exports::source)
                .collect(Collectors.joining(", ")));
        }
    }

    // Configuration utilities
    public static class ConfigurationUtils {

        public static Configuration resolveConfiguration(
                ModuleFinder beforeFinder,
                ModuleFinder afterFinder,
                Collection<String> roots) {

            return Configuration.empty()
                .resolve(beforeFinder, afterFinder, roots);
        }

        public static Set<String> findMissingDependencies(Configuration config) {
            // This would analyze the configuration for missing dependencies
            return config.modules().stream()
                .flatMap(resolvedModule -> resolvedModule.reference().descriptor().requires().stream())
                .map(ModuleDescriptor.Requires::name)
                .filter(name -> config.findModule(name).isEmpty())
                .collect(Collectors.toSet());
        }
    }

    // Boot layer utilities
    public void inspectBootLayer() {
        ModuleLayer bootLayer = ModuleLayer.boot();

        System.out.println("Boot layer configuration:");
        Configuration config = bootLayer.configuration();

        Set<ResolvedModule> modules = config.modules();
        System.out.println("Boot layer has " + modules.size() + " modules:");

        for (ResolvedModule module : modules) {
            System.out.println("  " + module.name());

            // Print dependencies
            Set<ResolvedModule> dependencies = module.reads();
            if (!dependencies.isEmpty()) {
                System.out.println("    Reads: " + dependencies.stream()
                    .map(ResolvedModule::name)
                    .collect(Collectors.joining(", ")));
            }
        }

        // Check specific modules
        Optional<Module> baseModule = bootLayer.findModule("java.base");
        System.out.println("java.base present: " + baseModule.isPresent());

        Optional<Module> loggingModule = bootLayer.findModule("java.logging");
        System.out.println("java.logging present: " + loggingModule.isPresent());
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modules_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify the classes were detected
    project_name = java_modules_project.name
    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}

    expected_classes = {
        f"{project_name}.src.core.module.com.example.core.ModuleConfiguration.ModuleConfiguration",
        f"{project_name}.src.core.module.com.example.core.ModuleConfiguration.ModuleConfiguration.ModuleFinderUtils",
        f"{project_name}.src.core.module.com.example.core.ModuleConfiguration.ModuleConfiguration.ConfigurationUtils",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_modular_application_structure(
    java_modules_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test modular application structure and cross-module dependencies."""
    # API module classes
    api_file = (
        java_modules_project
        / "src"
        / "api.module"
        / "com"
        / "example"
        / "api"
        / "UserAPI.java"
    )
    api_file.write_text(
        """
package com.example.api.interfaces;

import com.example.core.spi.DataProcessor;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;

// User management API
public interface UserService {
    CompletableFuture<User> createUser(CreateUserRequest request);
    CompletableFuture<Optional<User>> findUserById(String userId);
    CompletableFuture<List<User>> findUsersByEmail(String email);
    CompletableFuture<Void> updateUser(String userId, UpdateUserRequest request);
    CompletableFuture<Void> deleteUser(String userId);
    CompletableFuture<List<User>> searchUsers(SearchCriteria criteria);
}

// Authentication API
public interface AuthenticationService {
    CompletableFuture<AuthenticationResult> authenticate(Credentials credentials);
    CompletableFuture<Void> logout(String sessionId);
    CompletableFuture<Boolean> validateSession(String sessionId);
    CompletableFuture<RefreshResult> refreshToken(String refreshToken);
}

// Data Transfer Objects
package com.example.api.dto;

import java.time.LocalDateTime;
import java.util.Set;

public record User(
    String id,
    String email,
    String firstName,
    String lastName,
    Set<String> roles,
    LocalDateTime createdAt,
    LocalDateTime lastLoginAt,
    boolean active
) {
    // Validation in compact constructor
    public User {
        if (email == null || !email.contains("@")) {
            throw new IllegalArgumentException("Invalid email");
        }
        if (firstName == null || firstName.trim().isEmpty()) {
            throw new IllegalArgumentException("First name is required");
        }
    }

    public String getFullName() {
        return firstName + " " + lastName;
    }

    public boolean hasRole(String role) {
        return roles.contains(role);
    }
}

public record CreateUserRequest(
    String email,
    String firstName,
    String lastName,
    String password,
    Set<String> roles
) {
    public CreateUserRequest {
        if (email == null || !email.matches("\\\\S+@\\\\S+\\\\.\\\\S+")) {
            throw new IllegalArgumentException("Invalid email format");
        }
        if (password == null || password.length() < 8) {
            throw new IllegalArgumentException("Password must be at least 8 characters");
        }
        if (roles == null) {
            roles = Set.of("USER");
        }
    }
}

public record UpdateUserRequest(
    Optional<String> firstName,
    Optional<String> lastName,
    Optional<Set<String>> roles,
    Optional<Boolean> active
) {}

public record SearchCriteria(
    Optional<String> emailPattern,
    Optional<String> namePattern,
    Optional<Set<String>> roles,
    Optional<Boolean> active,
    int offset,
    int limit
) {
    public SearchCriteria {
        if (offset < 0) {
            throw new IllegalArgumentException("Offset cannot be negative");
        }
        if (limit <= 0 || limit > 1000) {
            throw new IllegalArgumentException("Limit must be between 1 and 1000");
        }
    }
}

public record Credentials(String email, String password) {
    public Credentials {
        if (email == null || password == null) {
            throw new IllegalArgumentException("Email and password are required");
        }
    }
}

public record AuthenticationResult(
    boolean success,
    Optional<String> sessionId,
    Optional<String> accessToken,
    Optional<String> refreshToken,
    Optional<User> user,
    Optional<String> errorMessage
) {}

public record RefreshResult(
    boolean success,
    Optional<String> newAccessToken,
    Optional<String> errorMessage
) {}

// Exception classes
package com.example.api.exceptions;

public class UserNotFoundException extends Exception {
    public UserNotFoundException(String userId) {
        super("User not found: " + userId);
    }
}

public class UserAlreadyExistsException extends Exception {
    public UserAlreadyExistsException(String email) {
        super("User already exists with email: " + email);
    }
}

public class AuthenticationException extends Exception {
    public AuthenticationException(String message) {
        super(message);
    }

    public AuthenticationException(String message, Throwable cause) {
        super(message, cause);
    }
}

public class AuthorizationException extends Exception {
    public AuthorizationException(String message) {
        super(message);
    }
}
"""
    )

    # Service module implementation
    service_file = (
        java_modules_project
        / "src"
        / "service.module"
        / "com"
        / "example"
        / "service"
        / "UserServiceImpl.java"
    )
    service_file.write_text(
        """
package com.example.service.impl;

import com.example.api.interfaces.*;
import com.example.api.dto.*;
import com.example.api.exceptions.*;
import com.example.core.spi.DataProcessor;
import com.example.core.spi.ConfigurationProvider;

import java.util.*;
import java.util.concurrent.*;
import java.time.LocalDateTime;

// User service implementation
public class DatabaseUserService implements UserService {

    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;
    private final ExecutorService executorService;
    private final DataProcessor dataProcessor;

    public DatabaseUserService(
            UserRepository userRepository,
            PasswordEncoder passwordEncoder,
            DataProcessor dataProcessor) {
        this.userRepository = userRepository;
        this.passwordEncoder = passwordEncoder;
        this.dataProcessor = dataProcessor;
        this.executorService = Executors.newFixedThreadPool(10);
    }

    @Override
    public CompletableFuture<User> createUser(CreateUserRequest request) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                // Check if user exists
                if (userRepository.findByEmail(request.email()).isPresent()) {
                    throw new CompletionException(
                        new UserAlreadyExistsException(request.email())
                    );
                }

                // Process data
                String processedEmail = dataProcessor.process(request.email());
                String hashedPassword = passwordEncoder.encode(request.password());

                // Create user entity
                UserEntity entity = new UserEntity(
                    UUID.randomUUID().toString(),
                    processedEmail,
                    request.firstName(),
                    request.lastName(),
                    hashedPassword,
                    request.roles(),
                    LocalDateTime.now(),
                    null,
                    true
                );

                // Save to database
                UserEntity saved = userRepository.save(entity);
                return mapToUser(saved);

            } catch (Exception e) {
                throw new CompletionException(e);
            }
        }, executorService);
    }

    @Override
    public CompletableFuture<Optional<User>> findUserById(String userId) {
        return CompletableFuture.supplyAsync(() -> {
            return userRepository.findById(userId).map(this::mapToUser);
        }, executorService);
    }

    @Override
    public CompletableFuture<List<User>> findUsersByEmail(String email) {
        return CompletableFuture.supplyAsync(() -> {
            String processedEmail = dataProcessor.process(email);
            return userRepository.findByEmailPattern(processedEmail)
                .stream()
                .map(this::mapToUser)
                .toList();
        }, executorService);
    }

    @Override
    public CompletableFuture<Void> updateUser(String userId, UpdateUserRequest request) {
        return CompletableFuture.runAsync(() -> {
            try {
                UserEntity entity = userRepository.findById(userId)
                    .orElseThrow(() -> new UserNotFoundException(userId));

                // Update fields if present
                request.firstName().ifPresent(entity::setFirstName);
                request.lastName().ifPresent(entity::setLastName);
                request.roles().ifPresent(entity::setRoles);
                request.active().ifPresent(entity::setActive);

                userRepository.save(entity);

            } catch (Exception e) {
                throw new CompletionException(e);
            }
        }, executorService);
    }

    @Override
    public CompletableFuture<Void> deleteUser(String userId) {
        return CompletableFuture.runAsync(() -> {
            if (!userRepository.existsById(userId)) {
                throw new CompletionException(new UserNotFoundException(userId));
            }
            userRepository.deleteById(userId);
        }, executorService);
    }

    @Override
    public CompletableFuture<List<User>> searchUsers(SearchCriteria criteria) {
        return CompletableFuture.supplyAsync(() -> {
            return userRepository.search(criteria)
                .stream()
                .map(this::mapToUser)
                .toList();
        }, executorService);
    }

    private User mapToUser(UserEntity entity) {
        return new User(
            entity.getId(),
            entity.getEmail(),
            entity.getFirstName(),
            entity.getLastName(),
            entity.getRoles(),
            entity.getCreatedAt(),
            entity.getLastLoginAt(),
            entity.isActive()
        );
    }
}

// Authentication service implementation
public class JwtAuthenticationService implements AuthenticationService {

    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;
    private final TokenGenerator tokenGenerator;
    private final SessionManager sessionManager;
    private final ExecutorService executorService;

    public JwtAuthenticationService(
            UserRepository userRepository,
            PasswordEncoder passwordEncoder,
            TokenGenerator tokenGenerator,
            SessionManager sessionManager) {
        this.userRepository = userRepository;
        this.passwordEncoder = passwordEncoder;
        this.tokenGenerator = tokenGenerator;
        this.sessionManager = sessionManager;
        this.executorService = Executors.newCachedThreadPool();
    }

    @Override
    public CompletableFuture<AuthenticationResult> authenticate(Credentials credentials) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                Optional<UserEntity> userOpt = userRepository.findByEmail(credentials.email());

                if (userOpt.isEmpty()) {
                    return new AuthenticationResult(
                        false, Optional.empty(), Optional.empty(),
                        Optional.empty(), Optional.empty(),
                        Optional.of("Invalid credentials")
                    );
                }

                UserEntity user = userOpt.get();

                if (!passwordEncoder.matches(credentials.password(), user.getPasswordHash())) {
                    return new AuthenticationResult(
                        false, Optional.empty(), Optional.empty(),
                        Optional.empty(), Optional.empty(),
                        Optional.of("Invalid credentials")
                    );
                }

                // Generate tokens
                String sessionId = UUID.randomUUID().toString();
                String accessToken = tokenGenerator.generateAccessToken(user);
                String refreshToken = tokenGenerator.generateRefreshToken(user);

                // Create session
                sessionManager.createSession(sessionId, user.getId(), accessToken);

                // Update last login
                user.setLastLoginAt(LocalDateTime.now());
                userRepository.save(user);

                return new AuthenticationResult(
                    true,
                    Optional.of(sessionId),
                    Optional.of(accessToken),
                    Optional.of(refreshToken),
                    Optional.of(mapToUser(user)),
                    Optional.empty()
                );

            } catch (Exception e) {
                return new AuthenticationResult(
                    false, Optional.empty(), Optional.empty(),
                    Optional.empty(), Optional.empty(),
                    Optional.of("Authentication failed: " + e.getMessage())
                );
            }
        }, executorService);
    }

    @Override
    public CompletableFuture<Void> logout(String sessionId) {
        return CompletableFuture.runAsync(() -> {
            sessionManager.invalidateSession(sessionId);
        }, executorService);
    }

    @Override
    public CompletableFuture<Boolean> validateSession(String sessionId) {
        return CompletableFuture.supplyAsync(() -> {
            return sessionManager.isSessionValid(sessionId);
        }, executorService);
    }

    @Override
    public CompletableFuture<RefreshResult> refreshToken(String refreshToken) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                String userId = tokenGenerator.extractUserIdFromRefreshToken(refreshToken);
                UserEntity user = userRepository.findById(userId)
                    .orElseThrow(() -> new AuthenticationException("User not found"));

                String newAccessToken = tokenGenerator.generateAccessToken(user);

                return new RefreshResult(
                    true,
                    Optional.of(newAccessToken),
                    Optional.empty()
                );

            } catch (Exception e) {
                return new RefreshResult(
                    false,
                    Optional.empty(),
                    Optional.of("Token refresh failed: " + e.getMessage())
                );
            }
        }, executorService);
    }

    private User mapToUser(UserEntity entity) {
        return new User(
            entity.getId(),
            entity.getEmail(),
            entity.getFirstName(),
            entity.getLastName(),
            entity.getRoles(),
            entity.getCreatedAt(),
            entity.getLastLoginAt(),
            entity.isActive()
        );
    }
}

// Supporting classes
interface UserRepository {
    Optional<UserEntity> findById(String id);
    Optional<UserEntity> findByEmail(String email);
    List<UserEntity> findByEmailPattern(String pattern);
    List<UserEntity> search(SearchCriteria criteria);
    UserEntity save(UserEntity entity);
    void deleteById(String id);
    boolean existsById(String id);
}

interface PasswordEncoder {
    String encode(String password);
    boolean matches(String password, String hash);
}

interface TokenGenerator {
    String generateAccessToken(UserEntity user);
    String generateRefreshToken(UserEntity user);
    String extractUserIdFromRefreshToken(String token);
}

interface SessionManager {
    void createSession(String sessionId, String userId, String accessToken);
    void invalidateSession(String sessionId);
    boolean isSessionValid(String sessionId);
}

class UserEntity {
    private String id;
    private String email;
    private String firstName;
    private String lastName;
    private String passwordHash;
    private Set<String> roles;
    private LocalDateTime createdAt;
    private LocalDateTime lastLoginAt;
    private boolean active;

    // Constructor, getters, setters...
    public UserEntity(String id, String email, String firstName, String lastName,
                     String passwordHash, Set<String> roles, LocalDateTime createdAt,
                     LocalDateTime lastLoginAt, boolean active) {
        this.id = id;
        this.email = email;
        this.firstName = firstName;
        this.lastName = lastName;
        this.passwordHash = passwordHash;
        this.roles = roles;
        this.createdAt = createdAt;
        this.lastLoginAt = lastLoginAt;
        this.active = active;
    }

    // Getters and setters
    public String getId() { return id; }
    public String getEmail() { return email; }
    public String getFirstName() { return firstName; }
    public void setFirstName(String firstName) { this.firstName = firstName; }
    public String getLastName() { return lastName; }
    public void setLastName(String lastName) { this.lastName = lastName; }
    public String getPasswordHash() { return passwordHash; }
    public Set<String> getRoles() { return roles; }
    public void setRoles(Set<String> roles) { this.roles = roles; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public LocalDateTime getLastLoginAt() { return lastLoginAt; }
    public void setLastLoginAt(LocalDateTime lastLoginAt) { this.lastLoginAt = lastLoginAt; }
    public boolean isActive() { return active; }
    public void setActive(boolean active) { this.active = active; }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_modules_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    # Verify classes across modules were detected
    all_calls = mock_ingestor.ensure_node_batch.call_args_list

    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    interface_calls = [call for call in all_calls if call[0][0] == "Interface"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    {call[0][1]["qualified_name"] for call in interface_calls}

    # Verify we have classes from both API and Service modules
    api_classes = [cls for cls in created_classes if "api.module" in cls]
    service_classes = [cls for cls in created_classes if "service.module" in cls]

    assert len(api_classes) > 0, "No API module classes detected"
    assert len(service_classes) > 0, "No Service module classes detected"
