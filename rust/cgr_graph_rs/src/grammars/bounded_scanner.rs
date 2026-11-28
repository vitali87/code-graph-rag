//! A copy of a grammar whose external scanner cannot write past the buffer
//! the runtime lends it (issue #3831).
//!
//! After each external token the runtime saves the scanner's state: it calls
//! the grammar's `serialize` with the parser's `lexer.debug_buffer`,
//! `TREE_SITTER_SERIALIZATION_BUFFER_SIZE` (1,024) bytes that end where the
//! parser's `Stack *` begins (`lexer.h`, `parser.c`). tree-sitter-python
//! 0.25.0's `serialize` (`src/scanner.c`, unchanged on upstream's master)
//! writes a flag, a count, a byte per open string, then two bytes per
//! indentation level, and checks `size < 1024` before each pair. With an odd
//! number of bytes ahead of the pairs, one string open, and 511 levels, it
//! writes a 1,025th byte, over the low byte of that pointer, and the parse
//! then pushes onto a stack at the wrong address and segfaults.
//!
//! `bounded` copies the grammar's `TSLanguage` with a `serialize` that has the
//! scanner write into a buffer with room to spare, and hands the runtime the
//! first 1,024 bytes. Python's `deserialize` reads the levels in whole pairs
//! and drops a lone last byte, so it restores what it does when the pairs
//! fill the buffer exactly: the nesting past it is lost, as it already was,
//! and nothing past the buffer is written. Below the buffer's end the copy
//! saves exactly the grammar's own bytes.

use std::ffi::{c_char, c_uint, c_void};
use std::ptr;
use std::sync::OnceLock;

/// `TREE_SITTER_SERIALIZATION_BUFFER_SIZE` (`parser.h`): what the runtime
/// lends `serialize`.
pub const SERIALIZATION_BUFFER: usize = 1024;

/// The language ABI `RawLanguage` lays out: `parser.h`'s
/// `TREE_SITTER_LANGUAGE_VERSION` from tree-sitter 0.25 on.
pub const ABI: u32 = 15;

/// What the scanner writes into: the buffer and as much again.
/// tree-sitter-python's writes at most 1,025 bytes: its last pair starts at
/// most at byte 1,023.
const SPARE: usize = 2 * SERIALIZATION_BUFFER;

pub(crate) type Serialize = unsafe extern "C" fn(*mut c_void, *mut c_char) -> c_uint;

/// `TSLanguage.external_scanner` (`parser.h`).
#[repr(C)]
#[derive(Clone, Copy)]
#[allow(dead_code)] // laid out to be copied whole, not read field by field
pub(crate) struct ExternalScanner {
    states: *const bool,
    symbol_map: *const u16,
    pub(crate) create: Option<unsafe extern "C" fn() -> *mut c_void>,
    pub(crate) destroy: Option<unsafe extern "C" fn(*mut c_void)>,
    scan: Option<unsafe extern "C" fn(*mut c_void, *mut c_void, *const bool) -> bool>,
    pub(crate) serialize: Option<Serialize>,
    pub(crate) deserialize: Option<unsafe extern "C" fn(*mut c_void, *const c_char, c_uint)>,
}

