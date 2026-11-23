//! Sheet workbook loader, profile, SQL guard, and query execution.

mod guard;
mod loader;
mod profile;
mod query;
mod range;

pub use guard::assert_readonly_select;
pub use loader::{load_workbook, LoadedWorkbook};
pub use profile::{build_profile, SheetProfile, TableProfile};
pub use query::{execute_query, query_sheet, run_sql_rows, QuerySheetResult};
pub use range::parse_a1_row_bounds;
