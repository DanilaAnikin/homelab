"use client";

import { StatusBadge } from "@/components/status-badge";
import {
  DataTable,
  type DataTableColumn,
  type RowStatus,
} from "@workspace/ui/components/data-table";

export interface EmailLogRow {
  id: string;
  subject: string;
  to: { email: string }[];
  status: string;
  createdAt: string;
}

/** Map a log status onto the DataTable status-spine tone. */
function logRowStatus(status: string): RowStatus | undefined {
  switch (status.toLowerCase()) {
    case "failed":
    case "bounced":
    case "complaint":
    case "error":
      return "destructive";
    case "queued":
    case "processing":
      return "info";
    default:
      return undefined;
  }
}

const recentColumns: DataTableColumn<EmailLogRow>[] = [
  {
    id: "status",
    header: "Status",
    cell: (log) => <StatusBadge status={log.status} />,
  },
  {
    id: "to",
    header: "To",
    mono: true,
    cell: (log) => (
      <span className="block max-w-[16rem] truncate text-muted-foreground">
        {log.to.map((r) => r.email).join(", ")}
      </span>
    ),
  },
  {
    id: "subject",
    header: "Subject",
    cell: (log) => (
      <span className="block max-w-[20rem] truncate text-body-strong">
        {log.subject || "(no subject)"}
      </span>
    ),
  },
  {
    id: "time",
    header: "Time",
    align: "right",
    mono: true,
    cell: (log) => (
      <span className="text-muted-foreground">
        {new Date(log.createdAt).toLocaleString(undefined, {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        })}
      </span>
    ),
  },
];

/**
 * Recent-activity table for the Overview page. Lives in a Client Component
 * because its columns carry `cell` render functions, which cannot be
 * serialized across the Server→Client boundary (doing so throws
 * "Functions cannot be passed directly to Client Components" during the RSC /
 * hydration render and trips the dashboard error boundary).
 */
export function RecentActivityTable({ logs }: { logs: EmailLogRow[] }) {
  return (
    <div className="border-t border-border-strong">
      <DataTable
        density="compact"
        columns={recentColumns}
        data={logs}
        getRowKey={(log) => log.id}
        getRowStatus={(log) => logRowStatus(log.status)}
      />
    </div>
  );
}
