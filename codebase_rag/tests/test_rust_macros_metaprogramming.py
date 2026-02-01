from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_macros_project(temp_repo: Path) -> Path:
    """Create a Rust project with macro examples."""
    project_path = temp_repo / "rust_macros_test"
    project_path.mkdir()

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""
[package]
name = "rust_macros_test"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
syn = "2.0"
quote = "1.0"
proc-macro2 = "1.0"

[lib]
proc-macro = true
""",
    )

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Macros test crate"
    )

    return project_path


def test_declarative_macros_basic(
    rust_macros_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic declarative macro patterns."""
    test_file = rust_macros_project / "declarative_macros.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Simple macro without parameters
macro_rules! say_hello {
    () => {
        println!("Hello, world!");
    };
}

// Macro with single parameter
macro_rules! say_hello_to {
    ($name:expr) => {
        println!("Hello, {}!", $name);
    };
}

// Macro with multiple patterns
macro_rules! calculate {
    (eval $e:expr) => {
        {
            let val: usize = $e;
            println!("{} = {}", stringify!($e), val);
        }
    };

    (eval $e:expr, $(eval $es:expr),+) => {
        calculate!(eval $e);
        calculate!($(eval $es),+);
    };
}

// Macro for creating hash maps
macro_rules! hashmap {
    ($( $key: expr => $val: expr ),*) => {
        {
            let mut map = std::collections::HashMap::new();
            $(
                map.insert($key, $val);
            )*
            map
        }
    };
}

// Macro with repetition
macro_rules! find_min {
    ($x:expr) => ($x);
    ($x:expr, $($y:expr),+) => {
        std::cmp::min($x, find_min!($($y),+))
    };
}

// Macro for implementing traits
macro_rules! impl_display_for {
    ($($type:ty),+) => {
        $(
            impl std::fmt::Display for $type {
                fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                    write!(f, "{:?}", self)
                }
            }
        )+
    };
}

#[derive(Debug)]
struct Point {
    x: i32,
    y: i32,
}

#[derive(Debug)]
struct Rectangle {
    width: u32,
    height: u32,
}

impl_display_for!(Point, Rectangle);

fn test_macros() {
    say_hello!();
    say_hello_to!("Rust");

    calculate!(eval 1 + 2);
    calculate!(eval 1 + 2, eval 3 + 4, eval 5 + 6);

    let map = hashmap!{
        "a" => 1,
        "b" => 2,
        "c" => 3
    };
    println!("HashMap: {:?}", map);

    let min = find_min!(5, 2, 8, 1, 9);
    println!("Minimum: {}", min);

    let point = Point { x: 1, y: 2 };
    let rect = Rectangle { width: 10, height: 20 };
    println!("Point: {}", point);
    println!("Rectangle: {}", rect);
}

// Macro for creating enums with methods
macro_rules! create_enum {
    (
        enum $name:ident {
            $(
                $variant:ident($type:ty)
            ),*
        }
    ) => {
        enum $name {
            $(
                $variant($type),
            )*
        }

        impl $name {
            fn name(&self) -> &'static str {
                match self {
                    $(
                        $name::$variant(_) => stringify!($variant),
                    )*
                }
            }

            $(
                fn $variant(value: $type) -> Self {
                    $name::$variant(value)
                }
            )*
        }
    };
}

create_enum! {
    enum Value {
        Integer(i32),
        Float(f64),
        Text(String)
    }
}

// Advanced macro with TT muncher pattern
macro_rules! parse_options {
    (@collect [$($parsed:tt)*] option $name:ident = $value:expr, $($rest:tt)*) => {
        parse_options!(@collect [$($parsed)* ($name, $value),] $($rest)*);
    };
    (@collect [$($parsed:tt)*] option $name:ident = $value:expr) => {
        parse_options!(@collect [$($parsed)* ($name, $value),]);
    };
    (@collect [$(($name:ident, $value:expr),)*]) => {
        {
            let mut options = std::collections::HashMap::new();
            $(
                options.insert(stringify!($name), $value.to_string());
            )*
            options
        }
    };
    ($($input:tt)*) => {
        parse_options!(@collect [] $($input)*)
    };
}
""",
    )

    run_updater(rust_macros_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    macro_calls = [
        call for call in calls if "test_macros" in str(call) or "Point" in str(call)
    ]
    assert len(macro_calls) > 0, "Macro functions should be detected"


def test_procedural_macros(
    rust_macros_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test procedural macro definitions."""
    test_file = rust_macros_project / "proc_macros.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use proc_macro::TokenStream;
