"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@workspace/ui/components/card";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Label } from "@workspace/ui/components/label";
import { Switch } from "@workspace/ui/components/switch";
import { FormField, FormLabel, FormControl } from "@workspace/ui/components/form-field";
import { toast } from "@workspace/ui/components/sonner";
import { PlugIcon } from "lucide-react";
import {
  updateSmtpConfigAction,
  deleteSmtpConfigAction,
  testImapSavedAction,
} from "./actions";
import { useRouter } from "next/navigation";
import type { SmtpConfigDetail } from "@/lib/types";

export function EditableConfig({ config }: { config: SmtpConfigDetail }) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [testingImap, setTestingImap] = useState(false);
  const [isDefault, setIsDefault] = useState(config.isDefault);

  async function testImap() {
    setTestingImap(true);
    const res = await testImapSavedAction(config.id);
    setTestingImap(false);
    if (res.success) toast.success("IMAP connection works");
    else toast.error(res.error || "IMAP connection failed");
  }

  async function handleSave(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const form = new FormData(e.currentTarget);
    const result = await updateSmtpConfigAction(config.id, form);
    if (result.error) {
      setError(result.error);
      setLoading(false);
    } else {
      setEditing(false);
      setLoading(false);
      router.refresh();
    }
  }

  async function handleDelete() {
    if (!confirm("Delete this SMTP configuration? This cannot be undone.")) return;
    setDeleting(true);
    const result = await deleteSmtpConfigAction(config.id);
    if (result.error) {
      setError(result.error);
      setDeleting(false);
    } else {
      router.push("/dashboard/smtp");
    }
  }

  if (!editing) {
    return (
      <Card>
        <CardHeader className="flex flex-row items-center justify-between border-b">
          <CardTitle>Connection</CardTitle>
          <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
            Edit
          </Button>
        </CardHeader>
        <CardContent className="grid gap-3">
          <Row label="Host" value={`${config.host}:${config.port}`} />
          <Row label="Username" value={config.username} />
          <Row label="From" value={`${config.fromName ?? ""} <${config.fromAddress}>`} />
          <Row label="Default" value={config.isDefault ? "Yes" : "No"} mono={false} />
          <Row
            label="Incoming (IMAP)"
            value={
              config.imapHost
                ? `${config.imapHost}:${config.imapPort ?? 993}`
                : "Not configured"
            }
            mono={!!config.imapHost}
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Edit configuration</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSave} className="space-y-4">
          {error && (
            <div className="rounded-lg bg-destructive/10 px-3 py-2 text-body text-destructive">
              {error}
            </div>
          )}
          <FormField>
            <FormLabel>Display name</FormLabel>
            <FormControl>
              <Input name="name" defaultValue={config.name} required />
            </FormControl>
          </FormField>
          <div className="grid grid-cols-3 gap-3">
            <FormField className="col-span-2">
              <FormLabel>SMTP host</FormLabel>
              <FormControl>
                <Input name="host" defaultValue={config.host} required />
              </FormControl>
            </FormField>
            <FormField>
              <FormLabel>Port</FormLabel>
              <FormControl>
                <Input name="port" type="number" defaultValue={config.port} required />
              </FormControl>
            </FormField>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <FormField>
              <FormLabel>Username</FormLabel>
              <FormControl>
                <Input
                  name="username"
                  defaultValue={config.username}
                  autoComplete="off"
                  required
                />
              </FormControl>
            </FormField>
            <PasswordField
              name="password"
              label="Password"
              isSet
              placeholder="Enter a new SMTP password"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <FormField>
              <FormLabel>From address</FormLabel>
              <FormControl>
                <Input name="fromAddress" type="email" defaultValue={config.fromAddress} required />
              </FormControl>
            </FormField>
            <FormField>
              <FormLabel>From name</FormLabel>
              <FormControl>
                <Input name="fromName" defaultValue={config.fromName ?? ""} />
              </FormControl>
            </FormField>
          </div>
          <div className="space-y-3 rounded-lg border border-dashed border-border p-3">
            <p className="text-eyebrow text-muted-foreground">
              Incoming mail (IMAP) — optional
            </p>
            <div className="grid grid-cols-3 gap-3">
              <FormField className="col-span-2">
                <FormLabel>IMAP host</FormLabel>
                <FormControl>
                  <Input
                    name="imapHost"
                    defaultValue={config.imapHost ?? ""}
                    placeholder="imap.example.com"
                  />
                </FormControl>
              </FormField>
              <FormField>
                <FormLabel>Port</FormLabel>
                <FormControl>
                  <Input
                    name="imapPort"
                    type="number"
                    defaultValue={config.imapPort ?? 993}
                  />
                </FormControl>
              </FormField>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <FormField>
                <FormLabel>IMAP username</FormLabel>
                <FormControl>
                  <Input
                    name="imapUsername"
                    defaultValue={config.imapUsername ?? ""}
                    autoComplete="off"
                    placeholder="Defaults to SMTP username"
                  />
                </FormControl>
              </FormField>
              <PasswordField
                name="imapPassword"
                label="IMAP password"
                isSet={!!config.imapHost}
                placeholder="Enter the IMAP password"
              />
            </div>
            {config.imapHost && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={testImap}
                loading={testingImap}
              >
                <PlugIcon className="mr-1.5 size-4" />
                {testingImap ? "Testing…" : "Test saved IMAP connection"}
              </Button>
            )}
          </div>

          {/* Mirrors the original checkbox: posts "on" when enabled so the
              server action's `formData.get("isDefault") === "on"` holds. */}
          {isDefault && <input type="hidden" name="isDefault" value="on" />}
          <div className="flex items-center justify-between rounded-lg border px-3 py-2.5">
            <Label htmlFor="isDefault" className="cursor-pointer">
              Set as default
            </Label>
            <Switch
              id="isDefault"
              checked={isDefault}
              onCheckedChange={setIsDefault}
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" loading={loading}>
              {loading ? "Saving..." : "Save changes"}
            </Button>
            <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </form>

        <div className="mt-6 border-t pt-4">
          <Button variant="destructive" size="sm" onClick={handleDelete} loading={deleting}>
            {deleting ? "Deleting..." : "Delete configuration"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * Credential field that never overwrites a stored secret by accident. When a
 * secret is already set, the real <input> is not rendered at all until the user
 * clicks "Change" — so browser/password-manager autofill can't populate it and
 * it's never submitted unless the user deliberately enters a new value. The
 * revealed input uses autoComplete="new-password" so managers don't silently
 * fill it either.
 */
function PasswordField({
  name,
  label,
  isSet,
  placeholder = "Enter a new password",
}: {
  name: string;
  label: string;
  isSet: boolean;
  placeholder?: string;
}) {
  // Not-yet-set secrets (e.g. first-time IMAP) are entered directly.
  const [revealed, setRevealed] = useState(!isSet);
  return (
    <FormField>
      <FormLabel>{label}</FormLabel>
      {revealed ? (
        <FormControl>
          <div className="flex items-center gap-2">
            <Input
              name={name}
              type="password"
              autoComplete="new-password"
              placeholder={placeholder}
            />
            {isSet && (
              <button
                type="button"
                onClick={() => setRevealed(false)}
                className="shrink-0 text-caption text-muted-foreground hover:text-foreground"
              >
                Cancel
              </button>
            )}
          </div>
        </FormControl>
      ) : (
        <div className="flex h-9 items-center justify-between gap-2 rounded-md border border-border px-3">
          <span className="tracking-widest text-muted-foreground">••••••••</span>
          <button
            type="button"
            onClick={() => setRevealed(true)}
            className="shrink-0 text-caption font-medium text-brand hover:underline"
          >
            Change
          </button>
        </div>
      )}
    </FormField>
  );
}

function Row({
  label,
  value,
  mono = true,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-caption text-muted-foreground">{label}</span>
      <span className={mono ? "text-mono text-xs" : "text-body-strong"}>{value}</span>
    </div>
  );
}
