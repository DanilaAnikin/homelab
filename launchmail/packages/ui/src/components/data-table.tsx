"use client"

import * as React from "react"

import { cn } from "@workspace/ui/lib/utils"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@workspace/ui/components/table"

type Density = "compact" | "comfortable"

/** Tone of the 2px left spine + faint row-wash on a row. */
type RowStatus = "destructive" | "warning" | "success" | "info" | "pending"

type ColumnAlign = "left" | "right" | "center"

type DataTableColumn<TRow> = {
  /** Stable key for the column. */
  id: string
  /** Eyebrow header label. Omit for a spacer/action column. */
  header?: React.ReactNode
  /** Cell renderer for a row. */
  cell: (row: TRow, rowIndex: number) => React.ReactNode
  /** Alignment. Numeric/time/id columns should use "right". */
  align?: ColumnAlign
  /**
   * Render this column with `font-mono tabular-nums` — use for
   * numeric, time, and id columns.
   */
  mono?: boolean
  /** Extra classes applied to both the header and body cells. */
  className?: string
  /** Header-only classes. */
  headerClassName?: string
  /** Body-cell-only classes. */
  cellClassName?: string
}

type DataTableProps<TRow> = {
  columns: DataTableColumn<TRow>[]
  data: TRow[]
  /** Stable key per row. */
  getRowKey: (row: TRow, index: number) => React.Key
  /** Optional status tone driving the left spine + row-wash. */
  getRowStatus?: (row: TRow, index: number) => RowStatus | undefined
  /** Invoked when a row is clicked; makes rows interactive. */
  onRowClick?: (row: TRow, index: number) => void
  /** Row height / padding density. */
  density?: Density
  /** Zebra striping using the --surface token. */
  zebra?: boolean
  /** Rendered (spanning all columns) when `data` is empty. */
  emptyState?: React.ReactNode
  className?: string
}

const alignClass: Record<ColumnAlign, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
}

const statusSpine: Record<RowStatus, string> = {
  destructive: "before:bg-destructive",
  warning: "before:bg-warning",
  success: "before:bg-success",
  info: "before:bg-info",
  pending: "before:bg-pending",
}

const statusWash: Record<RowStatus, string> = {
  destructive: "bg-destructive-tint/40 hover:bg-destructive-tint/60",
  warning: "bg-warning-tint/40 hover:bg-warning-tint/60",
  success: "bg-success-tint/40 hover:bg-success-tint/60",
  info: "bg-info-tint/40 hover:bg-info-tint/60",
  pending: "bg-pending-tint/60 hover:bg-pending-tint/80",
}

function DataTable<TRow>({
  columns,
  data,
  getRowKey,
  getRowStatus,
  onRowClick,
  density = "comfortable",
  zebra = false,
  emptyState,
  className,
}: DataTableProps<TRow>) {
  const cellPad = density === "compact" ? "py-2 px-3" : "py-3 px-3"
  const interactive = Boolean(onRowClick)

  return (
    <Table
      data-slot="data-table"
      data-density={density}
      className={cn("text-body", className)}
    >
      <TableHeader>
        <TableRow className="border-b-0 hover:bg-transparent">
          {columns.map((column) => (
            <TableHead
              key={column.id}
              className={cn(
                "h-auto border-b border-border-strong px-3 py-2 text-eyebrow",
                alignClass[column.align ?? "left"],
                column.className,
                column.headerClassName
              )}
            >
              {column.header}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.length === 0 ? (
          <TableRow className="hover:bg-transparent">
            <TableCell
              colSpan={columns.length}
              className="px-3 py-10 text-center text-caption text-muted-foreground"
            >
              {emptyState ?? "No results."}
            </TableCell>
          </TableRow>
        ) : (
          data.map((row, rowIndex) => {
            const status = getRowStatus?.(row, rowIndex)
            return (
              <TableRow
                key={getRowKey(row, rowIndex)}
                data-status={status}
                onClick={
                  onRowClick ? () => onRowClick(row, rowIndex) : undefined
                }
                className={cn(
                  "relative",
                  status &&
                    "before:absolute before:inset-y-0 before:left-0 before:w-0.5",
                  status && statusSpine[status],
                  status ? statusWash[status] : zebra && "even:bg-surface",
                  interactive && "cursor-pointer",
                  "hover:bg-muted"
                )}
              >
                {columns.map((column) => (
                  <TableCell
                    key={column.id}
                    className={cn(
                      cellPad,
                      "align-middle",
                      alignClass[column.align ?? "left"],
                      column.mono && "font-mono tabular-nums",
                      column.className,
                      column.cellClassName
                    )}
                  >
                    {column.cell(row, rowIndex)}
                  </TableCell>
                ))}
              </TableRow>
            )
          })
        )}
      </TableBody>
    </Table>
  )
}

export { DataTable }
export type { DataTableColumn, DataTableProps, Density, RowStatus }
