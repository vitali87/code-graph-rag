from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_nodes, run_updater


@pytest.fixture
def typescript_enums_project(temp_repo: Path) -> Path:
    """Create a comprehensive TypeScript project with enum patterns."""
    project_path = temp_repo / "typescript_enums_test"
    project_path.mkdir()

    (project_path / "types").mkdir()
    (project_path / "constants").mkdir()

    (project_path / "types" / "status.ts").write_text(
        encoding="utf-8",
        data="""
export enum Status {
    Pending,
    Approved,
    Rejected
}
""",
    )

    return project_path


def test_numeric_enums(
    typescript_enums_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript numeric enums."""
    test_file = typescript_enums_project / "numeric_enums.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Basic numeric enums

// Default numeric enum (starts at 0)
enum Direction {
    Up,     // 0
    Down,   // 1
    Left,   // 2
    Right   // 3
}

// Enum with custom starting value
enum HttpStatus {
    Ok = 200,
    NotFound = 404,
    InternalServerError = 500
}

// Mixed explicit and implicit values
enum Priority {
    Low = 1,
    Medium,     // 2
    High,       // 3
    Critical = 10,
    Emergency   // 11
}

// Enum with negative values
enum Temperature {
    Freezing = -10,
    Cold = 0,
    Warm = 20,
    Hot = 30
}

// Computed enum values
enum FileAccess {
    None = 0,
    Read = 1 << 0,      // 1
    Write = 1 << 1,     // 2
    ReadWrite = Read | Write  // 3
}

// Enum with calculated values
enum Permissions {
    None = 0,
    Read = 1,
    Write = 2,
    Execute = 4,
    ReadWrite = Read + Write,
    All = Read | Write | Execute
}

// Functions using numeric enums
function processDirection(direction: Direction): string {
    switch (direction) {
        case Direction.Up:
            return 'Moving up';
        case Direction.Down:
            return 'Moving down';
        case Direction.Left:
            return 'Moving left';
        case Direction.Right:
            return 'Moving right';
        default:
            return 'Unknown direction';
    }
}

function getHttpStatusMessage(status: HttpStatus): string {
    switch (status) {
        case HttpStatus.Ok:
            return 'Success';
        case HttpStatus.NotFound:
            return 'Resource not found';
        case HttpStatus.InternalServerError:
            return 'Server error';
        default:
            return 'Unknown status';
    }
}

function checkPermissions(required: Permissions, actual: Permissions): boolean {
    return (actual & required) === required;
}

// Enum as type
function setPriority(item: { priority: Priority }): void {
    console.log(`Priority set to: ${Priority[item.priority]}`);
}

// Reverse mapping usage
function getDirectionName(value: number): string | undefined {
    return Direction[value];
}

// Class using enums
class Task {
    constructor(
        public name: string,
        public priority: Priority = Priority.Medium,
        public status: HttpStatus = HttpStatus.Ok
    ) {}

    updatePriority(newPriority: Priority): void {
        this.priority = newPriority;
    }

    hasHighPriority(): boolean {
        return this.priority >= Priority.High;
    }

    getPriorityName(): string {
        return Priority[this.priority];
    }
}

// Interface with enum properties
interface ApiResponse {
    status: HttpStatus;
    data?: any;
    message?: string;
}

interface User {
    name: string;
    permissions: Permissions;
    direction?: Direction;
}

// Using numeric enums
console.log(Direction.Up);              // 0
console.log(Direction[0]);              // "Up"
console.log(HttpStatus.Ok);             // 200
console.log(Priority.Critical);         // 10

const result = processDirection(Direction.Left);
const statusMsg = getHttpStatusMessage(HttpStatus.NotFound);

const user: User = {
    name: 'Alice',
    permissions: Permissions.ReadWrite,
    direction: Direction.Up
};

const hasRead = checkPermissions(Permissions.Read, user.permissions);
const hasWrite = checkPermissions(Permissions.Write, user.permissions);

const task = new Task('Important Task', Priority.High, HttpStatus.Ok);
task.updatePriority(Priority.Critical);

// Array of enum values
const allDirections = Object.values(Direction).filter(v => typeof v === 'number') as Direction[];
const allPriorities = [Priority.Low, Priority.Medium, Priority.High, Priority.Critical];

// Enum iteration
for (const direction in Direction) {
    if (isNaN(Number(direction))) {
        console.log(`Direction: ${direction} = ${Direction[direction as keyof typeof Direction]}`);
    }
}

// Type guards with enums
function isValidDirection(value: any): value is Direction {
    return typeof value === 'number' && value in Direction;
}

function isHighPriority(priority: Priority): boolean {
    return priority >= Priority.High;
}

// Enum as object keys
const directionMessages: Record<Direction, string> = {
    [Direction.Up]: 'Going up',
    [Direction.Down]: 'Going down',
    [Direction.Left]: 'Going left',
    [Direction.Right]: 'Going right'
};

// Default parameter with enum
function movePlayer(direction: Direction = Direction.Up): void {
    console.log(directionMessages[direction]);
}

movePlayer(); // Uses default
movePlayer(Direction.Right);
""",
    )

    run_updater(typescript_enums_project, mock_ingestor)

    function_calls = get_nodes(mock_ingestor, "Function")

    enum_using_functions = [
        call
        for call in function_calls
        if "numeric_enums" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in [
                "processDirection",
                "getHttpStatusMessage",
                "checkPermissions",
            ]
        )
    ]

    assert len(enum_using_functions) >= 2, (
        f"Expected at least 2 enum-using functions, found {len(enum_using_functions)}"
    )

    class_calls = get_nodes(mock_ingestor, "Class")

    task_class = [
        call for call in class_calls if "Task" in call[0][1]["qualified_name"]
    ]

    assert len(task_class) >= 1, (
        f"Expected Task class using enums, found {len(task_class)}"
    )


