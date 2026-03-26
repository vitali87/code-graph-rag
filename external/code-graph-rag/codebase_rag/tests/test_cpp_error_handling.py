from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_relationships, run_updater


@pytest.fixture
def cpp_error_handling_project(temp_repo: Path) -> Path:
    """Create a comprehensive C++ project with error handling patterns."""
    project_path = temp_repo / "cpp_error_handling_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "include").mkdir()

    return project_path


def test_basic_exception_handling(
    cpp_error_handling_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic exception handling with try/catch/throw."""
    test_file = cpp_error_handling_project / "basic_exceptions.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <exception>
#include <stdexcept>
#include <string>

// Custom exception class
class FileProcessingError : public std::runtime_error {
private:
    std::string filename_;
    int error_code_;

public:
    FileProcessingError(const std::string& filename, int code, const std::string& message)
        : std::runtime_error(message), filename_(filename), error_code_(code) {}

    const std::string& getFilename() const { return filename_; }
    int getErrorCode() const { return error_code_; }

    virtual const char* what() const noexcept override {
        static std::string full_message =
            "FileProcessingError(" + filename_ + ", " +
            std::to_string(error_code_) + "): " + std::runtime_error::what();
        return full_message.c_str();
    }
};

// Network exception hierarchy
class NetworkException : public std::exception {
protected:
    std::string message_;

public:
    NetworkException(const std::string& message) : message_(message) {}
    virtual const char* what() const noexcept override { return message_.c_str(); }
};

class ConnectionTimeoutException : public NetworkException {
private:
    int timeout_seconds_;

public:
    ConnectionTimeoutException(int timeout)
        : NetworkException("Connection timeout after " + std::to_string(timeout) + " seconds"),
          timeout_seconds_(timeout) {}

    int getTimeoutSeconds() const { return timeout_seconds_; }
};

class AuthenticationException : public NetworkException {
private:
    std::string username_;

public:
    AuthenticationException(const std::string& username)
        : NetworkException("Authentication failed for user: " + username),
          username_(username) {}

    const std::string& getUsername() const { return username_; }
};

// File processor class with exception handling
class FileProcessor {
private:
    std::string base_path_;
    bool debug_mode_;

public:
    FileProcessor(const std::string& base_path, bool debug = false)
        : base_path_(base_path), debug_mode_(debug) {}

    void processFile(const std::string& filename) {
        if (debug_mode_) {
            std::cout << "Processing file: " << filename << std::endl;
        }

        try {
            validateFile(filename);
            openFile(filename);
            parseContent(filename);
            saveResults(filename);
        }
        catch (const FileProcessingError& e) {
            std::cerr << "File processing failed: " << e.what() << std::endl;
            cleanup(filename);
            throw; // Re-throw for caller to handle
        }
        catch (const std::exception& e) {
            std::cerr << "Unexpected error: " << e.what() << std::endl;
            cleanup(filename);
            throw FileProcessingError(filename, -1, "Unexpected processing error");
        }
    }

    void processBatch(const std::vector<std::string>& filenames) {
        int processed = 0;
        int failed = 0;

        for (const auto& filename : filenames) {
            try {
                processFile(filename);
                processed++;
            }
            catch (const FileProcessingError& e) {
                std::cerr << "Batch processing error: " << e.what() << std::endl;
                failed++;
                // Continue with next file
            }
        }

        std::cout << "Batch complete: " << processed << " processed, "
                  << failed << " failed" << std::endl;

        if (failed > 0 && failed >= processed) {
            throw std::runtime_error("Batch processing failed: too many errors");
        }
    }

private:
    void validateFile(const std::string& filename) {
        if (filename.empty()) {
            throw FileProcessingError(filename, 1, "Empty filename");
        }

        if (filename.find("..") != std::string::npos) {
            throw FileProcessingError(filename, 2, "Invalid path: contains '..'");
        }

        // Simulate file existence check
        if (filename == "nonexistent.txt") {
            throw FileProcessingError(filename, 3, "File does not exist");
        }
    }

    void openFile(const std::string& filename) {
        // Simulate file opening errors
        if (filename == "locked.txt") {
            throw FileProcessingError(filename, 4, "File is locked");
        }

        if (filename == "permission_denied.txt") {
            throw FileProcessingError(filename, 5, "Permission denied");
        }
    }

    void parseContent(const std::string& filename) {
        // Simulate parsing errors
        if (filename == "corrupted.txt") {
            throw FileProcessingError(filename, 6, "File is corrupted");
        }

        if (filename == "invalid_format.txt") {
            throw FileProcessingError(filename, 7, "Invalid file format");
        }
    }

    void saveResults(const std::string& filename) {
        // Simulate save errors
        if (filename == "disk_full.txt") {
            throw FileProcessingError(filename, 8, "Disk full");
        }
    }

    void cleanup(const std::string& filename) {
        if (debug_mode_) {
            std::cout << "Cleaning up after error in: " << filename << std::endl;
        }
        // Cleanup operations here
    }
};

// Network client with exception handling
class NetworkClient {
private:
    std::string server_address_;
    int port_;
    int timeout_seconds_;

public:
    NetworkClient(const std::string& address, int port, int timeout = 30)
        : server_address_(address), port_(port), timeout_seconds_(timeout) {}

    void connect() {
        try {
            establishConnection();
            authenticateUser();
            setupSession();
        }
        catch (const ConnectionTimeoutException& e) {
            std::cerr << "Connection failed: " << e.what() << std::endl;
            throw;
        }
        catch (const AuthenticationException& e) {
            std::cerr << "Authentication failed: " << e.what() << std::endl;
            disconnect(); // Cleanup
            throw;
        }
        catch (const NetworkException& e) {
            std::cerr << "Network error: " << e.what() << std::endl;
            disconnect(); // Cleanup
            throw;
        }
    }

    void sendData(const std::string& data) {
        if (data.empty()) {
            throw std::invalid_argument("Cannot send empty data");
        }

        try {
            transmitData(data);
            waitForAcknowledgment();
        }
        catch (const std::exception& e) {
            std::cerr << "Send failed: " << e.what() << std::endl;
            throw NetworkException("Failed to send data: " + std::string(e.what()));
        }
    }

private:
    void establishConnection() {
        // Simulate connection timeout
        if (server_address_ == "unreachable.server.com") {
            throw ConnectionTimeoutException(timeout_seconds_);
        }

        // Simulate other network errors
        if (server_address_ == "invalid.server.com") {
            throw NetworkException("Invalid server address");
        }
    }

    void authenticateUser() {
        // Simulate authentication failure
        if (port_ == 9999) {
            throw AuthenticationException("testuser");
        }
    }

    void setupSession() {
        // Session setup logic
    }

    void transmitData(const std::string& data) {
        // Data transmission logic
        if (data.size() > 1000000) {
            throw NetworkException("Data too large for transmission");
        }
    }

    void waitForAcknowledgment() {
        // Wait for server acknowledgment
    }

    void disconnect() {
        // Cleanup connection
        std::cout << "Disconnecting from " << server_address_ << std::endl;
    }
};

void testBasicExceptionHandling() {
    std::cout << "=== Testing Basic Exception Handling ===" << std::endl;

    FileProcessor processor("/home/data", true);

    // Test successful processing
    try {
        processor.processFile("valid.txt");
        std::cout << "Successfully processed valid.txt" << std::endl;
    }
    catch (const std::exception& e) {
        std::cerr << "Unexpected error: " << e.what() << std::endl;
    }

    // Test various error conditions
    std::vector<std::string> test_files = {
        "nonexistent.txt",
        "locked.txt",
        "corrupted.txt",
        "disk_full.txt"
    };

    for (const auto& filename : test_files) {
        try {
            processor.processFile(filename);
        }
        catch (const FileProcessingError& e) {
            std::cout << "Caught expected error for " << filename
                      << ": " << e.what() << std::endl;
        }
    }

    // Test batch processing
    std::vector<std::string> batch_files = {
        "valid1.txt", "corrupted.txt", "valid2.txt", "locked.txt", "valid3.txt"
    };

    try {
        processor.processBatch(batch_files);
    }
    catch (const std::exception& e) {
        std::cout << "Batch processing result: " << e.what() << std::endl;
    }
}

void testNetworkExceptions() {
    std::cout << "=== Testing Network Exceptions ===" << std::endl;

    // Test successful connection
    NetworkClient good_client("valid.server.com", 80);
    try {
        good_client.connect();
        good_client.sendData("Hello, server!");
        std::cout << "Network communication successful" << std::endl;
    }
    catch (const std::exception& e) {
        std::cerr << "Unexpected network error: " << e.what() << std::endl;
    }

    // Test timeout exception
    NetworkClient timeout_client("unreachable.server.com", 80, 5);
    try {
        timeout_client.connect();
    }
    catch (const ConnectionTimeoutException& e) {
        std::cout << "Caught timeout: " << e.what()
                  << " (timeout: " << e.getTimeoutSeconds() << "s)" << std::endl;
    }

    // Test authentication exception
    NetworkClient auth_client("valid.server.com", 9999);
    try {
        auth_client.connect();
    }
    catch (const AuthenticationException& e) {
        std::cout << "Caught auth error: " << e.what()
                  << " (user: " << e.getUsername() << ")" << std::endl;
    }

    // Test invalid data
    NetworkClient data_client("valid.server.com", 80);
    try {
        data_client.connect();
        data_client.sendData(""); // Empty data
    }
    catch (const std::invalid_argument& e) {
        std::cout << "Caught invalid argument: " << e.what() << std::endl;
    }
    catch (const std::exception& e) {
        std::cout << "Caught exception: " << e.what() << std::endl;
    }
}

void demonstrateBasicExceptionHandling() {
    testBasicExceptionHandling();
    testNetworkExceptions();
}
""",
    )

    run_updater(cpp_error_handling_project, mock_ingestor)

    project_name = cpp_error_handling_project.name

    expected_classes = [
        f"{project_name}.basic_exceptions.FileProcessingError",
        f"{project_name}.basic_exceptions.NetworkException",
        f"{project_name}.basic_exceptions.ConnectionTimeoutException",
        f"{project_name}.basic_exceptions.AuthenticationException",
        f"{project_name}.basic_exceptions.FileProcessor",
        f"{project_name}.basic_exceptions.NetworkClient",
    ]

    expected_functions = [
        f"{project_name}.basic_exceptions.testBasicExceptionHandling",
        f"{project_name}.basic_exceptions.testNetworkExceptions",
        f"{project_name}.basic_exceptions.demonstrateBasicExceptionHandling",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )

    created_functions = get_node_names(mock_ingestor, "Function")

    missing_functions = set(expected_functions) - created_functions
    assert not missing_functions, (
        f"Missing expected functions: {sorted(list(missing_functions))}"
    )


