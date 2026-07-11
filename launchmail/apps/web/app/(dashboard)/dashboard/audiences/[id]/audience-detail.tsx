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
} from "@workspace/ui/components/data-table";
import { StatusBadge } from "@/components/status-badge";
import { toast } from "@workspace/ui/components/sonner";
import { PlusIcon, Trash2Icon, SendIcon, UploadIcon } from "lucide-react";
import {
  addContactsAction,
  removeContactAction,
  sendBroadcastAction,
} from "../actions";

interface Contact {
  id: string;
  email: string;
  firstName: string | null;
  lastName: string | null;
  subscribed: boolean;
}
interface Props {
  audience: { id: string; name: string; contacts: Contact[] };
  templates: { id: string; name: string }[];
  configs: { id: string; name: string; host: string }[];
}

const selectClass =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

export function AudienceDetail({ audience, templates, configs }: Props) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [first, setFirst] = useState("");
  const [bulk, setBulk] = useState("");
  const [busy, setBusy] = useState(false);

  const [templateId, setTemplateId] = useState(templates[0]?.id ?? "");
  const [smtpConfigId, setSmtpConfigId] = useState(configs[0]?.id ?? "");
  const [name] = useState(`${audience.name} broadcast`);
  const [subject, setSubject] = useState("");
  const [sending, setSending] = useState(false);

  async function addOne(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setBusy(true);
    const res = await addContactsAction(audience.id, [
      { email: email.trim(), firstName: first.trim() || undefined },
    ]);
    setBusy(false);
    if (res?.error) return toast.error(res.error);
    toast.success("Contact added");
    setEmail("");
    setFirst("");
    router.refresh();
  }

  async function importBulk() {
    const rows = bulk
      .split("\n")
      .map((l) => {
        const [email, firstName] = l.split(",").map((s) => s.trim());
        return { email: email ?? "", firstName: firstName || undefined };
      })
      .filter((r) => /\S+@\S+\.\S+/.test(r.email));
    if (rows.length === 0) return toast.error("No valid emails found");
    setBusy(true);
    const res = await addContactsAction(audience.id, rows);
    setBusy(false);
    if (res?.error) return toast.error(res.error);
    toast.success(`Imported ${res.added} contacts`);
    setBulk("");
    router.refresh();
  }

  async function removeOne(id: string) {
    const res = await removeContactAction(audience.id, id);
    if (res?.error) return toast.error(res.error);
    router.refresh();
  }

  async function send() {
    if (!templateId) return toast.error("Pick a template");
    if (!subject.trim()) return toast.error("Add a subject");
    if (!smtpConfigId) return toast.error("Pick an SMTP connection");
    setSending(true);
    const res = await sendBroadcastAction({
      audienceId: audience.id,
      templateId,
      smtpConfigId,
      name: name.trim() || `${audience.name} broadcast`,
      subject: subject.trim(),
    });
    setSending(false);
    if (res?.error) return toast.error(res.error);
    const sent = res?.sentCount ?? 0;
    const failed = res?.failedCount ?? 0;
    if (failed > 0) {
      if (sent > 0) {
        toast.error(
          `Queued ${sent} email${sent === 1 ? "" : "s"}, but ${failed} failed`,
        );
      } else {
        toast.error(
          `Broadcast failed — ${failed} email${failed === 1 ? "" : "s"} could not be queued`,
        );
      }
    } else {
      toast.success(
        `Broadcast queued to ${sent} contact${sent === 1 ? "" : "s"}`,
      );
    }
    router.refresh();
  }

  const contactColumns: DataTableColumn<Contact>[] = [
    {
      id: "contact",
      header: "Contact",
      cell: (c) => (
        <div className="min-w-0">
          <p className="truncate text-body-strong">{c.email}</p>
          {(c.firstName || c.lastName) && (
            <p className="truncate text-caption text-muted-foreground">
              {[c.firstName, c.lastName].filter(Boolean).join(" ")}
            </p>
          )}
        </div>
      ),
    },
    {
      id: "status",
      header: "Status",
      align: "right",
      cell: (c) => (
        <StatusBadge status={c.subscribed ? "active" : "unverified"} />
      ),
    },
    {
      id: "actions",
      align: "right",
      cellClassName: "w-px",
      cell: (c) => (
        <button
          type="button"
          onClick={() => removeOne(c.id)}
          className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-destructive"
          aria-label={`Remove ${c.email}`}
        >
          <Trash2Icon className="size-4" />
        </button>
      ),
    },
  ];

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
      <div className="space-y-6">
        <Card>
          <CardContent className="space-y-4 p-5">
            <p className="text-sm font-semibold">Add contacts</p>
            <form onSubmit={addOne} className="flex flex-wrap gap-2">
              <Input
                type="email"
                placeholder="email@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="min-w-[200px] flex-1"
              />
              <Input
                placeholder="First name"
                value={first}
                onChange={(e) => setFirst(e.target.value)}
                className="w-32"
              />
              <Button type="submit" disabled={busy}>
                <PlusIcon className="mr-1 size-4" />
                Add
              </Button>
            </form>
            <div className="space-y-2">
              <Label className="text-xs text-muted-foreground">
                Or paste emails (one per line, optional ,name)
              </Label>
              <textarea
                value={bulk}
                onChange={(e) => setBulk(e.target.value)}
                rows={3}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="jane@example.com&#10;john@example.com"
              />
              <Button variant="outline" size="sm" onClick={importBulk} disabled={busy}>
                <UploadIcon className="mr-1 size-3.5" />
                Import
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-0">
            <div className="px-3 py-3">
              <p className="text-eyebrow text-muted-foreground">
                Contacts ({audience.contacts.length})
              </p>
            </div>
            <DataTable
              columns={contactColumns}
              data={audience.contacts}
              getRowKey={(c) => c.id}
              emptyState="No contacts yet."
            />
          </CardContent>
        </Card>
      </div>

      <Card className="h-fit lg:sticky lg:top-8">
        <CardContent className="space-y-4 p-5">
          <p className="text-sm font-semibold">Send a broadcast</p>
          {templates.length === 0 || configs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              You need at least one{" "}
              {templates.length === 0 ? "template" : "SMTP connection"} to send a
              broadcast.
            </p>
          ) : (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="bsubject">Subject</Label>
                <Input
                  id="bsubject"
                  value={subject}
                  onChange={(e) => setSubject(e.target.value)}
                  placeholder="Your newsletter subject"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="btpl">Template</Label>
                <select
                  id="btpl"
                  className={selectClass}
                  value={templateId}
                  onChange={(e) => setTemplateId(e.target.value)}
                >
                  {templates.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="bsmtp">SMTP connection</Label>
                <select
                  id="bsmtp"
                  className={selectClass}
                  value={smtpConfigId}
                  onChange={(e) => setSmtpConfigId(e.target.value)}
                >
                  {configs.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </div>
              <Button className="w-full" onClick={send} disabled={sending}>
                <SendIcon className="mr-1 size-4" />
                {sending ? "Sending…" : "Send broadcast"}
              </Button>
              <p className="text-xs text-muted-foreground">
                Sends to all subscribed contacts. Each email includes an
                unsubscribe link.
              </p>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