def test_string_enums(
    typescript_enums_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript string enums."""
    test_file = typescript_enums_project / "string_enums.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// String enums

// Basic string enum
enum Color {
    Red = 'red',
    Green = 'green',
    Blue = 'blue',
    Yellow = 'yellow'
}

// String enum with descriptive values
enum LogLevel {
    Error = 'ERROR',
    Warning = 'WARN',
    Info = 'INFO',
    Debug = 'DEBUG'
}

// API endpoint enum
enum ApiEndpoint {
    Users = '/api/users',
    Posts = '/api/posts',
    Comments = '/api/comments',
    Auth = '/api/auth',
    Upload = '/api/upload'
}

// Event type enum
enum EventType {
    Click = 'click',
    Hover = 'hover',
    KeyPress = 'keypress',
    Submit = 'submit',
    Load = 'load'
}

// Mixed string and computed values
enum Theme {
    Light = 'light',
    Dark = 'dark',
    Auto = 'auto',
    Custom = `custom-${Date.now()}`
}

// Functions using string enums
function setElementColor(element: HTMLElement, color: Color): void {
    element.style.backgroundColor = color; // Direct use as string
}

function log(level: LogLevel, message: string): void {
    console.log(`[${level}] ${message}`);
}

function makeApiCall(endpoint: ApiEndpoint): Promise<Response> {
    return fetch(endpoint); // Direct use as URL
}

function addEventListener(type: EventType, handler: () => void): void {
    document.addEventListener(type, handler);
}

// String enum as union type
type SupportedColor = Color.Red | Color.Blue | Color.Green;

function validateColor(color: string): color is SupportedColor {
    return Object.values(Color).includes(color as Color) &&
           [Color.Red, Color.Blue, Color.Green].includes(color as SupportedColor);
}

// Class using string enums
class Logger {
    constructor(private defaultLevel: LogLevel = LogLevel.Info) {}

    log(message: string, level: LogLevel = this.defaultLevel): void {
        if (this.shouldLog(level)) {
            console.log(`[${level}] ${message}`);
        }
    }

    error(message: string): void {
        this.log(message, LogLevel.Error);
    }

    warn(message: string): void {
        this.log(message, LogLevel.Warning);
    }

    info(message: string): void {
        this.log(message, LogLevel.Info);
    }

    debug(message: string): void {
        this.log(message, LogLevel.Debug);
    }

    private shouldLog(level: LogLevel): boolean {
        const levels = [LogLevel.Error, LogLevel.Warning, LogLevel.Info, LogLevel.Debug];
        const currentIndex = levels.indexOf(this.defaultLevel);
        const messageIndex = levels.indexOf(level);
        return messageIndex <= currentIndex;
    }

    setLevel(level: LogLevel): void {
        this.defaultLevel = level;
    }
}

// Interface with string enum
interface ThemeConfig {
    theme: Theme;
    primaryColor: Color;
    logLevel: LogLevel;
}

interface EventHandler {
    type: EventType;
    handler: () => void;
}

// Service class using multiple string enums
class ApiService {
    constructor(private baseUrl: string = '') {}

    async request(endpoint: ApiEndpoint, method: string = 'GET'): Promise<any> {
        const url = this.baseUrl + endpoint;
        const response = await fetch(url, { method });
        return response.json();
    }

    async getUsers(): Promise<any[]> {
        return this.request(ApiEndpoint.Users);
    }

    async getPosts(): Promise<any[]> {
        return this.request(ApiEndpoint.Posts);
    }

    async authenticate(credentials: any): Promise<any> {
        return this.request(ApiEndpoint.Auth, 'POST');
    }
}

// Using string enums
const logger = new Logger(LogLevel.Debug);
logger.error('This is an error');
logger.info('This is info');

const apiService = new ApiService('https://api.example.com');
apiService.getUsers().then(users => console.log(users));

// String enum in switch statement
function getColorHex(color: Color): string {
    switch (color) {
        case Color.Red:
            return '#FF0000';
        case Color.Green:
            return '#00FF00';
        case Color.Blue:
            return '#0000FF';
        case Color.Yellow:
            return '#FFFF00';
        default:
            return '#000000';
    }
}

// String enum as object keys
const colorNames: Record<Color, string> = {
    [Color.Red]: 'Red Color',
    [Color.Green]: 'Green Color',
    [Color.Blue]: 'Blue Color',
    [Color.Yellow]: 'Yellow Color'
};

// Enum iteration
const allColors = Object.values(Color);
const allLogLevels = Object.values(LogLevel);

for (const color of allColors) {
    console.log(`Color: ${color}, Hex: ${getColorHex(color)}`);
}

// Type assertion with string enum
function parseLogLevel(value: string): LogLevel {
    if (Object.values(LogLevel).includes(value as LogLevel)) {
        return value as LogLevel;
    }
    throw new Error(`Invalid log level: ${value}`);
}

// String enum in array
const supportedColors: Color[] = [Color.Red, Color.Green, Color.Blue];
const debugLevels: LogLevel[] = [LogLevel.Debug, LogLevel.Info];

// Configuration using string enums
const appConfig: ThemeConfig = {
    theme: Theme.Dark,
    primaryColor: Color.Blue,
    logLevel: LogLevel.Warning
};

// Event handlers using string enum
const eventHandlers: EventHandler[] = [
    { type: EventType.Click, handler: () => console.log('Clicked') },
    { type: EventType.Hover, handler: () => console.log('Hovered') },
    { type: EventType.Submit, handler: () => console.log('Submitted') }
];

eventHandlers.forEach(({ type, handler }) => {
    addEventListener(type, handler);
});

// String enum comparison
function isSameColor(color1: Color, color2: Color): boolean {
    return color1 === color2;
}

// Template literal with string enum
function createMessage(level: LogLevel, text: string): string {
    return `[${level.toUpperCase()}]: ${text}`;
}
""",
    )

    run_updater(typescript_enums_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    string_enum_classes = [
        call
        for call in class_calls
        if "string_enums" in call[0][1]["qualified_name"]
        and any(
            class_name in call[0][1]["qualified_name"]
            for class_name in ["Logger", "ApiService"]
        )
    ]

    assert len(string_enum_classes) >= 2, (
        f"Expected at least 2 classes using string enums, found {len(string_enum_classes)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    string_enum_functions = [
        call
        for call in function_calls
        if "string_enums" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["setElementColor", "log", "makeApiCall", "getColorHex"]
        )
    ]

    assert len(string_enum_functions) >= 3, (
        f"Expected at least 3 string enum functions, found {len(string_enum_functions)}"
    )


