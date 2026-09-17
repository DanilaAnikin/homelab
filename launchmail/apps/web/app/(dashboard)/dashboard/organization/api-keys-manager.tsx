"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Label } from "@workspace/ui/components/label";
import { Badge } from "@workspace/ui/components/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@workspace/ui/components/select";
import {
  DataTable,
  type DataTableColumn,
} from "@workspace/ui/components/data-table";
import { CheckIcon, CopyIcon, KeyIcon, PlusIcon } from "lucide-react";
import {
  createOrgApiKeyAction,
  revokeOrgApiKeyAction,
  type ApiKeyRole,
} from "./api-keys-actions";

export interface ApiKeyRow {
  id: string;
  name: string;
  role: string;
  tokenPrefix: string;
  smtpConfigId: string | null;
  lastUsedAt: string | null;
  expiresAt: string | null;
  createdAt: string;
}

export interface MailboxOption {
  id: string;
  label: string;
}

const ROLE_ORDER: ApiKeyRole[] = ["reader", "writer", "admin"];
const ROLE_RANK: Record<ApiKeyRole, number> = { reader: 0, writer: 1, admin: 2 };
const ROLE_VARIANT: Record<string, "brand" | "info" | "pending"> = {
  admin: "brand",
  writer: "info",
  reader: "pending",
};
const ROLE_HELP: Record<ApiKeyRole, string> = {
  reader: "Read-only: mailboxes, incoming and sent mail, logs. Cannot send.",
  writer: "Send and reply, manage configs, forms and keys.",
  admin: "Everything a writer can do in this organization.",
};
// Radix Select cannot use an empty string as an item value.
const ORGANIZATION_SCOPE = "__organization__";

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : "Never";
}

export function ApiKeysManager({
  role,
  keys,
  mailboxes,
}: {
  role: ApiKeyRole;
  keys: ApiKeyRow[];
  mailboxes: MailboxOption[];
}) {
  const router = useRouter();
  const canManage = role === "admin" || role === "writer";
  const grantable = ROLE_ORDER.filter((r) => ROLE_RANK[r] <= ROLE_RANK[role]);
  const [name, setName] = useState("");
  const [keyRole, setKeyRole] = useState<ApiKeyRole>("reader");
  const [scope, setScope] = useState<string>(ORGANIZATION_SCOPE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ id: string; name: string; token: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);

  const mailboxLabel = new Map(mailboxes.map((m) => [m.id, m.label]));

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    setCreated(null);
    const res = await createOrgApiKeyAction({
      name,
      role: keyRole,
      smtpConfigId: scope === ORGANIZATION_SCOPE ? null : scope,
    });
    setBusy(false);
    if ("error" in res && res.error) {
      setError(res.error);
      return;
    }
    if ("token" in res && res.token) {
      setCreated({ id: res.id, name: name.trim(), token: res.token });
      setName("");
      setCopied(false);
      router.refresh();
    }
  }

  async function revoke(id: string) {
    setError(null);
    const res = await revokeOrgApiKeyAction(id);
    setConfirmRevoke(null);
    if ("error" in res && res.error) {
      setError(res.error);
      return;
    }
    if (created?.id === id) setCreated(null);
    router.refresh();
  }

  async function copy() {
    if (!created) return;
    await navigator.clipboard.writeText(created.token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const columns: DataTableColumn<ApiKeyRow>[] = [
    {
      id: "name",
      header: "Key",
      cell: (k) => (
        <div className="min-w-0">
          <div className="truncate text-body-strong">{k.name}</div>
          <div className="text-mono text-xs text-muted-foreground">
            {k.tokenPrefix}…
          </div>
        </div>
      ),
    },
    {
      id: "scope",
      header: "Scope",
      cell: (k) =>
        k.smtpConfigId ? (
          <span className="text-body">
            {mailboxLabel.get(k.smtpConfigId) ?? "Mailbox"}
          </span>
        ) : (
          <Badge variant="outline">Whole organization</Badge>
        ),
    },
    {
      id: "role",
      header: "Role",
      cell: (k) => (
        <Badge
          variant={ROLE_VARIANT[k.role.toLowerCase()] ?? "pending"}
          className="capitalize"
        >
          {k.role}
        </Badge>
      ),
    },
    {
      id: "used",
      header: "Last used",
      cell: (k) => (
        <span className="text-caption text-muted-foreground">
          {formatDate(k.lastUsedAt)}
          {k.expiresAt && (
            <span className="block">Expires {formatDate(k.expiresAt)}</span>
          )}
        </span>
      ),
    },
    {
      id: "actions",
      align: "right",
      cellClassName: "w-px",
      cell: (k) =>
        canManage &&
        (confirmRevoke === k.id ? (
          <div className="flex gap-1">
            <Button size="sm" variant="destructive" onClick={() => revoke(k.id)}>
              Revoke
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirmRevoke(null)}>
              Keep
            </Button>
          </div>
        ) : (
          <Button size="sm" variant="ghost" onClick={() => setConfirmRevoke(k.id)}>
            Revoke
          </Button>
        )),
    },
  ];

  return (
    <div className="space-y-6">
      {created && (
        <div className="rounded-lg border border-success/30 bg-success-tint p-4">
          <div className="mb-2 flex items-center gap-2">
            <KeyIcon className="size-4 text-success-on" />
            <span className="text-body-strong text-success-on">
              {created.name} created
            </span>
          </div>
          <p className="mb-2 text-caption text-muted-foreground">
            Copy this key now — it won&apos;t be shown again.
          </p>
          <div className="flex gap-2">
            <code className="flex-1 break-all rounded bg-background px-3 py-2 text-mono text-xs">
              {created.token}
            </code>
            <Button size="sm" variant="outline" onClick={copy} aria-label="Copy key">
              {copied ? <CheckIcon className="size-4" /> : <CopyIcon className="size-4" />}
            </Button>
          </div>
        </div>
      )}

      {canManage && (
        <form onSubmit={create} className="space-y-3">
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-48 flex-1 space-y-1.5">
              <Label htmlFor="api-key-name">Name</Label>
              <Input
                id="api-key-name"
                placeholder="e.g. Mail agent reader"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="api-key-scope">Scope</Label>
              <Select value={scope} onValueChange={setScope}>
                <SelectTrigger id="api-key-scope" className="w-60">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ORGANIZATION_SCOPE}>Whole organization</SelectItem>
                  {mailboxes.map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="api-key-role">Role</Label>
              <Select value={keyRole} onValueChange={(v) => setKeyRole(v as ApiKeyRole)}>
                <SelectTrigger id="api-key-role" className="w-32 capitalize">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {grantable.map((r) => (
                    <SelectItem key={r} value={r} className="capitalize">
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button type="submit" loading={busy} disabled={!name.trim()}>
              {!busy && <PlusIcon className="mr-1 size-4" />}
              {busy ? "Creating..." : "Create key"}
            </Button>
          </div>
          <p className="text-caption text-muted-foreground">
            {ROLE_HELP[keyRole]}{" "}
            {scope === ORGANIZATION_SCOPE
              ? "Works across all mailboxes in this organization."
              : "Limited to this mailbox's incoming mail and sending."}
          </p>
        </form>
      )}
      {error && <p className="text-caption text-destructive">{error}</p>}

      {keys.length === 0 ? (
        <p className="text-body text-muted-foreground">No API keys yet.</p>
      ) : (
        <DataTable columns={columns} data={keys} getRowKey={(k) => k.id} />
      )}
    </div>
  );
}
