use serde::Serialize;
use std::sync::OnceLock;
use std::time::Instant;

static START: OnceLock<Instant> = OnceLock::new();

#[derive(Clone, Serialize)]
pub struct Row {
    pub id: u32,
    pub c0: String,
    pub c1: String,
    pub c2: String,
    pub c3: String,
    pub c4: String,
    pub c5: String,
}

fn generate_rows() -> Vec<Row> {
    (0..10_000)
        .map(|i| Row {
            id: i,
            c0: format!("A{i}"),
            c1: format!("B{i}"),
            c2: format!("C{i}"),
            c3: format!("{}", i * 3),
            c4: format!("{}", i % 97),
            c5: format!("R{}", i % 10),
        })
        .collect()
}

#[tauri::command]
fn get_rows() -> Vec<Row> {
    generate_rows()
}

#[tauri::command]
fn first_frame() {
    let start = START.get_or_init(Instant::now);
    let ms = start.elapsed().as_secs_f64() * 1000.0;
    // Harness parses this line from stdout.
    println!("FIRST_FRAME_MS {ms:.3}");
    let _ = std::io::Write::flush(&mut std::io::stdout());
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    START.get_or_init(Instant::now);
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![get_rows, first_frame])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
