/// Opaque token proving a human approved a side-effecting action.
/// Only this crate can construct one; side-effect tools require `&ApprovalToken`.
///
/// External crates cannot invent a token:
///
/// ```compile_fail,E0424,E0616
/// use donna_core::ApprovalToken;
/// let _ = ApprovalToken { _private: () };
/// ```
///
/// ```compile_fail,E0599
/// use donna_core::ApprovalToken;
/// let _ = ApprovalToken::new();
/// ```
#[derive(Debug, Clone)]
pub struct ApprovalToken {
    _private: (),
}

#[allow(dead_code)] // minted via Approver; production UI lands later
impl ApprovalToken {
    /// Construct an approval token. Callers outside this crate cannot invent one.
    pub(crate) fn new() -> Self {
        Self { _private: () }
    }
}

/// Stub approver surface for UI/connectors.
pub trait Approver {
    fn approve(&self, draft_id: &str) -> ApprovalToken;
}

/// Always-approves stub for tests only (feature `test-approver` or `cfg(test)`).
#[cfg(any(test, feature = "test-approver"))]
#[derive(Debug, Default)]
pub struct AlwaysApprover;

#[cfg(any(test, feature = "test-approver"))]
impl Approver for AlwaysApprover {
    fn approve(&self, _draft_id: &str) -> ApprovalToken {
        ApprovalToken::new()
    }
}