/// `TSLanguage` at ABI 15 (`parser.h`), field for field. It is copied whole,
/// so only its size and `external_scanner.serialize`'s offset matter; the
/// tests read the fields around it back through the runtime.
#[repr(C)]
#[derive(Clone, Copy)]
#[allow(dead_code)] // laid out to be copied whole, not read field by field
pub(crate) struct RawLanguage {
    pub(crate) abi_version: u32,
    symbol_count: u32,
    alias_count: u32,
    token_count: u32,
    external_token_count: u32,
    pub(crate) state_count: u32,
    large_state_count: u32,
    production_id_count: u32,
    pub(crate) field_count: u32,
    max_alias_sequence_length: u16,
    parse_table: *const u16,
    small_parse_table: *const u16,
    small_parse_table_map: *const u32,
    parse_actions: *const c_void,
    symbol_names: *const *const c_char,
    field_names: *const *const c_char,
    field_map_slices: *const c_void,
    field_map_entries: *const c_void,
    symbol_metadata: *const c_void,
    public_symbol_map: *const u16,
    alias_map: *const u16,
    alias_sequences: *const u16,
    lex_modes: *const c_void,
    lex_fn: Option<unsafe extern "C" fn(*mut c_void, u16) -> bool>,
    keyword_lex_fn: Option<unsafe extern "C" fn(*mut c_void, u16) -> bool>,
    keyword_capture_token: u16,
    pub(crate) external_scanner: ExternalScanner,
    primary_state_ids: *const u16,
    pub(crate) name: *const c_char,
    reserved_words: *const u16,
    max_reserved_word_set_size: u16,
    supertype_count: u32,
    supertype_symbols: *const u16,
    supertype_map_slices: *const c_void,
    supertype_map_entries: *const u16,
    /// `TSLanguageMetadata`: the major, minor and patch version.
    pub(crate) metadata: [u8; 3],
}

/// The grammar `bounded` copied, its own `serialize`, and the copy.
struct Bounded {
    original: *const RawLanguage,
    serialize: Serialize,
    copy: *const RawLanguage,
}

// SAFETY: every pointer is to a static (the grammar's, and the leaked copy),
// only ever read.
unsafe impl Send for Bounded {}
unsafe impl Sync for Bounded {}

/// One grammar per process: `bounded_serialize` is handed no language, so it
/// can call only one grammar's own `serialize`.
static BOUNDED: OnceLock<Bounded> = OnceLock::new();

unsafe extern "C" fn bounded_serialize(payload: *mut c_void, buffer: *mut c_char) -> c_uint {
    // Set before the copy, the only grammar that calls this, is handed out.
    let Some(bounded) = BOUNDED.get() else {
        return 0;
    };
    let mut spare: [c_char; SPARE] = [0; SPARE];
    // SAFETY: `payload` is the scanner the runtime made with the grammar's
    // own `create`, and `spare` holds more than its `serialize` writes.
    let written = unsafe { (bounded.serialize)(payload, spare.as_mut_ptr()) } as usize;
    let kept = written.min(SERIALIZATION_BUFFER);
    // SAFETY: `buffer` is the runtime's SERIALIZATION_BUFFER bytes, and
    // `kept` is no more than that or than `spare` holds.
    unsafe { ptr::copy_nonoverlapping(spare.as_ptr(), buffer, kept) };
    kept as c_uint
}

/// A copy of `raw` whose scanner writes nothing past the runtime's buffer, a
/// static that lives as long as this library. The same copy every time for
/// the same grammar; None when `raw` is not an ABI 15 grammar with an
/// external scanner, or is not the grammar bounded first.
///
/// # Safety
///
/// `raw` is null or a grammar's static `TSLanguage`, which lives as long as
/// the process.
pub unsafe fn bounded(raw: *const ()) -> Option<*const ()> {
    let raw = raw.cast::<RawLanguage>();
    if raw.is_null() {
        return None;
    }
    // The version first, alone: a grammar of another ABI may be shorter.
    // SAFETY: every ABI opens with the u32 version.
    if unsafe { ptr::read(raw.cast::<u32>()) } != ABI {
        return None;
    }
    // SAFETY: an ABI 15 grammar is laid out as `RawLanguage`.
    let language = unsafe { ptr::read(raw) };
    let serialize = language.external_scanner.serialize?;
    let bounded = BOUNDED.get_or_init(|| {
        let mut copy = language;
        copy.external_scanner.serialize = Some(bounded_serialize);
        Bounded {
            original: raw,
            serialize,
            copy: Box::leak(Box::new(copy)),
        }
    });
    (bounded.original == raw).then_some(bounded.copy.cast())
}