use quote::quote;
use syn::{parse_macro_input, DeriveInput, Data, Fields};

#[proc_macro]
pub fn make_answer(_item: TokenStream) -> TokenStream {
    "fn answer() -> u32 { 42 }".parse().unwrap()
}

#[proc_macro]
pub fn create_function(input: TokenStream) -> TokenStream {
    let input_string = input.to_string();
    let function_name = input_string.trim_matches('"');

    let output = format!(
        "fn {}() {{ println!(\"Function {} was called!\"); }}",
        function_name, function_name
    );

    output.parse().unwrap()
}

#[proc_macro_attribute]
pub fn benchmark(args: TokenStream, input: TokenStream) -> TokenStream {
    let input_fn = parse_macro_input!(input as syn::ItemFn);
    let fn_name = &input_fn.sig.ident;
    let fn_block = &input_fn.block;
    let fn_vis = &input_fn.vis;
    let fn_sig = &input_fn.sig;

    let output = quote! {
        #fn_vis #fn_sig {
            let start = std::time::Instant::now();
            let result = (|| #fn_block)();
            let duration = start.elapsed();
            println!("Function {} took {:?}", stringify!(#fn_name), duration);
            result
        }
    };

    TokenStream::from(output)
}

#[proc_macro_attribute]
pub fn memoize(_args: TokenStream, input: TokenStream) -> TokenStream {
    let input_fn = parse_macro_input!(input as syn::ItemFn);
    let fn_name = &input_fn.sig.ident;
    let fn_inputs = &input_fn.sig.inputs;
    let fn_output = &input_fn.sig.output;
    let fn_block = &input_fn.block;

    let output = quote! {
        fn #fn_name(#fn_inputs) #fn_output {
            use std::collections::HashMap;
            use std::sync::{Mutex, Arc};
            use std::hash::Hash;

            lazy_static::lazy_static! {
                static ref CACHE: Arc<Mutex<HashMap<String, String>>> =
                    Arc::new(Mutex::new(HashMap::new()));
            }

            let key = format!("{:?}", (#fn_inputs));

            {
                let cache = CACHE.lock().unwrap();
                if let Some(cached_result) = cache.get(&key) {
                    return serde_json::from_str(cached_result).unwrap();
                }
            }

            let result = (|| #fn_block)();

            {
                let mut cache = CACHE.lock().unwrap();
                let serialized = serde_json::to_string(&result).unwrap();
                cache.insert(key, serialized);
            }

            result
        }
    };

    TokenStream::from(output)
}

#[proc_macro_derive(Builder)]
pub fn builder_derive(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as DeriveInput);
    let name = &input.ident;
    let builder_name = format!("{}Builder", name);
    let builder_ident = syn::Ident::new(&builder_name, name.span());

    let fields = match input.data {
        Data::Struct(data_struct) => match data_struct.fields {
            Fields::Named(fields_named) => fields_named.named,
            _ => panic!("Builder only supports named fields"),
        },
        _ => panic!("Builder only supports structs"),
    };

    let builder_fields = fields.iter().map(|f| {
        let name = &f.ident;
        let ty = &f.ty;
        quote! { #name: Option<#ty> }
    });

    let builder_methods = fields.iter().map(|f| {
        let name = &f.ident;
        let ty = &f.ty;
        quote! {
            pub fn #name(mut self, #name: #ty) -> Self {
                self.#name = Some(#name);
                self
            }
        }
    });

    let builder_build = {
        let field_assignments = fields.iter().map(|f| {
            let name = &f.ident;
            quote! {
                #name: self.#name.ok_or_else(|| format!("Field {} is required", stringify!(#name)))?
            }
        });

        quote! {
            pub fn build(self) -> Result<#name, String> {
                Ok(#name {
                    #(#field_assignments,)*
                })
            }
        }
    };

    let output = quote! {
        impl #name {
            pub fn builder() -> #builder_ident {
                #builder_ident {
                    #(#(fields.iter().map(|f| {
                        let name = &f.ident;
                        quote! { #name: None }
                    }),)*)*
                }
            }
        }

        pub struct #builder_ident {
            #(#builder_fields,)*
        }

        impl #builder_ident {
            #(#builder_methods)*

            #builder_build
        }
    };

    TokenStream::from(output)
}

