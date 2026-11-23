//! E-04 mock connectors: fixture loader + mail / calendar / sheet backends.

mod calendar;
mod fixtures;
mod mail;
mod sheet;

pub use calendar::{earliest_slot, find_free_slot, format_datetime, merge_busy, MockCalendar};
pub use fixtures::fixtures_root;
pub use mail::MockMail;
pub use sheet::MockSheet;
