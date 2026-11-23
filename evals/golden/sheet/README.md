# Sheet golden set (E-08)

Layer-1 closed-loop tool calls for J8–J9 (plus ~50 sheet escalate / J11). Ids `sht-NNNN`.

## Loader / range conventions

- `donna_evals.sheets.load_workbook_to_duckdb`: one DuckDB table per worksheet (table name = sheet name); row 1 headers; `_row` = Excel row (data starts at 2); types inferred from values.
- SQL uses `FROM "<worksheet>"` (e.g. `sales`), never a generic `sheet` table.
- **J9 range:** A1 like `B2:B40` on `input.selection` and `query_sheet.args.range`; SQL restricts with `_row BETWEEN start AND end` and the selected columns (header row excluded).

## Counts (200)

J8 open-sheet 100 (`match: sql_exec`), J9 selected-range 50 (`sql_exec` + selection), escalate 50 (J11: messy / cross-sheet / why-forecast / ambiguous). Split 50/150. Difficulty 80/80/40. Local 150 / escalate 50. Test cases tagged `needs_human_review`.

## Rebuild

`cd evals && uv run python golden/sheet/build.py` (twice → identical bytes).