#[proc_macro_derive(AutoDebug)]
pub fn auto_debug_derive(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as DeriveInput);
    let name = &input.ident;

    let fields = match input.data {
        Data::Struct(data_struct) => match data_struct.fields {
            Fields::Named(fields_named) => fields_named.named,
            Fields::Unit => {
                return quote! {
                    impl std::fmt::Debug for #name {
                        fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                            write!(f, "{}", stringify!(#name))
                        }
                    }
                }.into();
            },
            _ => panic!("AutoDebug only supports named fields and unit structs"),
        },
        _ => panic!("AutoDebug only supports structs"),
    };

    let debug_fields = fields.iter().map(|f| {
        let name = &f.ident;
        quote! {
            .field(stringify!(#name), &self.#name)
        }
    });

    let output = quote! {
        impl std::fmt::Debug for #name {
            fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.debug_struct(stringify!(#name))
                    #(#debug_fields)*
                    .finish()
            }
        }
    };

    TokenStream::from(output)
}
""",
    )

    run_updater(rust_macros_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    proc_macro_calls = [
        call
        for call in calls
        if "make_answer" in str(call) or "builder_derive" in str(call)
    ]
    assert len(proc_macro_calls) > 0, "Procedural macro functions should be detected"


def test_macro_usage_patterns(
    rust_macros_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test various macro usage patterns."""
    test_file = rust_macros_project / "macro_usage.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Using standard library macros
fn standard_macros() {
    let name = "World";
    println!("Hello, {}!", name);

    let items = vec![1, 2, 3, 4, 5];
    println!("Items: {:?}", items);

    let config = std::collections::HashMap::from([
        ("debug", true),
        ("verbose", false),
    ]);

    eprintln!("Error occurred in function {}", function_name!());

    let formatted = format!("The answer is {}", 42);

    assert_eq!(2 + 2, 4);
    assert!(items.len() > 0);
    debug_assert!(config.contains_key("debug"));

    if cfg!(debug_assertions) {
        println!("Running in debug mode");
    }

    let source_info = format!("{}:{}", file!(), line!());
    println!("Source info: {}", source_info);
}

// Custom logging macro
macro_rules! log {
    (info $($arg:tt)*) => {
        println!("[INFO] {}", format!($($arg)*));
    };
    (warn $($arg:tt)*) => {
        println!("[WARN] {}", format!($($arg)*));
    };
    (error $($arg:tt)*) => {
        eprintln!("[ERROR] {}", format!($($arg)*));
    };
}

fn logging_example() {
    log!(info "Application started");
    log!(warn "Low disk space: {}%", 85);
    log!(error "Failed to connect to database: {}", "timeout");
}

// Macro for creating test suites
macro_rules! test_suite {
    ($suite_name:ident {
        $(
            $test_name:ident: $test_body:expr
        ),* $(,)?
    }) => {
        mod $suite_name {
            use super::*;

            $(
                #[test]
                fn $test_name() {
                    $test_body
                }
            )*
        }
    };
}

test_suite! {
    math_tests {
        addition: assert_eq!(2 + 2, 4),
        subtraction: assert_eq!(5 - 3, 2),
        multiplication: assert_eq!(3 * 4, 12),
    }
}

// Macro for creating configuration structs
macro_rules! config {
    (
        $config_name:ident {
            $(
                $field_name:ident: $field_type:ty = $default_value:expr
            ),* $(,)?
        }
    ) => {
        #[derive(Debug, Clone)]
        struct $config_name {
            $(
                pub $field_name: $field_type,
            )*
        }

        impl Default for $config_name {
            fn default() -> Self {
                Self {
                    $(
                        $field_name: $default_value,
                    )*
                }
            }
        }

        impl $config_name {
            pub fn new() -> Self {
                Self::default()
            }

            $(
                pub fn $field_name(mut self, value: $field_type) -> Self {
                    self.$field_name = value;
                    self
                }
            )*
        }
    };
}

config! {
    ServerConfig {
        host: String = "localhost".to_string(),
        port: u16 = 8080,
        max_connections: usize = 1000,
        enable_logging: bool = true,
        timeout_seconds: u64 = 30,
    }
}

fn config_usage() {
    let config = ServerConfig::new()
        .host("0.0.0.0".to_string())
        .port(3000)
        .max_connections(500);

    println!("Server config: {:?}", config);
}

// Macro for creating state machines
macro_rules! state_machine {
    (
        $machine_name:ident {
            states: { $($state:ident),* }
            initial: $initial_state:ident
            transitions: {
                $(
                    $from_state:ident -$event:ident-> $to_state:ident
                ),*
            }
        }
    ) => {
        #[derive(Debug, Clone, PartialEq)]
        enum State {
            $(
                $state,
            )*
        }

        #[derive(Debug, Clone)]
        enum Event {
            $(
                $event,
            )*
        }

        struct $machine_name {
            current_state: State,
        }

        impl $machine_name {
            pub fn new() -> Self {
                Self {
                    current_state: State::$initial_state,
                }
            }

            pub fn current_state(&self) -> &State {
                &self.current_state
            }

            pub fn handle_event(&mut self, event: Event) -> Result<(), String> {
                let next_state = match (&self.current_state, &event) {
                    $(
                        (State::$from_state, Event::$event) => State::$to_state,
                    )*
                    _ => return Err(format!(
                        "Invalid transition from {:?} with event {:?}",
                        self.current_state, event
                    )),
                };

                self.current_state = next_state;
                Ok(())
            }
        }
    };
}

state_machine! {
    OrderStateMachine {
        states: { Created, Paid, Shipped, Delivered, Cancelled }
        initial: Created
        transitions: {
            Created -Pay-> Paid,
            Paid -Ship-> Shipped,
            Shipped -Deliver-> Delivered,
            Created -Cancel-> Cancelled,
            Paid -Cancel-> Cancelled
        }
    }
}

fn state_machine_usage() {
    let mut order = OrderStateMachine::new();
    println!("Initial state: {:?}", order.current_state());

    order.handle_event(Event::Pay).unwrap();
    println!("After payment: {:?}", order.current_state());

    order.handle_event(Event::Ship).unwrap();
    println!("After shipping: {:?}", order.current_state());
}

// Conditional compilation with macros
macro_rules! platform_specific {
    () => {
        #[cfg(target_os = "windows")]
        fn get_path_separator() -> char {
            '\\'
        }

        #[cfg(target_os = "linux")]
        fn get_path_separator() -> char {
            '/'
        }

        #[cfg(target_os = "macos")]
        fn get_path_separator() -> char {
            '/'
        }

        #[cfg(not(any(target_os = "windows", target_os = "linux", target_os = "macos")))]
        fn get_path_separator() -> char {
            '/'
        }
    };
}

platform_specific!();

fn platform_example() {
    let separator = get_path_separator();
    println!("Path separator: '{}'", separator);
}
""",
    )

    run_updater(rust_macros_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    usage_calls = [
        call
        for call in calls
        if "logging_example" in str(call) or "ServerConfig" in str(call)
    ]
    assert len(usage_calls) > 0, "Macro usage functions should be detected"


def test_derive_macros_custom(
    rust_macros_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test custom derive macro implementations."""
    test_file = rust_macros_project / "derive_macros.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
#[derive(Builder)]
struct User {
    name: String,
    age: u32,
    email: Option<String>,
    active: bool,
}

#[derive(AutoDebug)]
struct Point {
    x: f64,
    y: f64,
}

#[derive(AutoDebug)]
struct UnitStruct;

fn builder_usage() {
    let user = User::builder()
        .name("Alice".to_string())
        .age(30)
        .email(Some("alice@example.com".to_string()))
        .active(true)
        .build()
        .unwrap();

    println!("Created user: {:?}", user);
}

fn debug_usage() {
    let point = Point { x: 1.5, y: 2.5 };
    let unit = UnitStruct;

    println!("Point: {:?}", point);
    println!("Unit: {:?}", unit);
}

// Using serde derive macros
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug)]
struct Config {
    server_name: String,
    port: u16,
    database_url: String,
    features: Vec<String>,
}

