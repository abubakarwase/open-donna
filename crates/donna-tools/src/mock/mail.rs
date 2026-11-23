use chrono::{DateTime, FixedOffset};
use serde_json::Value;

use crate::backend::{MailBackend, MailMessage, MessageSummary, SearchFilter, Thread};
use crate::mock::fixtures::load_mailbox;
use crate::{Result, ToolError};
use donna_core::Untrusted;

#[derive(Debug, Clone)]
pub struct MockMail {
    data: Value,
}

impl MockMail {
    pub fn from_ref(ref_name: &str) -> Result<Self> {
        Ok(Self {
            data: load_mailbox(ref_name)?,
        })
    }

    pub fn from_value(data: Value) -> Self {
        Self { data }
    }

    fn threads(&self) -> Result<&Vec<Value>> {
        self.data
            .get("threads")
            .and_then(|t| t.as_array())
            .ok_or_else(|| ToolError::Other("mailbox missing threads".into()))
    }
}

fn parse_dt(s: &str) -> Result<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(s)
        .or_else(|_| DateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S%.f%:z"))
        .map_err(|e| ToolError::Other(format!("bad datetime {s}: {e}")))
}

fn msg_in_range(msg_date: &str, after: Option<&str>, before: Option<&str>) -> Result<bool> {
    let md = parse_dt(msg_date)?;
    if let Some(a) = after {
        if md < parse_dt(a)? {
            return Ok(false);
        }
    }
    if let Some(b) = before {
        if md >= parse_dt(b)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn untrusted_str(v: &Value) -> Untrusted<String> {
    Untrusted::new(v.as_str().unwrap_or("").to_string())
}

impl MailBackend for MockMail {
    fn search(&self, filter: &SearchFilter) -> Result<Vec<MessageSummary>> {
        let q = filter
            .query
            .as_ref()
            .map(|s| s.to_lowercase())
            .unwrap_or_default();
        let mut out = Vec::new();
        for thread in self.threads()? {
            let tid = thread
                .get("thread_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let subject = thread
                .get("subject")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let messages = thread
                .get("messages")
                .and_then(|m| m.as_array())
                .cloned()
                .unwrap_or_default();
            let mut matched: Option<&Value> = None;
            for m in &messages {
                if filter.unread == Some(true)
                    && !m.get("unread").and_then(|u| u.as_bool()).unwrap_or(false)
                {
                    continue;
                }
                if let Some(from) = &filter.from {
                    if m.get("from").and_then(|f| f.as_str()) != Some(from.as_str()) {
                        continue;
                    }
                }
                if !msg_in_range(
                    m.get("date").and_then(|d| d.as_str()).unwrap_or(""),
                    filter.after.as_deref(),
                    filter.before.as_deref(),
                )? {
                    continue;
                }
                if !q.is_empty() {
                    let body = m.get("body").and_then(|b| b.as_str()).unwrap_or("");
                    let from = m.get("from").and_then(|f| f.as_str()).unwrap_or("");
                    let blob = format!("{subject} {body} {from}").to_lowercase();
                    if !blob.contains(&q) {
                        continue;
                    }
                }
                matched = Some(m);
                break;
            }
            if let Some(m) = matched {
                let body = m.get("body").and_then(|b| b.as_str()).unwrap_or("");
                let snippet: String = body.chars().take(120).collect();
                out.push(MessageSummary {
                    thread_id: tid,
                    message_id: m
                        .get("message_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                    subject: Untrusted::new(subject),
                    from: untrusted_str(m.get("from").unwrap_or(&Value::Null)),
                    date: m
                        .get("date")
                        .and_then(|d| d.as_str())
                        .unwrap_or("")
                        .to_string(),
                    unread: m.get("unread").and_then(|u| u.as_bool()).unwrap_or(false),
                    snippet: Some(Untrusted::new(snippet)),
                });
            }
        }
        out.sort_by(|a, b| a.thread_id.cmp(&b.thread_id));
        Ok(out)
    }

    fn thread(&self, thread_id: &str) -> Result<Thread> {
        for thread in self.threads()? {
            if thread.get("thread_id").and_then(|v| v.as_str()) != Some(thread_id) {
                continue;
            }
            let messages = thread
                .get("messages")
                .and_then(|m| m.as_array())
                .cloned()
                .unwrap_or_default();
            let msgs = messages
                .iter()
                .map(|m| MailMessage {
                    message_id: m
                        .get("message_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                    from: untrusted_str(m.get("from").unwrap_or(&Value::Null)),
                    to: m
                        .get("to")
                        .and_then(|t| t.as_array())
                        .map(|a| {
                            a.iter()
                                .filter_map(|v| v.as_str().map(str::to_string))
                                .collect()
                        })
                        .unwrap_or_default(),
                    cc: m
                        .get("cc")
                        .and_then(|t| t.as_array())
                        .map(|a| {
                            a.iter()
                                .filter_map(|v| v.as_str().map(str::to_string))
                                .collect()
                        })
                        .unwrap_or_default(),
                    date: m
                        .get("date")
                        .and_then(|d| d.as_str())
                        .unwrap_or("")
                        .to_string(),
                    body: untrusted_str(m.get("body").unwrap_or(&Value::Null)),
                    unread: m.get("unread").and_then(|u| u.as_bool()).unwrap_or(false),
                    labels: m
                        .get("labels")
                        .and_then(|t| t.as_array())
                        .map(|a| {
                            a.iter()
                                .filter_map(|v| v.as_str().map(str::to_string))
                                .collect()
                        })
                        .unwrap_or_default(),
                })
                .collect();
            return Ok(Thread {
                thread_id: thread_id.to_string(),
                subject: Untrusted::new(
                    thread
                        .get("subject")
                        .and_then(|s| s.as_str())
                        .unwrap_or("")
                        .to_string(),
                ),
                messages: msgs,
            });
        }
        Err(ToolError::Other(format!("thread not found: {thread_id}")))
    }

    fn thread_count(&self) -> usize {
        self.threads().map(|t| t.len()).unwrap_or(0)
    }
}
