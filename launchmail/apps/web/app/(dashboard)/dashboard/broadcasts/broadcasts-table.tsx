"use client";

import { StatusBadge } from "@/components/status-badge";
import {
  DataTable,
  type DataTableColumn,
  type RowStatus,
} from "@workspace/ui/components/data-table";

export interface BroadcastRow {
  id: string;
  name: string;
  subject: string;
  status: string;
  totalCount: number;
  sentCount: number;
  createdAt: string;
  sentAt: string | null;
}

function broadcastRowStatus(status: string): RowStatus | undefined {
  switch (status.toLowerCase()) {
    case "failed":
    case "error":
      return "destructive";
    case "processing":
    case "queued":
      return "info";
    default:
      return undefined;
  }
}

const columns: DataTableColumn<BroadcastRow>[] = [
  {
    id: "name",
    header: "Broadcast",
    cell: (b) => (
      <div className="min-w-0">
        <p className="truncate text-body-strong">{b.name}</p>
        <p className="truncate text-caption text-muted-foreground">
          {b.subject}
        </p>
      </div>
    ),
  },
  {
    id: "status",
    header: "Status",
    cell: (b) => <StatusBadge status={b.status} />,
  },
  {
    id: "sent",
    header: "Sent",
    align: "right",
    mono: true,
    cell: (b) => (
      <span className="text-muted-foreground">
        {b.sentCount}/{b.totalCount}
      </span>
    ),
  },
  {
    id: "date",
    header: "Date",
    align: "right",
    mono: true,
    cell: (b) => (
      <span className="text-muted-foreground">
        {new Date(b.sentAt ?? b.createdAt).toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
        })}
      </span>
    ),
  },
];

/**
 * Broadcasts table. A Client Component because its columns carry `cell` render
 * functions, which cannot be serialized across the Server→Client boundary.
 */
export function BroadcastsTable({
  broadcasts,
}: {
  broadcasts: BroadcastRow[];
}) {
  return (
    <DataTable
      columns={columns}
      data={broadcasts}
      getRowKey={(b) => b.id}
      getRowStatus={(b) => broadcastRowStatus(b.status)}
    />
  );
}