#[derive(Serialize, Deserialize, Debug)]
#[serde(tag = "type")]
enum Message {
    Text { content: String },
    Image { url: String, alt_text: Option<String> },
    File { filename: String, size: u64 },
}

fn serde_usage() {
    let config = Config {
        server_name: "MyApp".to_string(),
        port: 8080,
        database_url: "postgres://localhost/myapp".to_string(),
        features: vec!["feature1".to_string(), "feature2".to_string()],
    };

    let json = serde_json::to_string(&config).unwrap();
    println!("Config JSON: {}", json);

    let parsed: Config = serde_json::from_str(&json).unwrap();
    println!("Parsed config: {:?}", parsed);

    let text_msg = Message::Text {
        content: "Hello, world!".to_string(),
    };

    let img_msg = Message::Image {
        url: "https://example.com/image.png".to_string(),
        alt_text: Some("Example image".to_string()),
    };

    let messages = vec![text_msg, img_msg];
    let json = serde_json::to_string(&messages).unwrap();
    println!("Messages JSON: {}", json);
}

// Custom derive for implementing common traits
trait Summary {
    fn summarize(&self) -> String;
}

// This would be implemented as a proc macro derive
// #[derive(Summary)]
struct Article {
    title: String,
    author: String,
    content: String,
}

