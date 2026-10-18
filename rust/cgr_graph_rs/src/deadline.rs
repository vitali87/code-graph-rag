//! A statement's time limit, as the engine's `limits.Deadline` keeps it.
//!
//! A caller's timeout only stops the caller waiting; the engine stops the
//! statement itself, checking its deadline between steps of every loop that
//! can run long. The Python side hands over the seconds left (a monotonic
//! reading does not cross processes or libraries), and the extension checks
//! at the points the Python implementations call `check_deadline()`.

use std::time::{Duration, Instant};

/// When the running statement's time is up.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Deadline {
    Never,
    At(Instant),
}

/// The deadline passed; the caller raises the engine's `QueryTimeout`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TimedOut;

/// Why a read of the graph stopped: SQLite failed, or time ran out.
#[derive(Debug)]
pub enum Stopped {
    Sql(rusqlite::Error),
    TimedOut,
}

impl From<rusqlite::Error> for Stopped {
    fn from(error: rusqlite::Error) -> Self {
        Stopped::Sql(error)
    }
}

impl From<TimedOut> for Stopped {
    fn from(_: TimedOut) -> Self {
        Stopped::TimedOut
    }
}

impl Deadline {
    /// `seconds` from now; None, or more than a Duration holds, is no limit,
    /// and nothing left (or less) has already passed.
    pub fn after(seconds: Option<f64>) -> Deadline {
        let now = Instant::now();
        match seconds {
            None => Deadline::Never,
            Some(s) if s <= 0.0 => Deadline::At(now),
            Some(s) => Duration::try_from_secs_f64(s)
                .ok()
                .and_then(|left| now.checked_add(left))
                .map_or(Deadline::Never, Deadline::At),
        }
    }

    pub fn passed(&self) -> bool {
        match self {
            Deadline::Never => false,
            Deadline::At(at) => Instant::now() >= *at,
        }
    }

    pub fn check(&self) -> Result<(), TimedOut> {
        if self.passed() {
            Err(TimedOut)
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Deadline, TimedOut};

    #[test]
    fn no_limit_never_passes() {
        assert_eq!(Deadline::after(None).check(), Ok(()));
        assert_eq!(Deadline::after(Some(f64::INFINITY)).check(), Ok(()));
    }

    #[test]
    fn nothing_left_has_passed() {
        assert_eq!(Deadline::after(Some(0.0)).check(), Err(TimedOut));
        assert_eq!(Deadline::after(Some(-3.0)).check(), Err(TimedOut));
    }

    #[test]
    fn time_left_has_not_passed() {
        assert_eq!(Deadline::after(Some(60.0)).check(), Ok(()));
    }
}
