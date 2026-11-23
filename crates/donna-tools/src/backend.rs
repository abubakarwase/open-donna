//! Backend traits for mail, calendar, and sheet connectors.

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;

use donna_core::Untrusted;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MessageSummary {
    pub thread_id: String,
    pub message_id: String,
    pub subject: Untrusted<String>,
    pub from: Untrusted<String>,
    pub date: String,
    pub unread: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snippet: Option<Untrusted<String>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MailMessage {
    pub message_id: String,
    pub from: Untrusted<String>,
    pub to: Vec<String>,
    #[serde(default)]
    pub cc: Vec<String>,
    pub date: String,
    pub body: Untrusted<String>,
    pub unread: bool,
    #[serde(default)]
    pub labels: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Thread {
    pub thread_id: String,
    pub subject: Untrusted<String>,
    pub messages: Vec<MailMessage>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub event_id: String,
    pub title: Untrusted<String>,
    pub start: String,
    pub end: String,
    #[serde(default)]
    pub attendees: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recurrence: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub all_day: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub location: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FreeBusyBlock {
    pub start: String,
    pub end: String,
}

pub type FreeBusyMap = HashMap<String, Vec<FreeBusyBlock>>;

#[derive(Debug, Clone, Default)]
pub struct SearchFilter {
    pub query: Option<String>,
    pub from: Option<String>,
    pub unread: Option<bool>,
    pub after: Option<String>,
    pub before: Option<String>,
}

pub trait MailBackend {
    fn search(&self, filter: &SearchFilter) -> crate::Result<Vec<MessageSummary>>;
    fn thread(&self, thread_id: &str) -> crate::Result<Thread>;
    fn thread_count(&self) -> usize;
}

pub trait CalendarBackend {
    fn events(
        &self,
        range_start: &DateTime<FixedOffset>,
        range_end: &DateTime<FixedOffset>,
        timezone: &str,
    ) -> crate::Result<Vec<Event>>;
    fn freebusy(
        &self,
        attendees: &[String],
        range_start: &DateTime<FixedOffset>,
        range_end: &DateTime<FixedOffset>,
    ) -> crate::Result<FreeBusyMap>;
    fn event_count(&self) -> usize;
    fn timezone(&self) -> &str;
    /// Expanded busy intervals for the owner (merged) in the given timezone.
    fn owner_busy(
        &self,
        after: &DateTime<FixedOffset>,
        before: &DateTime<FixedOffset>,
        timezone: &str,
    ) -> crate::Result<Vec<(DateTime<FixedOffset>, DateTime<FixedOffset>)>>;
}

pub trait SheetBackend {
    fn path(&self, sheet_ref: &str) -> crate::Result<PathBuf>;
    /// Optional profile helper; default loads via [`crate::load_workbook`].
    fn profile(&self, sheet_ref: &str) -> crate::Result<crate::SheetProfile> {
        let path = self.path(sheet_ref)?;
        let loaded = crate::load_workbook(&path)?;
        crate::build_profile(&loaded)
    }
}