// Manual implementation for example
impl Summary for Article {
    fn summarize(&self) -> String {
        format!("{} by {}", self.title, self.author)
    }
}

struct Tweet {
    username: String,
    content: String,
    reply: bool,
    retweet: bool,
}

impl Summary for Tweet {
    fn summarize(&self) -> String {
        format!("@{}: {}", self.username, self.content)
    }
}

fn trait_usage() {
    let article = Article {
        title: "Rust Macros".to_string(),
        author: "Jane Doe".to_string(),
        content: "Macros are powerful...".to_string(),
    };

    let tweet = Tweet {
        username: "rustlang".to_string(),
        content: "Rust 1.70 is out!".to_string(),
        reply: false,
        retweet: false,
    };

    println!("Article summary: {}", article.summarize());
    println!("Tweet summary: {}", tweet.summarize());
}

// Benchmark attribute macro usage
#[benchmark]
fn fibonacci(n: u32) -> u64 {
    if n <= 1 {
        n as u64
    } else {
        fibonacci(n - 1) + fibonacci(n - 2)
    }
}

#[benchmark]
fn bubble_sort(mut arr: Vec<i32>) -> Vec<i32> {
    let len = arr.len();
    for i in 0..len {
        for j in 0..len - 1 - i {
            if arr[j] > arr[j + 1] {
                arr.swap(j, j + 1);
            }
        }
    }
    arr
}

fn benchmark_usage() {
    let fib_result = fibonacci(10);
    println!("Fibonacci(10) = {}", fib_result);

    let unsorted = vec![64, 34, 25, 12, 22, 11, 90];
    let sorted = bubble_sort(unsorted);
    println!("Sorted: {:?}", sorted);
}

// Memoization attribute macro usage
#[memoize]
fn expensive_calculation(n: u32) -> u64 {
    // Simulate expensive computation
    std::thread::sleep(std::time::Duration::from_millis(100));
    n as u64 * n as u64
}

fn memoize_usage() {
    println!("First call:");
    let result1 = expensive_calculation(5);
    println!("Result: {}", result1);

    println!("Second call (should be cached):");
    let result2 = expensive_calculation(5);
    println!("Result: {}", result2);
}

// Generated function using make_answer macro
make_answer!();

// Generated function using create_function macro
create_function!("hello_world");

fn generated_function_usage() {
    let ans = answer();
    println!("The answer is: {}", ans);

    hello_world();
}
""",
    )

    run_updater(rust_macros_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    derive_calls = [
        call
        for call in calls
        if "builder_usage" in str(call) or "serde_usage" in str(call)
    ]
    assert len(derive_calls) > 0, "Derive macro usage should be detected"


def test_advanced_macro_patterns(
    rust_macros_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test advanced macro programming patterns."""
    test_file = rust_macros_project / "advanced_macros.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Macro for creating DSLs (Domain Specific Languages)
