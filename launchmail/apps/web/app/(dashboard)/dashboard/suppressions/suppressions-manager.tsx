"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Card, CardContent } from "@workspace/ui/components/card";
import {
  DataTable,
  type DataTableColumn,
  type RowStatus,
} from "@workspace/ui/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/empty-state";
import { toast } from "@workspace/ui/components/sonner";
import { ShieldBanIcon, Trash2Icon, PlusIcon } from "lucide-react";
import { addSuppressionAction, removeSuppressionAction } from "./actions";

interface Row {
  id: string;
  email: string;
  reason: string;
  createdAt: string;
}

function suppressionRowStatus(reason: string): RowStatus | undefined {
  switch (reason.toLowerCase()) {
    case "bounced":
    case "bounce":
    case "complaint":
      return "destructive";
    default:
      return undefined;
  }
}

export function SuppressionsManager({ rows }: { rows: Row[] }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setBusy(true);
    const res = await addSuppressionAction(email.trim());
    setBusy(false);
    if (res?.error) return toast.error(res.error);
    toast.success(`${email.trim()} suppressed`);
    setEmail("");
    router.refresh();
  }

  async function remove(id: string, addr: string) {
    const res = await removeSuppressionAction(id);
    if (res?.error) return toast.error(res.error);
    toast.success(`${addr} removed`);
    router.refresh();
  }

  const columns: DataTableColumn<Row>[] = [
    {
      id: "email",
      header: "Address",
      cell: (r) => <span className="truncate text-body-strong">{r.email}</span>,
    },
    {
      id: "reason",
      header: "Reason",
      cell: (r) => <StatusBadge status={r.reason} />,
    },
    {
      id: "date",
      header: "Suppressed",
      align: "right",
      mono: true,
      cell: (r) => (
        <span className="text-muted-foreground">
          {new Date(r.createdAt).toLocaleDateString()}
        </span>
      ),
    },
    {
      id: "actions",
      align: "right",
      cellClassName: "w-px",
      cell: (r) => (
        <button
          type="button"
          onClick={() => remove(r.id, r.email)}
          className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-destructive"
          aria-label={`Remove ${r.email}`}
        >
          <Trash2Icon className="size-4" />
        </button>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <form onSubmit={add} className="flex gap-2">
        <Input
          type="email"
          placeholder="email@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="max-w-sm"
        />
        <Button type="submit" disabled={busy}>
          <PlusIcon className="mr-1 size-4" />
          Suppress
        </Button>
      </form>

      {rows.length === 0 ? (
        <EmptyState
          icon={ShieldBanIcon}
          title="No suppressed addresses"
          description="Addresses you suppress (or that hard-bounce) are blocked from future sends."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="flex items-center justify-between px-3 py-3">
              <p className="text-eyebrow text-muted-foreground">
                Suppressed addresses
              </p>
              <span className="text-caption tabular-nums text-muted-foreground">
                {rows.length}
              </span>
            </div>
            <DataTable
              columns={columns}
              data={rows}
              getRowKey={(r) => r.id}
              getRowStatus={(r) => suppressionRowStatus(r.reason)}
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
