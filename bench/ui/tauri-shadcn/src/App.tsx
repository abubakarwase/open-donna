import { useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  useReactTable,
  getCoreRowModel,
  flexRender,
  type ColumnDef,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { DayPicker } from "react-day-picker";
import { format } from "date-fns";
import { Check, ChevronsUpDown, CalendarIcon } from "lucide-react";
import * as Popover from "@radix-ui/react-popover";
import { Command } from "cmdk";
import { cn } from "./lib/utils";
import "react-day-picker/style.css";
import "./App.css";

type Row = {
  id: number;
  c0: string;
  c1: string;
  c2: string;
  c3: string;
  c4: string;
  c5: string;
};

const OPTIONS = ["email", "calendar", "sheet", "general", "escalate"];

export default function App() {
  const [text, setText] = useState("");
  const [date, setDate] = useState<Date | undefined>();
  const [openCal, setOpenCal] = useState(false);
  const [openMulti, setOpenMulti] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [rows, setRows] = useState<Row[]>([]);
  const signaled = useRef(false);
  const parentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    invoke<Row[]>("get_rows").then(setRows);
  }, []);

  useEffect(() => {
    if (rows.length > 0 && !signaled.current) {
      signaled.current = true;
      invoke("first_frame");
    }
  }, [rows]);

  const columns = useMemo<ColumnDef<Row>[]>(
    () => [
      { accessorKey: "id", header: "id", size: 80 },
      { accessorKey: "c0", header: "c0", size: 100 },
      { accessorKey: "c1", header: "c1", size: 100 },
      { accessorKey: "c2", header: "c2", size: 100 },
      { accessorKey: "c3", header: "c3", size: 100 },
      { accessorKey: "c4", header: "c4", size: 100 },
      { accessorKey: "c5", header: "c5", size: 100 },
    ],
    []
  );

  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const rowModel = table.getRowModel().rows;
  const virtualizer = useVirtualizer({
    count: rowModel.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 32,
    overscan: 12,
  });

  function toggleOpt(o: string) {
    setSelected((prev) =>
      prev.includes(o) ? prev.filter((x) => x !== o) : [...prev, o]
    );
  }

  return (
    <main className="page">
      <h1>Bench A — Tauri + React + shadcn-style</h1>
      <div className="controls">
        <label className="field">
          <span>Text</span>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Type here"
          />
        </label>

        <div className="field">
          <span>Date (Popover + DayPicker)</span>
          <Popover.Root open={openCal} onOpenChange={setOpenCal}>
            <Popover.Trigger asChild>
              <button type="button" className="btn">
                <CalendarIcon size={16} />
                {date ? format(date, "PPP") : "Pick a date"}
              </button>
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content className="popover" align="start" sideOffset={4}>
                <DayPicker
                  mode="single"
                  selected={date}
                  onSelect={(d) => {
                    setDate(d);
                    setOpenCal(false);
                  }}
                />
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        </div>

        <div className="field">
          <span>Multi-select (cmdk + Popover)</span>
          <Popover.Root open={openMulti} onOpenChange={setOpenMulti}>
            <Popover.Trigger asChild>
              <button type="button" className="btn">
                <ChevronsUpDown size={16} />
                {selected.length ? selected.join(", ") : "Select…"}
              </button>
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content className="popover list" align="start" sideOffset={4}>
                <Command label="Multi-select" shouldFilter={true}>
                  <Command.Input placeholder="Search…" className="cmd-input" />
                  <Command.List>
                    <Command.Empty>No match.</Command.Empty>
                    {OPTIONS.map((o) => (
                      <Command.Item
                        key={o}
                        value={o}
                        onSelect={() => toggleOpt(o)}
                        className="opt"
                      >
                        <Check
                          size={14}
                          className={cn(!selected.includes(o) && "invisible")}
                        />
                        {o}
                      </Command.Item>
                    ))}
                  </Command.List>
                </Command>
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        </div>
      </div>

      <div className="table-wrap" ref={parentRef}>
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          <table className="grid">
            <thead>
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id}>
                  {hg.headers.map((h) => (
                    <th key={h.id} style={{ width: h.getSize() }}>
                      {flexRender(h.column.columnDef.header, h.getContext())}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {virtualizer.getVirtualItems().map((vRow) => {
                const row = rowModel[vRow.index];
                return (
                  <tr
                    key={row.id}
                    style={{
                      position: "absolute",
                      top: 0,
                      transform: `translateY(${vRow.start + 32}px)`,
                      height: `${vRow.size}px`,
                      width: "100%",
                      display: "table",
                      tableLayout: "fixed",
                    }}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} style={{ width: cell.column.getSize() }}>
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext()
                        )}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
      <p className="meta">rows: {rows.length}</p>
    </main>
  );
}