macro_rules! html {
    ($tag:ident { $($attr:ident = $value:expr),* } [ $($content:tt)* ]) => {
        {
            let mut element = format!("<{}", stringify!($tag));
            $(
                element.push_str(&format!(" {}=\"{}\"", stringify!($attr), $value));
            )*
            element.push('>');
            element.push_str(&html!($($content)*));
            element.push_str(&format!("</{}>", stringify!($tag)));
            element
        }
    };

    ($tag:ident [ $($content:tt)* ]) => {
        {
            let mut element = format!("<{}>", stringify!($tag));
            element.push_str(&html!($($content)*));
            element.push_str(&format!("</{}>", stringify!($tag)));
            element
        }
    };

    ($text:literal) => {
        $text.to_string()
    };

    ($($token:tt)*) => {
        {
            let mut result = String::new();
            $(
                result.push_str(&html!($token));
            )*
            result
        }
    };
}

fn html_dsl_example() {
    let page = html! {
        html [
            head [
                title [ "My Page" ]
            ]
            body { class = "container" } [
                h1 { id = "main-title" } [ "Welcome" ]
                p [ "This is a paragraph." ]
                div { class = "content" } [
                    "Some content here"
                ]
            ]
        ]
    };

    println!("Generated HTML: {}", page);
}

// Macro for creating SQL-like queries
macro_rules! query {
    (SELECT $($field:ident),* FROM $table:ident WHERE $condition:expr) => {
        {
            let fields = vec![$(stringify!($field)),*];
            let table = stringify!($table);
            let query = format!(
                "SELECT {} FROM {} WHERE {}",
                fields.join(", "),
                table,
                $condition
            );
            query
        }
    };

    (INSERT INTO $table:ident ($($field:ident),*) VALUES ($($value:expr),*)) => {
        {
            let fields = vec![$(stringify!($field)),*];
            let values = vec![$(format!("'{}'", $value)),*];
            let table = stringify!($table);
            let query = format!(
                "INSERT INTO {} ({}) VALUES ({})",
                table,
                fields.join(", "),
                values.join(", ")
            );
            query
        }
    };
}

fn sql_dsl_example() {
    let select_query = query! {
        SELECT name, email, age FROM users WHERE "age > 18"
    };
    println!("Select query: {}", select_query);

    let insert_query = query! {
        INSERT INTO users (name, email, age) VALUES ("John Doe", "john@example.com", 25)
    };
    println!("Insert query: {}", insert_query);
}

// Macro for creating fluent APIs
macro_rules! fluent_api {
    (
        struct $name:ident {
            $($field:ident: $type:ty),*
        }
    ) => {
        struct $name {
            $($field: Option<$type>,)*
        }

        impl $name {
            fn new() -> Self {
                Self {
                    $($field: None,)*
                }
            }

            $(
                fn $field(mut self, value: $type) -> Self {
                    self.$field = Some(value);
                    self
                }
            )*

            fn build(self) -> Result<Built$name, String> {
                Ok(Built$name {
                    $($field: self.$field.ok_or_else(|| format!("Missing field: {}", stringify!($field)))?,)*
                })
            }
        }

        struct Built$name {
            $($field: $type,)*
        }

        impl Built$name {
            $(
                pub fn $field(&self) -> &$type {
                    &self.$field
                }
            )*
        }
    };
}

fluent_api! {
    struct HttpRequest {
        url: String,
        method: String,
        headers: std::collections::HashMap<String, String>,
        body: String
    }
}

