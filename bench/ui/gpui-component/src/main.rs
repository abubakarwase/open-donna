use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use gpui_kit::component::{
    IndexPath, h_flex, v_flex,
    combobox::{Combobox, ComboboxState},
    date_picker::{DatePicker, DatePickerState},
    input::{Input, InputState},
    label::Label,
    searchable_list::SearchableVec,
    table::{Column, DataTable, TableDelegate, TableState},
};
use gpui_kit::{
    App, AppContext, Bounds, Context, Entity, IntoElement, ParentElement, Render, SharedString,
    Styled, Window, WindowBounds, WindowOptions, div, px, size,
};

static FIRST_FRAME: AtomicBool = AtomicBool::new(false);
static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();

struct RowsDelegate {
    rows: Vec<[SharedString; 6]>,
}

impl RowsDelegate {
    fn new() -> Self {
        let rows = (0..10_000)
            .map(|i| {
                [
                    format!("A{i}").into(),
                    format!("B{i}").into(),
                    format!("C{i}").into(),
                    format!("{}", i * 3).into(),
                    format!("{}", i % 97).into(),
                    format!("R{}", i % 10).into(),
                ]
            })
            .collect();
        Self { rows }
    }
}

impl TableDelegate for RowsDelegate {
    fn columns_count(&self, _: &App) -> usize {
        6
    }

    fn rows_count(&self, _: &App) -> usize {
        self.rows.len()
    }

    fn column(&self, col_ix: usize, _: &App) -> Column {
        let name = format!("c{col_ix}");
        Column::new(name.clone(), name).width(px(120.))
    }

    fn render_td(
        &mut self,
        row_ix: usize,
        col_ix: usize,
        _: &mut Window,
        _: &mut Context<TableState<Self>>,
    ) -> impl IntoElement {
        self.rows[row_ix][col_ix].clone()
    }
}

struct BenchApp {
    input: Entity<InputState>,
    date: Entity<DatePickerState>,
    combo: Entity<ComboboxState<SearchableVec<&'static str>>>,
    table: Entity<TableState<RowsDelegate>>,
}

impl Render for BenchApp {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        if !FIRST_FRAME.swap(true, Ordering::SeqCst) {
            let start = START.get_or_init(Instant::now);
            let ms = start.elapsed().as_secs_f64() * 1000.0;
            println!("FIRST_FRAME_MS {ms:.3}");
            let _ = std::io::Write::flush(&mut std::io::stdout());
        }

        v_flex()
            .size_full()
            .p_4()
            .gap_3()
            .child(Label::new("Bench B — GPUI + gpui-kit 0.10.0"))
            .child(
                h_flex()
                    .gap_3()
                    .items_start()
                    .child(
                        v_flex()
                            .gap_1()
                            .w(px(220.))
                            .child(Label::new("Text"))
                            .child(Input::new(&self.input)),
                    )
                    .child(
                        v_flex()
                            .gap_1()
                            .w(px(240.))
                            .child(Label::new("DatePicker"))
                            .child(DatePicker::new(&self.date)),
                    )
                    .child(
                        v_flex()
                            .gap_1()
                            .w(px(260.))
                            .child(Label::new("Combobox.multiple"))
                            .child(Combobox::new(&self.combo).cleanable(true)),
                    ),
            )
            .child(
                div()
                    .flex_1()
                    .w_full()
                    .border_1()
                    .child(DataTable::new(&self.table).stripe(true).bordered(true)),
            )
    }
}

fn main() {
    START.get_or_init(Instant::now);
    gpui_kit::application().run(|cx| {
        gpui_kit::init(cx);
        gpui_kit::open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(Bounds::centered(
                    None,
                    size(px(900.), px(700.)),
                    cx,
                ))),
                ..Default::default()
            },
            cx,
            |window, cx| {
                cx.new(|cx| {
                    let input = cx.new(|cx| InputState::new(window, cx));
                    let date = cx.new(|cx| DatePickerState::new(window, cx));
                    let items = SearchableVec::new(vec![
                        "email",
                        "calendar",
                        "sheet",
                        "general",
                        "escalate",
                    ]);
                    let combo = cx.new(|cx| {
                        ComboboxState::new(items, Vec::<IndexPath>::new(), window, cx).multiple(true)
                    });
                    let table = cx.new(|cx| TableState::new(RowsDelegate::new(), window, cx));
                    BenchApp {
                        input,
                        date,
                        combo,
                        table,
                    }
                })
            },
        )
        .expect("open window");
    });
}
