from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater


def test_python_stdlib_introspection(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Python standard library import introspection."""
    project = temp_repo / "python_stdlib_test"
    project.mkdir()

    test_file = project / "stdlib_imports.py"
    test_file.write_text(
        encoding="utf-8",
        data="""
# Python standard library imports - these should resolve to module paths
from collections import defaultdict, Counter, OrderedDict
from datetime import datetime, timezone, timedelta
from json import loads, dumps, JSONEncoder
from pathlib import Path, PurePath
from typing import List, Dict, Optional, Union
from functools import lru_cache, wraps, partial
from itertools import chain, combinations, permutations
from os import path, environ, getcwd
from sys import argv, version, exit
import sqlite3
import urllib.parse
import xml.etree.ElementTree
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    stdlib_imports = [
        call for call in import_relationships if "stdlib_imports" in call.args[0][2]
    ]
    assert len(stdlib_imports) >= 10, (
        f"Expected at least 10 Python stdlib imports, found {len(stdlib_imports)}"
    )

    imported_modules = [call.args[2][2] for call in stdlib_imports]
    expected_modules = [
        "collections",
        "datetime",
        "json",
        "pathlib",
        "typing",
        "functools",
        "itertools",
        "os",
        "sys",
        "sqlite3",
        "urllib.parse",
        "xml.etree",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing Python module: {expected}"
        )


def test_javascript_stdlib_introspection(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Test JavaScript/Node.js standard library import introspection."""
    project = temp_repo / "js_stdlib_test"
    project.mkdir()

    test_file = project / "stdlib_imports.js"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Node.js standard library imports - should resolve to module paths
const fs = require('fs');
const { readFile, writeFile, createReadStream } = require('fs');
const path = require('path');
const { join, resolve, dirname, basename } = require('path');
const http = require('http');
const { createServer, request, get } = require('http');
const url = require('url');
const { parse, format } = require('url');
const crypto = require('crypto');
const { createHash, randomBytes } = require('crypto');
const events = require('events');
const { EventEmitter } = require('events');
const util = require('util');
const { promisify, inspect } = require('util');
const os = require('os');
const { platform, arch, cpus } = require('os');
const querystring = require('querystring');
const { parse: qsParse, stringify } = require('querystring');

// ES6 imports
import { promises } from 'fs';
import { URL, URLSearchParams } from 'url';
import { Worker, isMainThread } from 'worker_threads';

// Usage to ensure they're recognized
fs.readFileSync('test.txt');
path.join('/', 'home');
http.createServer();
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    js_imports = [
        call for call in import_relationships if "stdlib_imports" in call.args[0][2]
    ]
    assert len(js_imports) >= 8, (
        f"Expected at least 8 JavaScript stdlib imports, found {len(js_imports)}"
    )

    imported_modules = [call.args[2][2] for call in js_imports]
    expected_modules = [
        "fs",
        "path",
        "http",
        "url",
        "crypto",
        "events",
        "util",
        "os",
        "querystring",
        "worker_threads",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing JavaScript module: {expected}"
        )


def test_typescript_stdlib_introspection(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Test TypeScript standard library import introspection."""
    project = temp_repo / "ts_stdlib_test"
    project.mkdir()

    test_file = project / "stdlib_imports.ts"
    test_file.write_text(
        encoding="utf-8",
        data="""
// TypeScript standard library imports - should resolve to module paths
import * as fs from 'fs';
import { readFile, writeFile, promises as fsPromises } from 'fs';
import * as path from 'path';
import { join, resolve, dirname, basename, extname } from 'path';
import * as http from 'http';
import { createServer, IncomingMessage, ServerResponse } from 'http';
import * as https from 'https';
import { Agent } from 'https';
import * as url from 'url';
import { URL, URLSearchParams, parse, format } from 'url';
import * as crypto from 'crypto';
import { Hash, Cipher, createHash, randomBytes } from 'crypto';
import { EventEmitter } from 'events';
import * as util from 'util';
import { promisify, inspect, TextDecoder, TextEncoder } from 'util';
import * as os from 'os';
import { platform, arch, cpus, tmpdir, hostname } from 'os';
import * as stream from 'stream';
import { Readable, Writable, Transform, PassThrough } from 'stream';
import * as buffer from 'buffer';
import { Buffer } from 'buffer';

// TypeScript specific - built-in types and global objects
let json: JSON = JSON;
let math: Math = Math;
let date: DateConstructor = Date;
let array: ArrayConstructor = Array;
let object: ObjectConstructor = Object;
let promise: PromiseConstructor = Promise;

// Usage
const data = fs.readFileSync('test.txt');
const fullPath = path.join(__dirname, 'test.txt');
const server = http.createServer();
const hasher = crypto.createHash('sha256');
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    ts_imports = [
        call for call in import_relationships if "stdlib_imports" in call.args[0][2]
    ]
    assert len(ts_imports) >= 10, (
        f"Expected at least 10 TypeScript stdlib imports, found {len(ts_imports)}"
    )

    imported_modules = [call.args[2][2] for call in ts_imports]
    expected_modules = [
        "fs",
        "path",
        "http",
        "https",
        "url",
        "crypto",
        "events",
        "util",
        "os",
        "stream",
        "buffer",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing TypeScript module: {expected}"
        )


def test_go_stdlib_introspection(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Go standard library import introspection."""
    project = temp_repo / "go_stdlib_test"
    project.mkdir()

    test_file = project / "stdlib_imports.go"
    test_file.write_text(
        encoding="utf-8",
        data="""
package main

// Go standard library imports - should resolve to package paths
import (
    "fmt"
    "os"
    "io"
    "net/http"
    "encoding/json"
    "time"
    "strings"
    "strconv"
    "log"
    "context"
    "sync"
    "regexp"
    "math"
    "math/rand"
    "crypto/sha256"
    "database/sql"
    "net/url"
    "path/filepath"
    "bufio"
    "bytes"
    "errors"
    "sort"
    "reflect"
)

func main() {
    // Use imports to ensure they're recognized
    fmt.Println("Hello, World!")
    file, err := os.Open("test.txt")
    if err != nil {
        log.Fatal(err)
    }
    defer file.Close()

    resp, err := http.Get("https://example.com")
    if err == nil {
        defer resp.Body.Close()
    }

    data := map[string]interface{}{"key": "value"}
    jsonData, _ := json.Marshal(data)
    fmt.Println(string(jsonData))

    now := time.Now()
    str := strings.ToUpper("hello")
    num, _ := strconv.Atoi("123")

    ctx := context.Background()
    var mutex sync.Mutex
    matched, _ := regexp.MatchString(`\\d+`, "123")

    fmt.Printf("Time: %v, String: %s, Number: %d, Context: %v, Matched: %t\\n",
        now, str, num, ctx, matched)
}
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    go_imports = [
        call for call in import_relationships if "stdlib_imports" in call.args[0][2]
    ]
    assert len(go_imports) >= 15, (
        f"Expected at least 15 Go stdlib imports, found {len(go_imports)}"
    )

    imported_modules = [call.args[2][2] for call in go_imports]
    expected_packages = [
        "fmt",
        "os",
        "io",
        "net/http",
        "encoding/json",
        "time",
        "strings",
        "strconv",
        "log",
        "context",
        "sync",
        "regexp",
        "math",
        "crypto/sha256",
        "database/sql",
        "net/url",
        "path/filepath",
        "bufio",
        "bytes",
        "errors",
        "sort",
        "reflect",
    ]

    for expected in expected_packages:
        assert any(expected in module for module in imported_modules), (
            f"Missing Go package: {expected}"
        )


def test_rust_stdlib_introspection(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Rust standard library import introspection."""
    project = temp_repo / "rust_stdlib_test"
    project.mkdir()

    test_file = project / "stdlib_imports.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Rust standard library imports - should resolve to module paths
use std::collections::{HashMap, HashSet, BTreeMap, VecDeque};
use std::io::{self, Read, Write, BufReader, BufWriter, stdin, stdout};
use std::fs::{File, OpenOptions, read_to_string, write};
use std::net::{TcpListener, TcpStream, UdpSocket, SocketAddr};
use std::thread::{spawn, sleep, current, JoinHandle};
use std::sync::{Mutex, RwLock, Arc, mpsc, Condvar};
use std::fmt::{Display, Debug, Formatter, Result as FmtResult};
use std::str::{FromStr, from_utf8, parse};
use std::vec::Vec;
use std::string::String;
use std::option::{Option, Some, None};
use std::result::{Result, Ok, Err};
use std::convert::{From, Into, TryFrom, TryInto};
use std::iter::{Iterator, IntoIterator, once, repeat};
use std::time::{Duration, Instant, SystemTime};
use std::path::{Path, PathBuf};
use std::env::{args, var, current_dir};
use std::process::{Command, exit, id};
use std::mem::{size_of, align_of, drop, replace};
use std::ptr::{null, null_mut, NonNull};
use std::marker::{PhantomData, Send, Sync};
use std::cell::{Cell, RefCell};
use std::rc::{Rc, Weak as RcWeak};
use std::sync::{Arc, Weak as ArcWeak};

// External crates
extern crate serde;
use serde::{Serialize, Deserialize};

fn main() {
    // Use imports to ensure they're recognized
    let mut map: HashMap<String, i32> = HashMap::new();
    map.insert("key".to_string(), 42);

    let mut set = HashSet::new();
    set.insert("value");

    let path = Path::new("test.txt");
    let file = File::create(path);

    let vec: Vec<i32> = vec![1, 2, 3];
    let option: Option<i32> = Some(42);
    let result: Result<i32, &str> = Ok(100);

    let thread = spawn(|| {
        println!("Hello from thread!");
    });
    thread.join().unwrap();

    let duration = Duration::from_secs(1);
    let now = Instant::now();

    println!("Map: {:?}, Set: {:?}, Vec: {:?}", map, set, vec);
    println!("Duration: {:?}, Now: {:?}", duration, now);
}
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    rust_imports = [
        call for call in import_relationships if "stdlib_imports" in call.args[0][2]
    ]
    assert len(rust_imports) >= 15, (
        f"Expected at least 15 Rust stdlib imports, found {len(rust_imports)}"
    )

    imported_modules = [call.args[2][2] for call in rust_imports]
    expected_modules = [
        "std::collections",
        "std::io",
        "std::fs",
        "std::net",
        "std::thread",
        "std::sync",
        "std::fmt",
        "std::str",
        "std::vec",
        "std::string",
        "std::option",
        "std::result",
        "std::convert",
        "std::iter",
        "std::time",
        "std::path",
        "std::env",
        "std::process",
        "std::mem",
        "std::ptr",
        "std::marker",
        "std::cell",
        "std::rc",
        "serde",
    ]

    for expected in expected_modules:
        assert any(expected in module for module in imported_modules), (
            f"Missing Rust module: {expected}"
        )


def test_cpp_stdlib_introspection(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test C++ standard library import introspection with dynamic compilation."""
    project = temp_repo / "cpp_stdlib_test"
    project.mkdir()

    test_file = project / "stdlib_imports.cpp"
    test_file.write_text(
        encoding="utf-8",
        data="""
// C++ standard library includes - should be detected with dynamic compilation
#include <iostream>
#include <vector>
#include <string>
#include <map>
#include <unordered_map>
#include <set>
#include <unordered_set>
#include <deque>
#include <list>
#include <stack>
#include <queue>
#include <priority_queue>
#include <memory>
#include <algorithm>
#include <functional>
#include <thread>
#include <mutex>
#include <atomic>
#include <future>
#include <chrono>
#include <fstream>
#include <sstream>
#include <iomanip>
#include <regex>
#include <random>
#include <numeric>
#include <iterator>
#include <utility>
#include <tuple>
#include <array>
#include <type_traits>

int main() {
    // Use the stdlib types to verify dynamic compilation works
    std::vector<int> vec = {1, 2, 3};
    std::string str = "hello";
    std::map<int, std::string> map_obj;
    std::unique_ptr<int> ptr = std::make_unique<int>(42);
    std::thread t([]() { std::cout << "Thread\\n"; });
    t.join();

    std::sort(vec.begin(), vec.end());
    auto future = std::async(std::launch::async, []() { return 42; });
    int result = future.get();

    std::cout << "Vector size: " << vec.size() << ", Result: " << result << std::endl;
    return 0;
}
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    cpp_imports = [
        call for call in import_relationships if "stdlib_imports" in call.args[0][2]
    ]
    assert len(cpp_imports) >= 10, (
        f"Expected at least 10 C++ stdlib imports, found {len(cpp_imports)}"
    )

    imported_modules = [call.args[2][2] for call in cpp_imports]
    expected_headers = [
        "iostream",
        "vector",
        "string",
        "map",
        "memory",
        "algorithm",
        "functional",
        "thread",
        "mutex",
        "chrono",
        "fstream",
        "regex",
        "random",
        "atomic",
        "future",
    ]

    found_headers = 0
    for expected in expected_headers:
        if any(expected in module for module in imported_modules):
            found_headers += 1

    assert found_headers >= 8, f"Expected at least 8 C++ headers, found {found_headers}"


def test_java_stdlib_introspection(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Java standard library import introspection."""
    project = temp_repo / "java_stdlib_test"
    project.mkdir()

    test_file = project / "StdlibImports.java"
    test_file.write_text(
        encoding="utf-8",
        data="""
// Java standard library imports - should resolve to package paths
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.*;
import java.lang.*;
import java.io.*;
import java.nio.file.*;
import java.net.*;
import java.time.*;
import java.time.format.*;
import java.text.*;
import java.math.*;
import java.security.*;
import java.util.regex.*;
import java.util.function.*;

// Specific class imports that should resolve to packages
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.lang.String;
import java.lang.StringBuilder;
import java.io.File;
import java.io.FileReader;
import java.io.IOException;
import java.net.URL;
import java.net.URLConnection;
import java.time.LocalDateTime;
import java.time.Duration;
import java.math.BigDecimal;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.stream.Stream;
import java.util.function.Predicate;
import java.util.regex.Pattern;

public class StdlibImports {
    public static void main(String[] args) {
        ArrayList<String> list = new ArrayList<>();
        HashMap<String, Integer> map = new HashMap<>();
        HashSet<Integer> set = new HashSet<>();
        StringBuilder sb = new StringBuilder();
        File file = new File("test.txt");
        LocalDateTime now = LocalDateTime.now();
        BigDecimal decimal = new BigDecimal("123.45");
        ExecutorService executor = Executors.newFixedThreadPool(4);
        Stream<String> stream = list.stream();
        Predicate<String> predicate = s -> s.length() > 0;
        Pattern pattern = Pattern.compile("\\\\d+");

        System.out.println("Initialized all stdlib components");
    }
}
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    java_imports = [
        call for call in import_relationships if "StdlibImports" in call.args[0][2]
    ]
    assert len(java_imports) >= 15, (
        f"Expected at least 15 Java stdlib imports, found {len(java_imports)}"
    )

    imported_modules = [call.args[2][2] for call in java_imports]
    expected_packages = [
        "java.util",
        "java.lang",
        "java.io",
        "java.net",
        "java.time",
        "java.math",
        "java.util.concurrent",
        "java.util.stream",
        "java.util.function",
        "java.util.regex",
        "java.nio.file",
        "java.text",
        "java.security",
    ]

    for expected in expected_packages:
        assert any(expected in module for module in imported_modules), (
            f"Missing Java package: {expected}"
        )


def test_lua_stdlib_introspection(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """Test Lua standard library introspection resolves stdlib function calls to modules."""
    project = temp_repo / "lua_stdlib_test"
    project.mkdir()

    test_file = project / "stdlib_usage.lua"
    test_file.write_text(
        encoding="utf-8",
        data="""
-- Lua standard library usage - should resolve to module paths
local upper_str = string.upper("hello")
local lower_str = string.lower("WORLD")
local sub_str = string.sub("hello", 1, 3)
local find_pos = string.find("hello world", "world")
local gsub_result = string.gsub("hello", "l", "x")
local format_str = string.format("Number: %d", 42)

local floor_val = math.floor(3.14)
local ceil_val = math.ceil(2.1)
local sin_val = math.sin(math.pi / 2)
local max_val = math.max(1, 5, 3)
local min_val = math.min(1, 5, 3)
local random_val = math.random(1, 100)

local table_size = #{}
table.insert({}, "value")
table.remove({1, 2, 3}, 1)
local concat_result = table.concat({"a", "b", "c"}, ",")
table.sort({3, 1, 2})

local date_str = os.date("%Y-%m-%d")
local time_val = os.time()
local env_var = os.getenv("PATH")
local clock_val = os.clock()

local file = io.open("test.txt", "r")
if file then
    local content = io.read("*a")
    io.close(file)
end
local temp_file = io.tmpfile()

-- Debug module usage
local debug_info = debug.getinfo(1)
local debug_local = debug.getlocal(1, 1)

-- Package module usage
local package_path = package.path
local package_cpath = package.cpath

-- Custom module usage (should pass through)
local my_module = require("my_custom_module")
local another_module = require("utils.helper")
local result = my_module.my_function()

return {
    upper_str = upper_str,
    floor_val = floor_val,
    table_size = table_size,
    date_str = date_str,
    debug_info = debug_info,
    package_path = package_path
}
""",
    )

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    lua_imports = [
        call for call in import_relationships if "stdlib_usage" in call.args[0][2]
    ]
    assert len(lua_imports) >= 2, (
        f"Expected at least 2 Lua imports, found {len(lua_imports)}"
    )

    imported_modules = [call.args[2][2] for call in lua_imports]

    assert any("my_custom_module" in module for module in imported_modules), (
        "Expected custom module import"
    )
    assert any("utils.helper" in module for module in imported_modules), (
        "Expected utils.helper import"
    )

    expected_stdlib_modules = {
        "string",
        "math",
        "table",
        "os",
        "io",
        "debug",
        "package",
    }
    found_stdlib_modules = {
        module for module in imported_modules if module in expected_stdlib_modules
    }

    assert len(found_stdlib_modules) >= 3, (
        f"Expected at least 3 stdlib modules (string, math, table/os/io), "
        f"found: {found_stdlib_modules}"
    )

    assert "string" in found_stdlib_modules, (
        f"Expected 'string' module import for string.upper/lower calls, found: {found_stdlib_modules}"
    )
    assert "math" in found_stdlib_modules, (
        f"Expected 'math' module import for math.floor/ceil calls, found: {found_stdlib_modules}"
    )

    bad_imports = [
        module
        for module in imported_modules
        if any(
            bad in module
            for bad in [
                "string.upper",
                "math.floor",
                "table.insert",
                "os.date",
                "io.open",
            ]
        )
    ]
    assert not bad_imports, (
        f"Found unresolved qualified names that should be modules: {bad_imports}"
    )


def test_all_languages_stdlib_consistency(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Test that ALL languages handle stdlib imports consistently - no entity paths allowed."""
    project = temp_repo / "all_languages_consistency_test"
    project.mkdir()

    files_and_content = {
        "python_test.py": "from collections import defaultdict\\nfrom json import loads\\nfrom pathlib import Path",
        "javascript_test.js": "const { readFile } = require('fs');\\nconst { join } = require('path');",
        "typescript_test.ts": "import { readFile } from 'fs';\\nimport { join } from 'path';",
        "go_test.go": 'package main\\nimport (\\n\\t\\"fmt\\"\\n\\t\\"os\\"\\n)',
        "rust_test.rs": "use std::collections::HashMap;\\nuse std::io::Read;\\nuse std::fs::File;",
        "cpp_test.cpp": "#include <vector>\\n#include <string>\\n#include <map>",
        "java_test.java": "import java.util.ArrayList;\\nimport java.lang.String;",
        "lua_test.lua": "local my_module = require('my_module')\\nlocal result = my_module.func()",
    }

    for filename, content in files_and_content.items():
        (project / filename).write_text(encoding="utf-8", data=content)

    run_updater(project, mock_ingestor)

    import_relationships = get_relationships(mock_ingestor, "IMPORTS")

    assert len(import_relationships) >= 8, (
        f"Expected imports from all languages, found {len(import_relationships)}"
    )

    forbidden_entity_endings = [
        ".defaultdict",
        ".loads",
        ".Path",
        ".Counter",
        ".HashMap",
        ".ArrayList",
        ".String",
        ".Exception",
        ".IOException",
        ".StringBuilder",
        ".readFile",
        ".writeFile",
        ".join",
        ".resolve",
        ".createServer",
        ".get",
        ".request",
        ".EventEmitter",
        ".promisify",
        ".inspect",
        "/Print",
        "/Printf",
        "/Println",
        "/Open",
        "/Create",
        "/Marshal",
        "/Unmarshal",
        "/Get",
        "/Post",
        "/Now",
        "/Sleep",
        "::HashMap",
        "::HashSet",
        "::Vec",
        "::String",
        "::Read",
        "::Write",
        "::File",
        "::TcpListener",
        "::Mutex",
        "::Arc",
        "::vector",
        "::string",
        "::map",
        "::unique_ptr",
        "::shared_ptr",
        "::thread",
        "::mutex",
        "::sort",
        "::find",
        ".ArrayList",
        ".HashMap",
        ".String",
        ".File",
        ".URL",
        ".Pattern",
        ".LocalDateTime",
        ".BigDecimal",
        ".ExecutorService",
    ]

    for relationship in import_relationships:
        source_module = relationship.args[0][2]
        target_module = relationship.args[2][2]

        for forbidden_ending in forbidden_entity_endings:
            assert not target_module.endswith(forbidden_ending), (
                f"IMPORTS relationship incorrectly points to entity, not module: "
                f"{source_module} -> {target_module} (ends with {forbidden_ending})"
            )

    imported_modules = [call.args[2][2] for call in import_relationships]

    python_correct = any(
        "collections" in module and not module.endswith(".defaultdict")
        for module in imported_modules
    )

    java_correct = any(
        "java.util" in module and not module.endswith(".ArrayList")
        for module in imported_modules
    )

    rust_correct = any(
        "std::collections" in module and not module.endswith("::HashMap")
        for module in imported_modules
    )

    print(f"Found imported modules: {imported_modules}")
    print(
        f"Python correct: {python_correct}, Java correct: {java_correct}, Rust correct: {rust_correct}"
    )

    correct_count = sum([python_correct, java_correct, rust_correct])
    assert correct_count >= 1, (
        f"Expected at least 1 language to have correct stdlib introspection, "
        f"but found {correct_count} correct out of 3 tested"
    )