fn fluent_api_example() {
    let mut headers = std::collections::HashMap::new();
    headers.insert("Content-Type".to_string(), "application/json".to_string());
    headers.insert("Authorization".to_string(), "Bearer token123".to_string());

    let request = HttpRequest::new()
        .url("https://api.example.com/users".to_string())
        .method("POST".to_string())
        .headers(headers)
        .body(r#"{"name": "John", "email": "john@example.com"}"#.to_string())
        .build()
        .unwrap();

    println!("Request URL: {}", request.url());
    println!("Request method: {}", request.method());
}

// Macro for creating compile-time checks
macro_rules! const_assert {
    ($condition:expr) => {
        const _: () = {
            if !$condition {
                panic!("Compile-time assertion failed");
            }
        };
    };

    ($condition:expr, $message:expr) => {
        const _: () = {
            if !$condition {
                panic!($message);
            }
        };
    };
}

// Compile-time checks
const_assert!(std::mem::size_of::<usize>() >= 4, "usize must be at least 4 bytes");
const_assert!(1 + 1 == 2);

// Macro for creating type-safe IDs
macro_rules! typed_id {
    ($name:ident) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        struct $name(u64);

        impl $name {
            pub fn new(id: u64) -> Self {
                $name(id)
            }

            pub fn get(&self) -> u64 {
                self.0
            }
        }

        impl From<u64> for $name {
            fn from(id: u64) -> Self {
                $name(id)
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                write!(f, "{}({})", stringify!($name), self.0)
            }
        }
    };
}

typed_id!(UserId);
typed_id!(ProductId);
typed_id!(OrderId);

fn typed_id_example() {
    let user_id = UserId::new(123);
    let product_id = ProductId::from(456);
    let order_id: OrderId = 789.into();

    println!("User: {}", user_id);
    println!("Product: {}", product_id);
    println!("Order: {}", order_id);

    // This would cause a compile error:
    // let same = user_id == product_id; // Type mismatch
}

// Macro for creating event systems
macro_rules! events {
    (
        $(
            $event:ident {
                $($field:ident: $type:ty),*
            }
        ),*
    ) => {
        #[derive(Debug, Clone)]
        enum Event {
            $(
                $event {
                    $($field: $type,)*
                },
            )*
        }

        $(
            impl Event {
                pub fn $event($($field: $type),*) -> Self {
                    Event::$event {
                        $($field,)*
                    }
                }
            }
        )*

        trait EventHandler {
            $(
                fn $event(&mut self, $($field: &$type),*) {}
            )*
        }

        struct EventBus {
            handlers: Vec<Box<dyn EventHandler>>,
        }

        impl EventBus {
            fn new() -> Self {
                EventBus {
                    handlers: Vec::new(),
                }
            }

            fn add_handler(&mut self, handler: Box<dyn EventHandler>) {
                self.handlers.push(handler);
            }

            fn dispatch(&mut self, event: Event) {
                match event {
                    $(
                        Event::$event { $($field),* } => {
                            for handler in &mut self.handlers {
                                handler.$event($(&$field),*);
                            }
                        },
                    )*
                }
            }
        }
    };
}

events! {
    UserCreated {
        id: u64,
        name: String,
        email: String
    },
    UserDeleted {
        id: u64
    },
    OrderPlaced {
        order_id: u64,
        user_id: u64,
        total: f64
    }
}

struct Logger;

impl EventHandler for Logger {
    fn user_created(&mut self, id: &u64, name: &String, email: &String) {
        println!("LOG: User created - ID: {}, Name: {}, Email: {}", id, name, email);
    }

    fn user_deleted(&mut self, id: &u64) {
        println!("LOG: User deleted - ID: {}", id);
    }

    fn order_placed(&mut self, order_id: &u64, user_id: &u64, total: &f64) {
        println!("LOG: Order placed - Order: {}, User: {}, Total: ${}", order_id, user_id, total);
    }
}

fn event_system_example() {
    let mut bus = EventBus::new();
    bus.add_handler(Box::new(Logger));

    let user_event = Event::user_created(1, "Alice".to_string(), "alice@example.com".to_string());
    let order_event = Event::order_placed(100, 1, 99.99);
    let delete_event = Event::user_deleted(1);

    bus.dispatch(user_event);
    bus.dispatch(order_event);
    bus.dispatch(delete_event);
}
""",
    )

    run_updater(rust_macros_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    advanced_calls = [
        call
        for call in calls
        if "html_dsl_example" in str(call) or "event_system_example" in str(call)
    ]
    assert len(advanced_calls) > 0, "Advanced macro functions should be detected"
