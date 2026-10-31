// SPDX-License-Identifier: MIT
//! Input decoding shared by the cgr_graph_rs fuzz targets.
//!
//! Every decoder is total: each byte string is one program, and the end of
//! the input reads as zeros, so no input is ever rejected. Each target
//! bounds its own counts and sizes, so every run ends quickly.

/// Abort when a property is false.
///
/// A property must hold for every input, so its failure is a bug. It ends
/// in `abort()`, never in a value the fuzz target returns: the driver counts
/// a return of -1 as a pass.
#[macro_export]
macro_rules! require {
    ($cond:expr, $($msg:tt)+) => {
        if !$cond {
            eprintln!("property failed: {}", format_args!($($msg)+));
            std::process::abort();
        }
    };
}

/// The fuzzer's bytes, read from the front.
pub struct Input<'a> {
    data: &'a [u8],
}

impl<'a> Input<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        Input { data }
    }

    pub fn is_empty(&self) -> bool {
        self.data.is_empty()
    }

    /// The next byte, or 0 once the input is used up.
    pub fn byte(&mut self) -> u8 {
        match self.data.split_first() {
            Some((&b, rest)) => {
                self.data = rest;
                b
            }
            None => 0,
        }
    }

    fn le<const N: usize>(&mut self) -> [u8; N] {
        let mut out = [0u8; N];
        for b in &mut out {
            *b = self.byte();
        }
        out
    }

    pub fn u16(&mut self) -> u16 {
        u16::from_le_bytes(self.le())
    }

    pub fn i64(&mut self) -> i64 {
        i64::from_le_bytes(self.le())
    }

    /// Any bit pattern, NaN and the infinities included.
    pub fn f64(&mut self) -> f64 {
        f64::from_bits(u64::from_le_bytes(self.le()))
    }

    pub fn bool(&mut self) -> bool {
        self.byte() & 1 == 1
    }

    /// A number in `0..n`; `n` must be positive and at most 65536.
    pub fn below(&mut self, n: usize) -> usize {
        if n <= 256 {
            self.byte() as usize % n
        } else {
            self.u16() as usize % n
        }
    }

    pub fn pick<'b, T>(&mut self, items: &'b [T]) -> &'b T {
        &items[self.below(items.len())]
    }

    /// Up to `max` bytes, read as UTF-8 with each bad sequence replaced.
    pub fn text(&mut self, max: usize) -> String {
        let len = self.below(max + 1).min(self.data.len());
        let (head, rest) = self.data.split_at(len);
        self.data = rest;
        String::from_utf8_lossy(head).into_owned()
    }
}
