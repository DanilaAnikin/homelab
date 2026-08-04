"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Label } from "@workspace/ui/components/label";
import { Card, CardContent } from "@workspace/ui/components/card";
import {
  DataTable,
  type DataTableColumn,
  type RowStatus,
} from "@workspace/ui/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/empty-state";
import { toast } from "@workspace/ui/components/sonner";
import {
  WebhookIcon,
  Trash2Icon,
  SendIcon,
  PlusIcon,
  EyeIcon,
  EyeOffIcon,
} from "lucide-react";
import {
  createWebhookAction,
  deleteWebhookAction,
  updateWebhookAction,
  pingWebhookAction,
} from "./actions";
import type { WebhookEvent as Event } from "@workspace/db/schemas";

interface Hook {
  id: string;
  url: string;
  events: Event[];
  secret: string;
  enabled: boolean;
  lastStatus: string | null;
}

// A failing last delivery washes the row destructive; a disabled endpoint reads
// as neutral/pending; an active endpoint is unmarked.
function hookRowStatus(h: Hook): RowStatus | undefined {
  if (h.lastStatus && !/^2/.test(h.lastStatus)) return "destructive";
  if (!h.enabled) return "pending";
  return undefined;
}

export function WebhooksManager({
  hooks,
  availableEvents,
}: {
  hooks: Hook[];
  availableEvents: Event[];
}) {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [selected, setSelected] = useState<Event[]>(["email.sent"]);
  const [busy, setBusy] = useState(false);
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});

  function toggleEvent(e: Event) {
    setSelected((s) => (s.includes(e) ? s.filter((x) => x !== e) : [...s, e]));
  }

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim() || selected.length === 0) {
      return toast.error("Add a URL and at least one event");
    }
    setBusy(true);
    const res = await createWebhookAction(url.trim(), selected);
    setBusy(false);
    if (res?.error) return toast.error(res.error);
    toast.success("Webhook created");
    setUrl("");
    setSelected(["email.sent"]);
    router.refresh();
  }

  async function ping(id: string) {
    const res = await pingWebhookAction(id);
    if (res?.error) return toast.error(res.error);
    toast.success(`Ping sent (${res.status})`);
    router.refresh();
  }

  async function toggle(h: Hook) {
    const res = await updateWebhookAction(h.id, { enabled: !h.enabled });
    if (res?.error) return toast.error(res.error);
    router.refresh();
  }

  async function remove(id: string) {
    if (!confirm("Delete this webhook?")) return;
    const res = await deleteWebhookAction(id);
    if (res?.error) return toast.error(res.error);
    toast.success("Webhook deleted");
    router.refresh();
  }

  const columns: DataTableColumn<Hook>[] = [
    {
      id: "endpoint",
      header: "Endpoint",
      cell: (h) => (
        <div className="min-w-0 space-y-1.5">
          <p className="truncate font-mono text-body-strong">{h.url}</p>
          <div className="flex flex-wrap gap-1.5">
            {h.events.map((e) => (
              <span
                key={e}
                className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
              >
                {e}
              </span>
            ))}
          </div>
          <div className="flex items-center gap-2 rounded-md bg-muted px-2.5 py-1.5">
            <span className="text-caption text-muted-foreground">Secret</span>
            <code className="min-w-0 flex-1 truncate font-mono text-caption">
              {revealed[h.id] ? h.secret : "whsec_••••••••••••••••"}
            </code>
            <button
              type="button"
              onClick={() =>
                setRevealed((r) => ({ ...r, [h.id]: !r[h.id] }))
              }
              className="text-muted-foreground transition-colors hover:text-foreground"
              aria-label={revealed[h.id] ? "Hide secret" : "Reveal secret"}
            >
              {revealed[h.id] ? (
                <EyeOffIcon className="size-3.5" />
              ) : (
                <EyeIcon className="size-3.5" />
              )}
            </button>
          </div>
        </div>
      ),
    },
    {
      id: "status",
      header: "Status",
      cell: (h) => (
        <div className="flex flex-wrap items-center gap-2">
          {h.lastStatus && (
            <StatusBadge
              status={/^2/.test(h.lastStatus) ? "delivered" : "failed"}
            />
          )}
          <StatusBadge status={h.enabled ? "active" : "disabled"} />
        </div>
      ),
    },
    {
      id: "actions",
      header: "",
      align: "right",
      cellClassName: "w-px",
      cell: (h) => (
        <div className="flex items-center justify-end gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => ping(h.id)}
            iconStart={<SendIcon />}
          >
            Test
          </Button>
          <Button variant="ghost" size="sm" onClick={() => toggle(h)}>
            {h.enabled ? "Disable" : "Enable"}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="text-muted-foreground hover:text-destructive"
            onClick={() => remove(h.id)}
            aria-label="Delete webhook"
          >
            <Trash2Icon className="size-4" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="p-5">
          <form onSubmit={add} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="wurl">Endpoint URL</Label>
              <Input
                id="wurl"
                placeholder="https://yourapp.com/webhooks/launchmail"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label>Events</Label>
              <div className="flex flex-wrap gap-2">
                {availableEvents.map((event) => (
                  <button
                    key={event}
                    type="button"
                    onClick={() => toggleEvent(event)}
                    className={
                      "rounded-badge border px-3 py-1 font-mono text-xs transition-colors " +
                      (selected.includes(event)
                        ? "border-brand-200 bg-brand-100 text-brand-800"
                        : "text-muted-foreground hover:bg-muted")
                    }
                  >
                    {event}
                  </button>
                ))}
              </div>
            </div>
            <Button type="submit" disabled={busy} loading={busy} iconStart={<PlusIcon />}>
              Add webhook
            </Button>
          </form>
        </CardContent>
      </Card>

      {hooks.length === 0 ? (
        <EmptyState
          icon={WebhookIcon}
          title="No webhooks yet"
          description="Get notified at your own URL when emails are sent/failed or forms are submitted. Payloads are signed with HMAC-SHA256."
        />
      ) : (
        <Card>
          <CardContent className="px-3 pb-3">
            <div className="flex items-center justify-between px-3 py-3">
              <p className="text-eyebrow text-muted-foreground">Endpoints</p>
              <span className="text-caption tabular-nums text-muted-foreground">
                {hooks.length}
              </span>
            </div>
            <DataTable
              columns={columns}
              data={hooks}
              getRowKey={(h) => h.id}
              getRowStatus={hookRowStatus}
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