def test_raii_patterns(
    cpp_error_handling_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test RAII (Resource Acquisition Is Initialization) patterns."""
    test_file = cpp_error_handling_project / "raii_patterns.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
#include <iostream>
#include <memory>
#include <fstream>
#include <vector>
#include <mutex>
#include <thread>

// RAII file handle wrapper
class FileHandle {
private:
    std::FILE* file_;
    std::string filename_;

public:
    FileHandle(const std::string& filename, const std::string& mode)
        : filename_(filename) {
        file_ = std::fopen(filename.c_str(), mode.c_str());
        if (!file_) {
            throw std::runtime_error("Failed to open file: " + filename);
        }
        std::cout << "File opened: " << filename << std::endl;
    }

    ~FileHandle() {
        if (file_) {
            std::fclose(file_);
            std::cout << "File closed: " << filename_ << std::endl;
        }
    }

    // Delete copy constructor and assignment to prevent copying
    FileHandle(const FileHandle&) = delete;
    FileHandle& operator=(const FileHandle&) = delete;

    // Move constructor and assignment
    FileHandle(FileHandle&& other) noexcept
        : file_(other.file_), filename_(std::move(other.filename_)) {
        other.file_ = nullptr;
    }

    FileHandle& operator=(FileHandle&& other) noexcept {
        if (this != &other) {
            if (file_) {
                std::fclose(file_);
            }
            file_ = other.file_;
            filename_ = std::move(other.filename_);
            other.file_ = nullptr;
        }
        return *this;
    }

    std::FILE* get() const { return file_; }
    const std::string& filename() const { return filename_; }

    bool isValid() const { return file_ != nullptr; }
};

// RAII memory buffer
class MemoryBuffer {
private:
    char* buffer_;
    size_t size_;

public:
    MemoryBuffer(size_t size) : size_(size) {
        buffer_ = new char[size_];
        std::cout << "Memory allocated: " << size_ << " bytes" << std::endl;
    }

    ~MemoryBuffer() {
        delete[] buffer_;
        std::cout << "Memory deallocated: " << size_ << " bytes" << std::endl;
    }

    // Delete copy operations
    MemoryBuffer(const MemoryBuffer&) = delete;
    MemoryBuffer& operator=(const MemoryBuffer&) = delete;

    // Move operations
    MemoryBuffer(MemoryBuffer&& other) noexcept
        : buffer_(other.buffer_), size_(other.size_) {
        other.buffer_ = nullptr;
        other.size_ = 0;
    }

    MemoryBuffer& operator=(MemoryBuffer&& other) noexcept {
        if (this != &other) {
            delete[] buffer_;
            buffer_ = other.buffer_;
            size_ = other.size_;
            other.buffer_ = nullptr;
            other.size_ = 0;
        }
        return *this;
    }

    char* data() { return buffer_; }
    const char* data() const { return buffer_; }
    size_t size() const { return size_; }
};

// RAII lock guard (simplified version)
class SimpleLockGuard {
private:
    std::mutex& mutex_;

public:
    explicit SimpleLockGuard(std::mutex& m) : mutex_(m) {
        mutex_.lock();
        std::cout << "Mutex locked" << std::endl;
    }

    ~SimpleLockGuard() {
        mutex_.unlock();
        std::cout << "Mutex unlocked" << std::endl;
    }

    // Delete copy and move operations
    SimpleLockGuard(const SimpleLockGuard&) = delete;
    SimpleLockGuard& operator=(const SimpleLockGuard&) = delete;
    SimpleLockGuard(SimpleLockGuard&&) = delete;
    SimpleLockGuard& operator=(SimpleLockGuard&&) = delete;
};

// RAII database connection
class DatabaseConnection {
private:
    std::string connection_string_;
    bool connected_;

public:
    DatabaseConnection(const std::string& conn_str)
        : connection_string_(conn_str), connected_(false) {
        try {
            connect();
            connected_ = true;
            std::cout << "Database connected: " << connection_string_ << std::endl;
        }
        catch (const std::exception& e) {
            std::cerr << "Database connection failed: " << e.what() << std::endl;
            throw;
        }
    }

    ~DatabaseConnection() {
        if (connected_) {
            disconnect();
            std::cout << "Database disconnected: " << connection_string_ << std::endl;
        }
    }

    // Delete copy operations
    DatabaseConnection(const DatabaseConnection&) = delete;
    DatabaseConnection& operator=(const DatabaseConnection&) = delete;

    void executeQuery(const std::string& query) {
        if (!connected_) {
            throw std::runtime_error("Database not connected");
        }

        std::cout << "Executing query: " << query << std::endl;

        // Simulate query execution that might throw
        if (query.find("INVALID") != std::string::npos) {
            throw std::runtime_error("Invalid SQL query");
        }
    }

    bool isConnected() const { return connected_; }

private:
    void connect() {
        // Simulate connection process
        if (connection_string_.find("invalid") != std::string::npos) {
            throw std::runtime_error("Invalid connection string");
        }
    }

    void disconnect() {
        // Cleanup connection resources
        connected_ = false;
    }
};

// Resource manager combining multiple RAII objects
class ResourceManager {
private:
    std::unique_ptr<FileHandle> log_file_;
    std::unique_ptr<MemoryBuffer> buffer_;
    std::unique_ptr<DatabaseConnection> db_conn_;

public:
    ResourceManager(const std::string& log_filename,
                   size_t buffer_size,
                   const std::string& db_connection) {
        try {
            // Initialize resources in order
            log_file_ = std::make_unique<FileHandle>(log_filename, "w");
            buffer_ = std::make_unique<MemoryBuffer>(buffer_size);
            db_conn_ = std::make_unique<DatabaseConnection>(db_connection);

            logMessage("ResourceManager initialized successfully");
        }
        catch (const std::exception& e) {
            logMessage("ResourceManager initialization failed: " + std::string(e.what()));
            throw;
        }
    }

    void processData(const std::string& query) {
        if (!isReady()) {
            throw std::runtime_error("ResourceManager not ready");
        }

        try {
            logMessage("Processing data with query: " + query);

            // Use database connection
            db_conn_->executeQuery(query);

            // Use memory buffer for temporary storage
            std::string result = "Query result data";
            if (result.size() <= buffer_->size()) {
                std::memcpy(buffer_->data(), result.c_str(), result.size());
                logMessage("Data stored in buffer");
            }

            logMessage("Data processing completed");
        }
        catch (const std::exception& e) {
            logMessage("Data processing failed: " + std::string(e.what()));
            throw;
        }
    }

    bool isReady() const {
        return log_file_ && log_file_->isValid() &&
               buffer_ && buffer_->data() &&
               db_conn_ && db_conn_->isConnected();
    }

private:
    void logMessage(const std::string& message) {
        if (log_file_ && log_file_->isValid()) {
            std::fprintf(log_file_->get(), "[%s] %s\\n",
                        getCurrentTimestamp().c_str(), message.c_str());
            std::fflush(log_file_->get());
        }
        std::cout << "[LOG] " << message << std::endl;
    }

    std::string getCurrentTimestamp() const {
        return "2024-01-01 12:00:00"; // Simplified timestamp
    }
};

void testRAIIFileHandling() {
    std::cout << "=== Testing RAII File Handling ===" << std::endl;

    try {
        FileHandle file("test.txt", "w");
        std::fprintf(file.get(), "Hello, RAII!\\n");
        std::cout << "File operations completed" << std::endl;
        // File automatically closed when 'file' goes out of scope
    }
    catch (const std::exception& e) {
        std::cerr << "File handling error: " << e.what() << std::endl;
    }

    // Test exception safety
    try {
        FileHandle bad_file("nonexistent/path/file.txt", "w");
    }
    catch (const std::exception& e) {
        std::cout << "Expected file error: " << e.what() << std::endl;
    }
}

void testRAIIMemoryManagement() {
    std::cout << "=== Testing RAII Memory Management ===" << std::endl;

    {
        MemoryBuffer buffer(1024);
        std::strcpy(buffer.data(), "Hello, RAII Memory!");
        std::cout << "Buffer content: " << buffer.data() << std::endl;
        // Memory automatically freed when 'buffer' goes out of scope
    }

    std::cout << "Buffer scope ended" << std::endl;
}

void testRAIIMutexLocking() {
    std::cout << "=== Testing RAII Mutex Locking ===" << std::endl;

    std::mutex shared_mutex;
    int shared_counter = 0;

    auto worker_function = [&shared_mutex, &shared_counter](int worker_id) {
        try {
            SimpleLockGuard lock(shared_mutex);

            // Critical section
            int old_value = shared_counter;
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            shared_counter = old_value + 1;

            std::cout << "Worker " << worker_id << " incremented counter to "
                      << shared_counter << std::endl;

            // Lock automatically released when 'lock' goes out of scope
        }
        catch (const std::exception& e) {
            std::cerr << "Worker " << worker_id << " error: " << e.what() << std::endl;
        }
    };

    // Create threads that will use RAII locking
    std::vector<std::thread> workers;
    for (int i = 0; i < 3; ++i) {
        workers.emplace_back(worker_function, i);
    }

    for (auto& worker : workers) {
        worker.join();
    }

    std::cout << "Final counter value: " << shared_counter << std::endl;
}

void testRAIIResourceManager() {
    std::cout << "=== Testing RAII Resource Manager ===" << std::endl;

    try {
        ResourceManager manager("application.log", 2048, "postgresql://localhost:5432/test");

        manager.processData("SELECT * FROM users");
        manager.processData("UPDATE users SET status = 'active'");

        std::cout << "Resource manager operations completed" << std::endl;
        // All resources automatically cleaned up when 'manager' goes out of scope
    }
    catch (const std::exception& e) {
        std::cerr << "Resource manager error: " << e.what() << std::endl;
    }

    // Test resource manager with invalid connection
    try {
        ResourceManager bad_manager("application.log", 1024, "invalid://connection");
    }
    catch (const std::exception& e) {
        std::cout << "Expected resource manager error: " << e.what() << std::endl;
    }
}

void demonstrateRAIIPatterns() {
    testRAIIFileHandling();
    testRAIIMemoryManagement();
    testRAIIMutexLocking();
    testRAIIResourceManager();
}
""",
    )

    run_updater(cpp_error_handling_project, mock_ingestor)

    project_name = cpp_error_handling_project.name

    expected_classes = [
        f"{project_name}.raii_patterns.FileHandle",
        f"{project_name}.raii_patterns.MemoryBuffer",
        f"{project_name}.raii_patterns.SimpleLockGuard",
        f"{project_name}.raii_patterns.DatabaseConnection",
        f"{project_name}.raii_patterns.ResourceManager",
    ]

    created_classes = get_node_names(mock_ingestor, "Class")

    missing_classes = set(expected_classes) - created_classes
    assert not missing_classes, (
        f"Missing expected classes: {sorted(list(missing_classes))}"
    )


