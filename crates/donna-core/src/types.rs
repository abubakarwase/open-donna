use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Marks third-party / untrusted string content (mail bodies, invite titles, etc.).
/// Serializes as the inner value (transparent) so traces stay plain JSON strings.
/// Inner text is private so prompt paths must go through fencing helpers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Untrusted<T>(T);

impl<T> Untrusted<T> {
    pub fn new(inner: T) -> Self {
        Self(inner)
    }

    /// Reveal the inner value for UI display or tests. Do not feed into model prompts.
    pub fn into_inner_for_display(self) -> T {
        self.0
    }

    /// Borrow for UI display or tests. Do not feed into model prompts.
    pub fn as_display(&self) -> &T {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Input {
    pub text: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selection: Option<Selection>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Selection {
    pub kind: SelectionKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sheet: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub range: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email_ids: Option<Vec<String>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SelectionKind {
    SheetRange,
    Emails,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Context {
    pub now: String,
    pub timezone: String,
    pub fixtures: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteLabel {
    Email,
    Calendar,
    Sheet,
    Escalate,
    General,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Route {
    pub label: RouteLabel,
    pub confidence: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EscalateReason {
    LowConfidence,
    MultiStep,
    SchemaFailed,
    MessySheet,
    CrossSheetJoin,
    WhyOrForecast,
    Ambiguous,
    UserRequested,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    pub name: String,
    pub args: Value,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DraftKind {
    Email,
    Event,
    EventUpdate,
    EventCancel,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Draft {
    pub id: String,
    pub kind: DraftKind,
    pub payload: Value,
    /// Safety / guard flags (e.g. untrusted recipient). Empty by default for schema compat.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub flags: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Escalation {
    pub escalated: bool,
    pub reason: Option<String>,
    pub payload_bytes: u64,
    /// Consent state for the handoff: `local_only` / `requested` / `granted` / `denied`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub consent: Option<String>,
}

impl Escalation {
    pub fn none() -> Self {
        Self {
            escalated: false,
            reason: None,
            payload_bytes: 0,
            consent: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Trace {
    pub case_id: String,
    pub route: Route,
    pub tool_calls: Vec<ToolCall>,
    pub drafts: Vec<Draft>,
    pub escalation: Escalation,
    pub executed_side_effects: Vec<Value>,
    pub steps: u64,
    pub latency_ms: f64,
    pub peak_rss_bytes: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub answer: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl Trace {
    /// Build a minimal valid trace; SUT fills `case_id`, `latency_ms`, `peak_rss_bytes`.
    pub fn stub(route: Route) -> Self {
        Self {
            case_id: String::new(),
            route,
            tool_calls: Vec::new(),
            drafts: Vec::new(),
            escalation: Escalation::none(),
            executed_side_effects: Vec::new(),
            steps: 1,
            latency_ms: 0.0,
            peak_rss_bytes: 0,
            answer: None,
            error: None,
        }
    }

    pub fn with_case_id(mut self, case_id: impl Into<String>) -> Self {
        self.case_id = case_id.into();
        self
    }

    pub fn with_latency_ms(mut self, latency_ms: f64) -> Self {
        self.latency_ms = latency_ms;
        self
    }

    pub fn with_peak_rss_bytes(mut self, peak_rss_bytes: u64) -> Self {
        self.peak_rss_bytes = peak_rss_bytes;
        self
    }
}
