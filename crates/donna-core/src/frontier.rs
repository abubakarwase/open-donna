use thiserror::Error;

/// Errors from a frontier completion call.
#[derive(Debug, Error)]
pub enum FrontierError {
    #[error("frontier HTTP error: {0}")]
    Http(String),
    #[error("frontier JSON error: {0}")]
    Json(String),
    #[error("{0}")]
    Other(String),
}

/// Frontier (cloud / larger) model completion surface. No I/O in this crate.
pub trait FrontierProvider {
    fn complete(&self, payload: &str) -> Result<String, FrontierError>;
}
