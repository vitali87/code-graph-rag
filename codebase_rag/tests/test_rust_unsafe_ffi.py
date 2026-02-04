from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import run_updater


@pytest.fixture
def rust_unsafe_project(temp_repo: Path) -> Path:
    """Create a Rust project structure for unsafe and FFI testing."""
    project_path = temp_repo / "rust_unsafe_test"
    project_path.mkdir()

    (project_path / "src").mkdir()
    (project_path / "src" / "lib.rs").write_text(
        encoding="utf-8", data="// Unsafe and FFI test crate"
    )

    (project_path / "Cargo.toml").write_text(
        encoding="utf-8",
        data="""[package]
name = "rust_unsafe_test"
version = "0.1.0"
edition = "2021"

[dependencies]
libc = "0.2"
""",
    )

    return project_path


def test_raw_pointers_and_dereferencing(
    rust_unsafe_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test raw pointer operations and unsafe dereferencing."""
    test_file = rust_unsafe_project / "raw_pointers.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::ptr;

// Raw pointer basic operations
fn raw_pointer_basics() {
    let mut x = 42i32;
    let raw_ptr: *mut i32 = &mut x as *mut i32;
    let const_ptr: *const i32 = &x as *const i32;

    unsafe {
        *raw_ptr = 100;
        let value = *const_ptr;
        println!("Value: {}", value);
    }
}

// Pointer arithmetic
unsafe fn pointer_arithmetic() {
    let arr = [1, 2, 3, 4, 5];
    let ptr = arr.as_ptr();

    for i in 0..arr.len() {
        let elem_ptr = ptr.add(i);
        let value = *elem_ptr;
        println!("Element {}: {}", i, value);
    }
}

// Manual memory management
struct RawBuffer {
    ptr: *mut u8,
    capacity: usize,
    len: usize,
}

impl RawBuffer {
    fn new(capacity: usize) -> Self {
        let layout = std::alloc::Layout::array::<u8>(capacity).unwrap();
        let ptr = unsafe { std::alloc::alloc(layout) };

        if ptr.is_null() {
            panic!("Failed to allocate memory");
        }

        RawBuffer {
            ptr,
            capacity,
            len: 0,
        }
    }

    unsafe fn push(&mut self, value: u8) {
        if self.len < self.capacity {
            let offset_ptr = self.ptr.add(self.len);
            *offset_ptr = value;
            self.len += 1;
        }
    }

    unsafe fn get(&self, index: usize) -> Option<u8> {
        if index < self.len {
            Some(*self.ptr.add(index))
        } else {
            None
        }
    }
}

impl Drop for RawBuffer {
    fn drop(&mut self) {
        if !self.ptr.is_null() {
            let layout = std::alloc::Layout::array::<u8>(self.capacity).unwrap();
            unsafe {
                std::alloc::dealloc(self.ptr, layout);
            }
        }
    }
}

// Null pointer handling
unsafe fn null_pointer_checks() {
    let null_ptr: *mut i32 = ptr::null_mut();

    if !null_ptr.is_null() {
        *null_ptr = 42; // This would segfault
    }

    // Safe null pointer creation and checking
    let maybe_ptr: Option<*mut i32> = None;
    match maybe_ptr {
        Some(ptr) if !ptr.is_null() => {
            *ptr = 100;
        }
        _ => println!("Null or invalid pointer"),
    }
}

// Raw pointer casting
unsafe fn pointer_casting() {
    let x = 42u64;
    let ptr = &x as *const u64;

    // Cast to different types
    let byte_ptr = ptr as *const u8;
    let void_ptr = ptr as *const std::ffi::c_void;
    let int_ptr = ptr as *const i32; // Potentially dangerous

    // Cast between mutability
    let mut_ptr = ptr as *mut u64;
    *mut_ptr = 100; // Undefined behavior if x is not mutable
}

// Volatile operations
unsafe fn volatile_operations() {
    let mut x = 42;
    let ptr = &mut x as *mut i32;

    // Volatile read/write (compiler won't optimize)
    ptr::write_volatile(ptr, 100);
    let value = ptr::read_volatile(ptr);
    println!("Volatile value: {}", value);
}

// Memory ordering and atomic operations
use std::sync::atomic::{AtomicUsize, Ordering};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

unsafe fn atomic_raw_operations() {
    let atomic_ptr = &COUNTER as *const AtomicUsize as *mut usize;

    // Direct memory manipulation (very unsafe)
    *atomic_ptr = 42;

    // Proper atomic operations
    COUNTER.store(100, Ordering::SeqCst);
    let value = COUNTER.load(Ordering::Acquire);
}
""",
    )

    run_updater(rust_unsafe_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    unsafe_calls = [
        call
        for call in calls
        if "pointer_arithmetic" in str(call) or "RawBuffer" in str(call)
    ]
    assert len(unsafe_calls) > 0, "Unsafe pointer functions should be detected"


def test_extern_c_functions(
    rust_unsafe_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test extern C function declarations and FFI patterns."""
    test_file = rust_unsafe_project / "extern_c.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::os::raw::{c_double, c_float, c_long, c_uchar};

// Basic extern C functions
extern "C" {
    fn malloc(size: usize) -> *mut c_void;
    fn free(ptr: *mut c_void);
    fn strlen(s: *const c_char) -> usize;
    fn strcpy(dest: *mut c_char, src: *const c_char) -> *mut c_char;
    fn printf(format: *const c_char, ...) -> c_int;
}

// Math library functions
extern "C" {
    fn sin(x: c_double) -> c_double;
    fn cos(x: c_double) -> c_double;
    fn sqrt(x: c_double) -> c_double;
    fn pow(base: c_double, exp: c_double) -> c_double;
}

// Custom C library binding
extern "C" {
    fn custom_init() -> c_int;
    fn custom_process(data: *const c_uchar, len: usize) -> c_int;
    fn custom_cleanup();
    fn custom_get_version() -> *const c_char;
}

// Rust functions callable from C
#[no_mangle]
pub extern "C" fn rust_add(a: c_int, b: c_int) -> c_int {
    a + b
}

#[no_mangle]
pub extern "C" fn rust_string_length(s: *const c_char) -> usize {
    if s.is_null() {
        return 0;
    }

    unsafe {
        let c_str = CStr::from_ptr(s);
        c_str.to_bytes().len()
    }
}

#[no_mangle]
pub extern "C" fn rust_allocate_array(size: usize) -> *mut c_int {
    if size == 0 {
        return std::ptr::null_mut();
    }

    let layout = std::alloc::Layout::array::<c_int>(size).unwrap();
    unsafe {
        let ptr = std::alloc::alloc(layout) as *mut c_int;
        if !ptr.is_null() {
            // Initialize to zero
            for i in 0..size {
                *ptr.add(i) = 0;
            }
        }
        ptr
    }
}

#[no_mangle]
pub extern "C" fn rust_free_array(ptr: *mut c_int, size: usize) {
    if !ptr.is_null() && size > 0 {
        let layout = std::alloc::Layout::array::<c_int>(size).unwrap();
        unsafe {
            std::alloc::dealloc(ptr as *mut u8, layout);
        }
    }
}

// Callback function types
type ProgressCallback = extern "C" fn(progress: c_double) -> c_int;
type ErrorCallback = extern "C" fn(error_code: c_int, message: *const c_char);

extern "C" {
    fn start_long_operation(
        progress_cb: Option<ProgressCallback>,
        error_cb: Option<ErrorCallback>,
    ) -> c_int;
}

// Safe wrapper functions
pub fn safe_malloc(size: usize) -> Option<*mut c_void> {
    unsafe {
        let ptr = malloc(size);
        if ptr.is_null() {
            None
        } else {
            Some(ptr)
        }
    }
}

pub fn safe_c_string_length(s: &str) -> usize {
    let c_string = CString::new(s).unwrap();
    unsafe { strlen(c_string.as_ptr()) }
}

pub fn safe_math_operations(x: f64) -> (f64, f64, f64) {
    unsafe {
        (sin(x), cos(x), sqrt(x.abs()))
    }
}

// Complex FFI structure
#[repr(C)]
pub struct CComplexStruct {
    pub id: c_int,
    pub name: *mut c_char,
    pub data: *mut c_double,
    pub data_len: usize,
    pub flags: c_int,
}

extern "C" {
    fn process_complex_struct(s: *const CComplexStruct) -> c_int;
    fn create_complex_struct() -> *mut CComplexStruct;
    fn destroy_complex_struct(s: *mut CComplexStruct);
}

impl CComplexStruct {
    pub fn from_rust_data(id: i32, name: &str, data: &[f64]) -> Self {
        let c_name = CString::new(name).unwrap().into_raw();
        let c_data = data.as_ptr() as *mut c_double;

        CComplexStruct {
            id,
            name: c_name,
            data: c_data,
            data_len: data.len(),
            flags: 0,
        }
    }
}
""",
    )

    run_updater(rust_unsafe_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    extern_calls = [
        call
        for call in calls
        if "extern" in str(call)
        or "rust_add" in str(call)
        or "CComplexStruct" in str(call)
    ]
    assert len(extern_calls) > 0, "Extern C functions should be detected"


def test_unsafe_traits_and_implementations(
    rust_unsafe_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test unsafe traits and their implementations."""
    test_file = rust_unsafe_project / "unsafe_traits.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::marker::PhantomData;
use std::ptr::NonNull;

// Unsafe trait for raw memory operations
unsafe trait RawMemory {
    unsafe fn read_raw(&self, offset: usize) -> u8;
    unsafe fn write_raw(&mut self, offset: usize, value: u8);
    fn size(&self) -> usize;
}

// Raw byte buffer implementation
struct RawByteBuffer {
    ptr: NonNull<u8>,
    len: usize,
    capacity: usize,
}

unsafe impl RawMemory for RawByteBuffer {
    unsafe fn read_raw(&self, offset: usize) -> u8 {
        if offset < self.len {
            *self.ptr.as_ptr().add(offset)
        } else {
            0
        }
    }

    unsafe fn write_raw(&mut self, offset: usize, value: u8) {
        if offset < self.capacity {
            *self.ptr.as_ptr().add(offset) = value;
            if offset >= self.len {
                self.len = offset + 1;
            }
        }
    }

    fn size(&self) -> usize {
        self.len
    }
}

impl RawByteBuffer {
    fn new(capacity: usize) -> Self {
        let layout = std::alloc::Layout::array::<u8>(capacity).unwrap();
        let ptr = unsafe { std::alloc::alloc(layout) };

        RawByteBuffer {
            ptr: NonNull::new(ptr).expect("Failed to allocate memory"),
            len: 0,
            capacity,
        }
    }
}

impl Drop for RawByteBuffer {
    fn drop(&mut self) {
        let layout = std::alloc::Layout::array::<u8>(self.capacity).unwrap();
        unsafe {
            std::alloc::dealloc(self.ptr.as_ptr(), layout);
        }
    }
}

// Unsafe Send and Sync implementations
struct UnsafeSharedPtr<T> {
    ptr: *const T,
    _phantom: PhantomData<T>,
}

unsafe impl<T> Send for UnsafeSharedPtr<T> {}
unsafe impl<T> Sync for UnsafeSharedPtr<T> {}

impl<T> UnsafeSharedPtr<T> {
    unsafe fn new(ptr: *const T) -> Self {
        UnsafeSharedPtr {
            ptr,
            _phantom: PhantomData,
        }
    }

    unsafe fn deref(&self) -> &T {
        &*self.ptr
    }
}

// Custom allocator trait
unsafe trait Allocator {
    unsafe fn allocate(&self, size: usize, align: usize) -> *mut u8;
    unsafe fn deallocate(&self, ptr: *mut u8, size: usize, align: usize);

    unsafe fn reallocate(
        &self,
        ptr: *mut u8,
        old_size: usize,
        new_size: usize,
        align: usize,
    ) -> *mut u8 {
        let new_ptr = self.allocate(new_size, align);
        if !new_ptr.is_null() && !ptr.is_null() {
            std::ptr::copy_nonoverlapping(ptr, new_ptr, old_size.min(new_size));
            self.deallocate(ptr, old_size, align);
        }
        new_ptr
    }
}

// Simple bump allocator
struct BumpAllocator {
    memory: *mut u8,
    offset: std::cell::Cell<usize>,
    size: usize,
}

unsafe impl Allocator for BumpAllocator {
    unsafe fn allocate(&self, size: usize, align: usize) -> *mut u8 {
        let current_offset = self.offset.get();
        let aligned_offset = (current_offset + align - 1) & !(align - 1);

        if aligned_offset + size <= self.size {
            self.offset.set(aligned_offset + size);
            self.memory.add(aligned_offset)
        } else {
            std::ptr::null_mut()
        }
    }

    unsafe fn deallocate(&self, _ptr: *mut u8, _size: usize, _align: usize) {
        // Bump allocator doesn't support individual deallocation
    }
}

impl BumpAllocator {
    fn new(size: usize) -> Self {
        let layout = std::alloc::Layout::from_size_align(size, 8).unwrap();
        let memory = unsafe { std::alloc::alloc(layout) };

        BumpAllocator {
            memory,
            offset: std::cell::Cell::new(0),
            size,
        }
    }

    fn reset(&self) {
        self.offset.set(0);
    }
}

impl Drop for BumpAllocator {
    fn drop(&mut self) {
        if !self.memory.is_null() {
            let layout = std::alloc::Layout::from_size_align(self.size, 8).unwrap();
            unsafe {
                std::alloc::dealloc(self.memory, layout);
            }
        }
    }
}

// Unsafe trait for zero-copy serialization
unsafe trait ZeroCopy: Copy {
    fn as_bytes(&self) -> &[u8] {
        unsafe {
            std::slice::from_raw_parts(
                self as *const Self as *const u8,
                std::mem::size_of::<Self>(),
            )
        }
    }

    unsafe fn from_bytes(bytes: &[u8]) -> Option<&Self> {
        if bytes.len() >= std::mem::size_of::<Self>() {
            Some(&*(bytes.as_ptr() as *const Self))
        } else {
            None
        }
    }
}

// Safe types that implement ZeroCopy
#[repr(C)]
#[derive(Copy, Clone)]
struct Point {
    x: f32,
    y: f32,
}

unsafe impl ZeroCopy for Point {}

#[repr(C)]
#[derive(Copy, Clone)]
struct Color {
    r: u8,
    g: u8,
    b: u8,
    a: u8,
}

unsafe impl ZeroCopy for Color {}

// Function using unsafe trait bounds
fn serialize_zerocopy<T: ZeroCopy>(value: &T) -> Vec<u8> {
    value.as_bytes().to_vec()
}

unsafe fn deserialize_zerocopy<T: ZeroCopy>(bytes: &[u8]) -> Option<T> {
    T::from_bytes(bytes).copied()
}
""",
    )

    run_updater(rust_unsafe_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    unsafe_trait_calls = [
        call
        for call in calls
        if "RawMemory" in str(call)
        or "Allocator" in str(call)
        or "ZeroCopy" in str(call)
    ]
    assert len(unsafe_trait_calls) > 0, "Unsafe traits should be detected"


def test_inline_assembly(
    rust_unsafe_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test inline assembly and low-level operations."""
    test_file = rust_unsafe_project / "inline_asm.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::arch::asm;

// Basic inline assembly
unsafe fn inline_assembly_basic() {
    let mut x: u64 = 42;

    asm!(
        "add {0}, 1",
        inout(reg) x,
    );

    println!("Result: {}", x);
}

// Assembly with multiple operands
unsafe fn inline_assembly_multiple() {
    let a: u64 = 10;
    let b: u64 = 20;
    let mut result: u64;

    asm!(
        "add {result}, {a}, {b}",
        a = in(reg) a,
        b = in(reg) b,
        result = out(reg) result,
    );

    println!("Sum: {}", result);
}

// Memory operations with assembly
unsafe fn memory_assembly() {
    let src = [1u32, 2, 3, 4];
    let mut dst = [0u32; 4];

    asm!(
        "mov {tmp}, {src}",
        "mov {dst}, {tmp}",
        src = in(reg) src.as_ptr(),
        dst = in(reg) dst.as_mut_ptr(),
        tmp = out(reg) _,
        options(nostack, preserves_flags)
    );
}

// CPU feature detection
fn cpu_features() {
    unsafe {
        let mut eax: u32;
        let mut ebx: u32;
        let mut ecx: u32;
        let mut edx: u32;

        asm!(
            "cpuid",
            inout("eax") 1u32 => eax,
            out("ebx") ebx,
            out("ecx") ecx,
            out("edx") edx,
        );

        let sse_support = (edx >> 25) & 1;
        let sse2_support = (edx >> 26) & 1;

        println!("SSE: {}, SSE2: {}", sse_support, sse2_support);
    }
}

// Atomic operations with assembly
unsafe fn atomic_assembly() {
    let mut value: u32 = 0;
    let increment: u32 = 1;

    asm!(
        "lock xadd {value}, {increment}",
        value = inout(reg) value,
        increment = in(reg) increment,
        options(nostack, preserves_flags)
    );

    println!("Atomic result: {}", value);
}

// System calls with assembly
unsafe fn system_call_write(fd: i32, buf: *const u8, count: usize) -> isize {
    let mut result: isize;

    asm!(
        "syscall",
        inout("rax") 1i64 => result, // sys_write
        in("rdi") fd,
        in("rsi") buf,
        in("rdx") count,
        out("rcx") _,
        out("r11") _,
        options(nostack, preserves_flags)
    );

    result
}

// SIMD operations with inline assembly
unsafe fn simd_assembly() {
    let a = [1.0f32, 2.0, 3.0, 4.0];
    let b = [5.0f32, 6.0, 7.0, 8.0];
    let mut result = [0.0f32; 4];

    asm!(
        "movups xmm0, [{a}]",
        "movups xmm1, [{b}]",
        "addps xmm0, xmm1",
        "movups [{result}], xmm0",
        a = in(reg) a.as_ptr(),
        b = in(reg) b.as_ptr(),
        result = in(reg) result.as_mut_ptr(),
        out("xmm0") _,
        out("xmm1") _,
        options(nostack, preserves_flags)
    );

    println!("SIMD result: {:?}", result);
}

// Context switching assembly
unsafe fn context_switch() {
    let mut old_stack: *mut u8;
    let new_stack: *mut u8 = std::ptr::null_mut();

    asm!(
        "mov {old_stack}, rsp",
        "mov rsp, {new_stack}",
        old_stack = out(reg) old_stack,
        new_stack = in(reg) new_stack,
        options(nostack)
    );
}

// Performance counter access
unsafe fn read_performance_counter() -> u64 {
    let mut low: u32;
    let mut high: u32;

    asm!(
        "rdtsc",
        out("eax") low,
        out("edx") high,
        options(nostack, preserves_flags, readonly)
    );

    ((high as u64) << 32) | (low as u64)
}

// Memory barriers and synchronization
unsafe fn memory_barriers() {
    asm!("mfence", options(nostack, preserves_flags));
    asm!("lfence", options(nostack, preserves_flags));
    asm!("sfence", options(nostack, preserves_flags));
}

// Platform-specific assembly functions
#[cfg(target_arch = "x86_64")]
unsafe fn x86_64_specific() {
    let mut value: u64 = 0;

    asm!(
        "mov rax, cr0",
        "mov {value}, rax",
        value = out(reg) value,
        options(nostack, preserves_flags)
    );
}

#[cfg(target_arch = "aarch64")]
unsafe fn aarch64_specific() {
    let mut value: u64;

    asm!(
        "mrs {value}, sp_el0",
        value = out(reg) value,
        options(nostack, preserves_flags)
    );
}
""",
    )

    run_updater(rust_unsafe_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    asm_calls = [
        call
        for call in calls
        if "inline_assembly" in str(call) or "simd_assembly" in str(call)
    ]
    assert len(asm_calls) > 0, "Inline assembly functions should be detected"


def test_unsafe_unions_and_transmute(
    rust_unsafe_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test unsafe unions and transmute operations."""
    test_file = rust_unsafe_project / "unions_transmute.rs"
    test_file.write_text(
        encoding="utf-8",
        data="""
use std::mem;

// Basic union definition
#[repr(C)]
union FloatBits {
    float_val: f32,
    int_val: u32,
    bytes: [u8; 4],
}

impl FloatBits {
    fn new_float(f: f32) -> Self {
        FloatBits { float_val: f }
    }

    fn new_int(i: u32) -> Self {
        FloatBits { int_val: i }
    }

    unsafe fn as_float(&self) -> f32 {
        self.float_val
    }

    unsafe fn as_int(&self) -> u32 {
        self.int_val
    }

    unsafe fn as_bytes(&self) -> [u8; 4] {
        self.bytes
    }
}

// Union for type punning
#[repr(C)]
union TypePunning {
    as_u64: u64,
    as_i64: i64,
    as_f64: f64,
    as_bytes: [u8; 8],
    as_words: [u32; 2],
}

unsafe fn demonstrate_type_punning() {
    let mut punning = TypePunning { as_f64: 3.14159 };

    // Access as different types
    let float_bits = punning.as_u64;
    let byte_representation = punning.as_bytes;
    let word_representation = punning.as_words;

    println!("Float as bits: 0x{:016x}", float_bits);
    println!("Float as bytes: {:?}", byte_representation);
    println!("Float as words: {:?}", word_representation);
}

// Transmute operations
unsafe fn transmute_operations() {
    // Basic transmute
    let x: u32 = 42;
    let y: i32 = mem::transmute(x);
    println!("u32 {} as i32: {}", x, y);

    // Transmute slice to different element type
    let bytes: &[u8] = &[1, 2, 3, 4, 5, 6, 7, 8];
    let words: &[u32] = mem::transmute(bytes);
    println!("Bytes as words: {:?}", words);

    // Transmute function pointers
    let fn_ptr: fn() = || println!("Hello");
    let raw_ptr: *const () = mem::transmute(fn_ptr);
    let back_to_fn: fn() = mem::transmute(raw_ptr);
    back_to_fn();
}

// Safe wrapper for transmute
fn safe_transmute_u32_to_f32(x: u32) -> f32 {
    unsafe { mem::transmute(x) }
}

fn safe_transmute_f32_to_u32(x: f32) -> u32 {
    unsafe { mem::transmute(x) }
}

// Transmute with lifetime manipulation (very dangerous)
unsafe fn transmute_lifetimes<'a, 'b>(x: &'a str) -> &'b str {
    mem::transmute(x)
}

// Union with generic types
#[repr(C)]
union GenericUnion<T, U> {
    variant_a: T,
    variant_b: U,
}

impl<T, U> GenericUnion<T, U> {
    unsafe fn new_a(value: T) -> Self {
        GenericUnion { variant_a: value }
    }

    unsafe fn new_b(value: U) -> Self {
        GenericUnion { variant_b: value }
    }

    unsafe fn get_a(&self) -> &T {
        &self.variant_a
    }

    unsafe fn get_b(&self) -> &U {
        &self.variant_b
    }
}

// Tagged union implementation
#[repr(C)]
struct TaggedUnion {
    tag: u8,
    data: UnionData,
}

#[repr(C)]
union UnionData {
    int_val: i32,
    float_val: f32,
    bool_val: bool,
    char_val: char,
}

impl TaggedUnion {
    fn new_int(value: i32) -> Self {
        TaggedUnion {
            tag: 0,
            data: UnionData { int_val: value },
        }
    }

    fn new_float(value: f32) -> Self {
        TaggedUnion {
            tag: 1,
            data: UnionData { float_val: value },
        }
    }

    fn new_bool(value: bool) -> Self {
        TaggedUnion {
            tag: 2,
            data: UnionData { bool_val: value },
        }
    }

    unsafe fn as_int(&self) -> Option<i32> {
        if self.tag == 0 {
            Some(self.data.int_val)
        } else {
            None
        }
    }

    unsafe fn as_float(&self) -> Option<f32> {
        if self.tag == 1 {
            Some(self.data.float_val)
        } else {
            None
        }
    }

    unsafe fn as_bool(&self) -> Option<bool> {
        if self.tag == 2 {
            Some(self.data.bool_val)
        } else {
            None
        }
    }
}

// Memory layout manipulation
#[repr(C, packed)]
struct PackedStruct {
    a: u8,
    b: u64,
    c: u16,
}

unsafe fn packed_struct_operations() {
    let packed = PackedStruct { a: 1, b: 2, c: 3 };

    // Accessing packed fields requires unsafe
    let a = packed.a;
    let b = packed.b; // This might be misaligned
    let c = packed.c;

    println!("Packed: a={}, b={}, c={}", a, b, c);
}

// Transmute for zero-cost conversions
#[repr(transparent)]
struct NewType(u32);

fn zero_cost_conversion(x: u32) -> NewType {
    unsafe { mem::transmute(x) }
}

// Bit manipulation with unions
#[repr(C)]
union BitField {
    value: u32,
    bits: BitFieldBits,
}

#[repr(C)]
struct BitFieldBits {
    low: u16,
    high: u16,
}

unsafe fn bit_field_operations() {
    let mut bf = BitField { value: 0x12345678 };

    let low_bits = bf.bits.low;
    let high_bits = bf.bits.high;

    bf.bits.low = 0xABCD;
    let new_value = bf.value;

    println!("Original: 0x{:08x}", 0x12345678u32);
    println!("Low: 0x{:04x}, High: 0x{:04x}", low_bits, high_bits);
    println!("Modified: 0x{:08x}", new_value);
}
""",
    )

    run_updater(rust_unsafe_project, mock_ingestor)
    calls = mock_ingestor.method_calls

    union_calls = [
        call
        for call in calls
        if "FloatBits" in str(call)
        or "transmute" in str(call)
        or "TaggedUnion" in str(call)
    ]
    assert len(union_calls) > 0, "Union and transmute operations should be detected"
