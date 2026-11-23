/// When (if ever) a frontier handoff is allowed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ConsentPolicy {
    /// Never call a frontier provider; still record the escalate handoff.
    LocalOnly,
    /// Ask via [`Consent`] before any provider call.
    #[default]
    AskEachTime,
    /// Treat consent as granted; call provider when present.
    Auto,
}

/// Opaque consent gate for escalate handoffs (mirrors [`crate::Approver`]).
pub trait Consent {
    fn allow_escalate(&self, reason: &str, payload_bytes: u64) -> bool;
}

/// Always denies escalate consent (evals / SUT default).
#[derive(Debug, Default, Clone, Copy)]
pub struct DenyConsent;

impl Consent for DenyConsent {
    fn allow_escalate(&self, _reason: &str, _payload_bytes: u64) -> bool {
        false
    }
}

/// Always grants escalate consent (tests only).
#[cfg(any(test, feature = "test-approver"))]
#[derive(Debug, Default, Clone, Copy)]
pub struct GrantConsent;

#[cfg(any(test, feature = "test-approver"))]
impl Consent for GrantConsent {
    fn allow_escalate(&self, _reason: &str, _payload_bytes: u64) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deny_and_grant() {
        assert!(!DenyConsent.allow_escalate("messy_sheet", 10));
        assert!(GrantConsent.allow_escalate("messy_sheet", 10));
        assert_eq!(ConsentPolicy::default(), ConsentPolicy::AskEachTime);
    }
}