def test_cpp_error_handling_comprehensive(
    cpp_error_handling_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all error handling patterns create proper relationships."""
    test_file = cpp_error_handling_project / "comprehensive_error_handling.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Comprehensive error handling combining exceptions, RAII, and error recovery
#include <iostream>
#include <memory>
#include <exception>
#include <vector>

class ComprehensiveErrorDemo {
private:
    std::vector<std::unique_ptr<std::exception>> error_log_;
    bool recovery_mode_;

public:
    ComprehensiveErrorDemo() : recovery_mode_(false) {}

    void processWithRecovery() {
        std::cout << "=== Comprehensive Error Handling Demo ===" << std::endl;

        try {
            performRiskyOperations();
        }
        catch (const std::exception& e) {
            handleError(e);
            attemptRecovery();
        }
    }

private:
    void performRiskyOperations() {
        // Simulate various operations that might fail
        std::vector<int> data = {1, 2, 3, 0, 5}; // Zero will cause division error

        for (size_t i = 0; i < data.size(); ++i) {
            try {
                int result = 100 / data[i]; // Potential division by zero
                std::cout << "Result " << i << ": " << result << std::endl;
            }
            catch (const std::exception& e) {
                logError(std::make_unique<std::runtime_error>(
                    "Division error at index " + std::to_string(i)));
                throw; // Re-throw for higher level handling
            }
        }
    }

    void handleError(const std::exception& e) {
        std::cout << "Handling error: " << e.what() << std::endl;
        logError(std::make_unique<std::runtime_error>(e.what()));
    }

    void attemptRecovery() {
        recovery_mode_ = true;
        std::cout << "Attempting error recovery..." << std::endl;

        // Recovery logic here
        std::cout << "Recovery completed. Logged " << error_log_.size()
                  << " errors." << std::endl;
    }

    void logError(std::unique_ptr<std::exception> error) {
        error_log_.push_back(std::move(error));
    }
};

void demonstrateComprehensiveErrorHandling() {
    ComprehensiveErrorDemo demo;
    demo.processWithRecovery();
}
""",
    )

    run_updater(cpp_error_handling_project, mock_ingestor)

    call_relationships = get_relationships(mock_ingestor, "CALLS")
    defines_relationships = get_relationships(mock_ingestor, "DEFINES")

    comprehensive_calls = [
        call
        for call in call_relationships
        if "comprehensive_error_handling" in call.args[0][2]
    ]

    assert len(comprehensive_calls) >= 2, (
        f"Expected at least 2 comprehensive error handling calls, found {len(comprehensive_calls)}"
    )

    assert defines_relationships, "Should still have DEFINES relationships"
