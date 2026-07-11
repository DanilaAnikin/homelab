"use client";

import {
  DataTable,
  type DataTableColumn,
} from "@workspace/ui/components/data-table";

export interface AuditRow {
  id: string;
  userName: string | null;
  action: string;
  target: string | null;
  createdAt: string;
}

const auditColumns: DataTableColumn<AuditRow>[] = [
  {
    id: "event",
    header: "Event",
    cell: (a) => (
      <p className="text-body">
        <span className="text-body-strong">{a.userName ?? "Someone"}</span>{" "}
        <span className="text-muted-foreground">{a.action}</span>
        {a.target && <span className="text-body-strong"> {a.target}</span>}
      </p>
    ),
  },
  {
    id: "time",
    header: "When",
    align: "right",
    mono: true,
    cellClassName: "text-muted-foreground",
    cell: (a) =>
      new Date(a.createdAt).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      }),
  },
];

/**
 * Workspace activity-log table. A Client Component because its columns carry
 * `cell` render functions, which cannot be serialized across the Server→Client
 * boundary.
 */
export function AuditLogTable({ audit }: { audit: AuditRow[] }) {
  return (
    <DataTable
      columns={auditColumns}
      data={audit}
      getRowKey={(a) => a.id}
      density="compact"
      emptyState="No activity recorded yet."
    />
  );
}