def test_const_enums(
    typescript_enums_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test TypeScript const enums."""
    test_file = typescript_enums_project / "const_enums.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Const enums - inlined at compile time

// Basic const enum
const enum HttpMethod {
    GET = 'GET',
    POST = 'POST',
    PUT = 'PUT',
    DELETE = 'DELETE',
    PATCH = 'PATCH'
}

// Numeric const enum
const enum Size {
    Small,      // 0
    Medium,     // 1
    Large,      // 2
    ExtraLarge  // 3
}

// Const enum with computed values
const enum Flags {
    None = 0,
    Read = 1 << 0,      // 1
    Write = 1 << 1,     // 2
    Execute = 1 << 2,   // 4
    All = Read | Write | Execute  // 7
}

// Const enum with string values
const enum Environment {
    Development = 'development',
    Testing = 'testing',
    Staging = 'staging',
    Production = 'production'
}

// Functions using const enums
function makeRequest(method: HttpMethod, url: string): Promise<Response> {
    return fetch(url, { method });
}

function getSizeClass(size: Size): string {
    switch (size) {
        case Size.Small:
            return 'size-sm';
        case Size.Medium:
            return 'size-md';
        case Size.Large:
            return 'size-lg';
        case Size.ExtraLarge:
            return 'size-xl';
        default:
            return 'size-md';
    }
}

function hasPermission(userFlags: Flags, required: Flags): boolean {
    return (userFlags & required) === required;
}

function getConfigForEnvironment(env: Environment): object {
    switch (env) {
        case Environment.Development:
            return { debug: true, apiUrl: 'http://localhost:3000' };
        case Environment.Testing:
            return { debug: true, apiUrl: 'http://test.example.com' };
        case Environment.Staging:
            return { debug: false, apiUrl: 'http://staging.example.com' };
        case Environment.Production:
            return { debug: false, apiUrl: 'https://api.example.com' };
        default:
            return { debug: false, apiUrl: 'https://api.example.com' };
    }
}

// Class using const enums
class ApiClient {
    constructor(private environment: Environment = Environment.Development) {}

    async get(url: string): Promise<any> {
        return this.request(HttpMethod.GET, url);
    }

    async post(url: string, data?: any): Promise<any> {
        return this.request(HttpMethod.POST, url, data);
    }

    async put(url: string, data: any): Promise<any> {
        return this.request(HttpMethod.PUT, url, data);
    }

    async delete(url: string): Promise<any> {
        return this.request(HttpMethod.DELETE, url);
    }

    private async request(method: HttpMethod, url: string, data?: any): Promise<any> {
        const config = getConfigForEnvironment(this.environment);
        const fullUrl = `${config.apiUrl}${url}`;

        const options: RequestInit = { method };
        if (data) {
            options.body = JSON.stringify(data);
            options.headers = { 'Content-Type': 'application/json' };
        }

        const response = await fetch(fullUrl, options);
        return response.json();
    }

    setEnvironment(env: Environment): void {
        this.environment = env;
    }
}

// Interface using const enums
interface RequestConfig {
    method: HttpMethod;
    environment: Environment;
    flags: Flags;
}

interface ComponentProps {
    size: Size;
    disabled?: boolean;
}

// Using const enums
const client = new ApiClient(Environment.Development);
client.get('/users').then(users => console.log(users));
client.post('/users', { name: 'Alice', email: 'alice@example.com' });

// Const enum values are inlined at compile time
const getMethod = HttpMethod.GET;          // Becomes 'GET'
const smallSize = Size.Small;              // Becomes 0
const readFlag = Flags.Read;               // Becomes 1
const prodEnv = Environment.Production;    // Becomes 'production'

// Const enums in arrays
const allMethods: HttpMethod[] = [
    HttpMethod.GET,
    HttpMethod.POST,
    HttpMethod.PUT,
    HttpMethod.DELETE
];

const sizeOrder: Size[] = [
    Size.Small,
    Size.Medium,
    Size.Large,
    Size.ExtraLarge
];

// Type guards with const enums
function isValidHttpMethod(value: string): value is HttpMethod {
    return ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'].includes(value);
}

function isValidSize(value: number): value is Size {
    return value >= Size.Small && value <= Size.ExtraLarge;
}

// Configuration using const enums
const apiConfig: RequestConfig = {
    method: HttpMethod.POST,
    environment: Environment.Staging,
    flags: Flags.Read | Flags.Write
};

const buttonProps: ComponentProps = {
    size: Size.Large,
    disabled: false
};

// Const enum in switch with fall-through
function getMethodDescription(method: HttpMethod): string {
    switch (method) {
        case HttpMethod.GET:
            return 'Retrieve data';
        case HttpMethod.POST:
        case HttpMethod.PUT:
        case HttpMethod.PATCH:
            return 'Modify data';
        case HttpMethod.DELETE:
            return 'Remove data';
        default:
            return 'Unknown operation';
    }
}

// Computed const enum usage
function checkUserPermissions(user: { flags: Flags }): {
    canRead: boolean;
    canWrite: boolean;
    canExecute: boolean;
} {
    return {
        canRead: hasPermission(user.flags, Flags.Read),
        canWrite: hasPermission(user.flags, Flags.Write),
        canExecute: hasPermission(user.flags, Flags.Execute)
    };
}

// Const enum as default parameter
function createRequest(
    url: string,
    method: HttpMethod = HttpMethod.GET,
    env: Environment = Environment.Development
): RequestConfig {
    return {
        method,
        environment: env,
        flags: Flags.Read
    };
}

// Using const enums in template literals
function logRequest(method: HttpMethod, url: string): void {
    console.log(`Making ${method} request to ${url}`);
}

// Const enum in object keys (careful - these get inlined)
const methodHandlers = {
    [HttpMethod.GET]: (url: string) => console.log(`GET ${url}`),
    [HttpMethod.POST]: (url: string) => console.log(`POST ${url}`)
};

logRequest(HttpMethod.POST, '/api/users');
methodHandlers[HttpMethod.GET]('/api/data');
""",
    )

    run_updater(typescript_enums_project, mock_ingestor)

    class_calls = get_nodes(mock_ingestor, "Class")

    api_client_class = [
        call for call in class_calls if "ApiClient" in call[0][1]["qualified_name"]
    ]

    assert len(api_client_class) >= 1, (
        f"Expected ApiClient class using const enums, found {len(api_client_class)}"
    )

    function_calls = get_nodes(mock_ingestor, "Function")

    const_enum_functions = [
        call
        for call in function_calls
        if "const_enums" in call[0][1]["qualified_name"]
        and any(
            pattern in call[0][1]["qualified_name"]
            for pattern in ["makeRequest", "getSizeClass", "hasPermission"]
        )
    ]

    assert len(const_enum_functions) >= 2, (
        f"Expected at least 2 const enum functions, found {len(const_enum_functions)}"
    )


