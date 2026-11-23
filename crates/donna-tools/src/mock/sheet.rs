use std::path::PathBuf;

use crate::backend::SheetBackend;
use crate::mock::fixtures::sheet_path;
use crate::Result;

#[derive(Debug, Clone, Default)]
pub struct MockSheet;

impl SheetBackend for MockSheet {
    fn path(&self, sheet_ref: &str) -> Result<PathBuf> {
        sheet_path(sheet_ref)
    }
}