def test_enum_comprehensive(
    typescript_enums_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Comprehensive test ensuring all TypeScript enum patterns are covered."""
    test_file = typescript_enums_project / "comprehensive_enums.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Every TypeScript enum pattern in one file

// Numeric enum
enum Status {
    Pending,
    Approved,
    Rejected
}

// String enum
enum Color {
    Red = 'red',
    Green = 'green',
    Blue = 'blue'
}

// Const enum
const enum Direction {
    Up = 'up',
    Down = 'down',
    Left = 'left',
    Right = 'right'
}

// Mixed enum
enum Mixed {
    Zero = 0,
    One = 1,
    Two = 'two'
}

// Computed enum
enum Computed {
    A = 1,
    B = A * 2,
    C = B + 1
}

// Interface using enums
interface Item {
    status: Status;
    color: Color;
    direction: Direction;
}

// Class using all enum types
class EnumDemo {
    constructor(
        private status: Status = Status.Pending,
        private color: Color = Color.Red
    ) {}

    updateStatus(newStatus: Status): void {
        this.status = newStatus;
    }

    getColor(): Color {
        return this.color;
    }

    move(direction: Direction): string {
        return `Moving ${direction}`;
    }

    process(): string {
        switch (this.status) {
            case Status.Pending:
                return 'Processing...';
            case Status.Approved:
                return 'Completed';
            case Status.Rejected:
                return 'Failed';
            default:
                return 'Unknown';
        }
    }
}

// Function using multiple enums
function createItem(
    status: Status = Status.Pending,
    color: Color = Color.Blue,
    direction: Direction = Direction.Up
): Item {
    return { status, color, direction };
}

// Using all enum patterns
const demo = new EnumDemo(Status.Approved, Color.Green);
console.log(demo.process());
console.log(demo.move(Direction.Right));

const item = createItem(Status.Pending, Color.Red, Direction.Left);

// Enum utilities
function isApproved(status: Status): boolean {
    return status === Status.Approved;
}

function getStatusName(status: Status): string {
    return Status[status];
}

// Array of enums
const allStatuses = [Status.Pending, Status.Approved, Status.Rejected];
const allColors = Object.values(Color);

// Enum iteration
for (const status of allStatuses) {
    console.log(`Status ${status}: ${getStatusName(status)}`);
}

console.log(isApproved(item.status));
console.log(demo.getColor());
""",
    )

    run_updater(typescript_enums_project, mock_ingestor)

    all_relationships = cast(
        MagicMock, mock_ingestor.ensure_relationship_batch
    ).call_args_list

    [c for c in all_relationships if c.args[1] == "CALLS"]
    [c for c in all_relationships if c.args[1] == "DEFINES"]

    class_calls = get_nodes(mock_ingestor, "Class")

    enum_demo_class = [
        call for call in class_calls if "EnumDemo" in call[0][1]["qualified_name"]
    ]

    assert len(enum_demo_class) >= 1, (
        f"Expected EnumDemo class, found {len(enum_demo_class)}"
    )
