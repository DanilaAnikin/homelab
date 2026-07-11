"use client";

import { useMemo, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import {
  getDesign,
  listDesigns,
  renderForm,
  SAMPLE_DATA,
  DEFAULT_DESIGN_KEY,
} from "@workspace/templates";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Label } from "@workspace/ui/components/label";
import { Switch } from "@workspace/ui/components/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@workspace/ui/components/select";
import { Tabs, TabsList, TabsTrigger } from "@workspace/ui/components/tabs";
import { FormField, FormLabel } from "@workspace/ui/components/form-field";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@workspace/ui/components/card";
import { EmailPreview } from "@/components/email-preview";
import { toast } from "@workspace/ui/components/sonner";
import { updateFormAction, deleteFormAction } from "../actions";

type Role = "admin" | "writer" | "reader";

interface FormView {
  id: string;
  name: string;
  templateKey: string;
  endpointToken: string;
  recipients: string[];
  subject: string | null;
  replyToField: string | null;
  branding: {
    brandName?: string;
    accent?: string;
    footer?: string;
    theme?: "light" | "dark";
  };
  settings: {
    honeypot?: boolean;
    redirectUrl?: string;
    allowedOrigins?: string[];
    storeSubmissions?: boolean;
    maxPerHour?: number;
    autoRespond?: boolean;
    autoRespondSubject?: string;
    autoRespondMessage?: string;
  };
  enabled: boolean;
}

const designs = listDesigns();

// origin is read from the browser without a setState-in-effect: the snapshot is
// stable so React never re-subscribes, and the server snapshot is empty.
const noopSubscribe = () => () => {};
const getOriginSnapshot = () => window.location.origin;
const getServerOriginSnapshot = () => "";

export function FormEditor({ form, role }: { form: FormView; role: Role }) {
  const router = useRouter();
  const canEdit = role === "admin" || role === "writer";

  const [name, setName] = useState(form.name);
  const [templateKey, setTemplateKey] = useState(form.templateKey);
  const [recipients, setRecipients] = useState(form.recipients.join(", "));
  const [subject, setSubject] = useState(form.subject ?? "");
  const [brandName, setBrandName] = useState(form.branding.brandName ?? "");
  const [accent, setAccent] = useState(form.branding.accent ?? "");
  const [theme, setTheme] = useState<"light" | "dark">(
    form.branding.theme ?? "light",
  );
  const [redirectUrl, setRedirectUrl] = useState(
    form.settings.redirectUrl ?? "",
  );
  const [allowedOrigins, setAllowedOrigins] = useState(
    (form.settings.allowedOrigins ?? []).join(", "),
  );
  const [autoRespond, setAutoRespond] = useState(
    form.settings.autoRespond ?? false,
  );
  const [autoRespondSubject, setAutoRespondSubject] = useState(
    form.settings.autoRespondSubject ?? "Thanks for reaching out",
  );
  const [autoRespondMessage, setAutoRespondMessage] = useState(
    form.settings.autoRespondMessage ??
      "Thanks for your submission — we'll be in touch soon.",
  );
  const [enabled, setEnabled] = useState(form.enabled);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const origin = useSyncExternalStore(
    noopSubscribe,
    getOriginSnapshot,
    getServerOriginSnapshot,
  );

  const endpoint = `${origin}/f/${form.endpointToken}`;

  const previewHtml = useMemo(() => {
    const design = getDesign(templateKey) ?? getDesign(DEFAULT_DESIGN_KEY)!;
    return renderForm({
      design,
      branding: {
        brandName: brandName || undefined,
        accent: accent || undefined,
        theme,
      },
      subjectTemplate: subject || undefined,
      data: SAMPLE_DATA,
      submittedAt: "just now",
    }).html;
  }, [templateKey, brandName, accent, subject, theme]);

  async function save() {
    const recipientList = recipients
      .split(/[,\s]+/)
      .map((r) => r.trim())
      .filter(Boolean);
    if (recipientList.length === 0) {
      toast.error("Add at least one recipient email");
      return;
    }
    setBusy(true);
    setSaved(false);
    const res = await updateFormAction(form.id, {
      name,
      templateKey,
      recipients: recipientList,
      subject: subject || null,
      branding: {
        brandName: brandName || undefined,
        accent: accent || undefined,
        theme,
      },
      settings: {
        ...form.settings,
        autoRespond,
        autoRespondSubject: autoRespondSubject || undefined,
        autoRespondMessage: autoRespondMessage || undefined,
        redirectUrl: redirectUrl || undefined,
        allowedOrigins: allowedOrigins
          .split(/[,\s]+/)
          .map((o) => o.trim())
          .filter(Boolean),
      },
      enabled,
    });
    setBusy(false);
    if (res?.error) {
      toast.error(res.error);
      return;
    }
    setSaved(true);
    toast.success("Form saved");
    router.refresh();
  }

  async function remove() {
    if (!confirm("Delete this form and its submissions?")) return;
    setBusy(true);
    const res = await deleteFormAction(form.id);
    if (res?.error) {
      toast.error(res.error);
      setBusy(false);
    }
  }

  const curl = `curl -X POST ${endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{"name":"Jane","email":"jane@example.com","message":"Hi!"}'`;

  const htmlForm = `<form action="${endpoint}" method="POST">
  <input name="email" type="email" />
  <textarea name="message"></textarea>
  <button type="submit">Send</button>
</form>`;

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_minmax(320px,420px)]">
      <div className="space-y-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <FormField>
              <FormLabel>Name</FormLabel>
              <Input value={name} onChange={(e) => setName(e.target.value)} disabled={!canEdit} />
            </FormField>
            <FormField>
              <FormLabel>Template</FormLabel>
              <Select
                value={templateKey}
                onValueChange={setTemplateKey}
                disabled={!canEdit}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Select a template" />
                </SelectTrigger>
                <SelectContent>
                  {designs.map((d) => (
                    <SelectItem key={d.key} value={d.key}>
                      {d.name} ({d.category})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField>
              <FormLabel>Send submissions to (comma-separated)</FormLabel>
              <Input value={recipients} onChange={(e) => setRecipients(e.target.value)} disabled={!canEdit} />
            </FormField>
            <FormField>
              <FormLabel>Subject template (supports {"{{field}}"})</FormLabel>
              <Input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="New submission from {{name}}" disabled={!canEdit} />
            </FormField>
            <div className="grid grid-cols-2 gap-3">
              <FormField>
                <FormLabel>Brand name</FormLabel>
                <Input value={brandName} onChange={(e) => setBrandName(e.target.value)} disabled={!canEdit} />
              </FormField>
              <FormField>
                <FormLabel>Accent color</FormLabel>
                <div className="flex items-center gap-2">
                  <label className="relative size-9 shrink-0 overflow-hidden rounded-md border border-input">
                    <span
                      className="block size-full"
                      style={{ backgroundColor: accent || "#2563eb" }}
                    />
                    <input
                      type="color"
                      value={accent || "#2563eb"}
                      onChange={(e) => setAccent(e.target.value)}
                      disabled={!canEdit}
                      aria-label="Accent color"
                      className="absolute inset-0 size-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
                    />
                  </label>
                  <Input
                    value={accent}
                    onChange={(e) => setAccent(e.target.value)}
                    placeholder="#2563eb"
                    disabled={!canEdit}
                    className="font-mono"
                  />
                </div>
              </FormField>
            </div>
            <FormField>
              <FormLabel>Redirect URL after submit (optional)</FormLabel>
              <Input value={redirectUrl} onChange={(e) => setRedirectUrl(e.target.value)} placeholder="https://yoursite.com/thanks" disabled={!canEdit} />
            </FormField>
            <FormField>
              <FormLabel>Allowed origins (comma-separated, blank = any)</FormLabel>
              <Input value={allowedOrigins} onChange={(e) => setAllowedOrigins(e.target.value)} placeholder="https://yoursite.com" disabled={!canEdit} />
            </FormField>

            <div className="space-y-3 rounded-lg border p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                  <Label htmlFor="auto-respond" className="text-sm font-medium">
                    Auto-responder
                  </Label>
                  <p className="text-caption text-muted-foreground">
                    Send a confirmation email back to the submitter (their{" "}
                    <code className="rounded bg-muted px-1">email</code> field).
                  </p>
                </div>
                <Switch
                  id="auto-respond"
                  checked={autoRespond}
                  onCheckedChange={setAutoRespond}
                  disabled={!canEdit}
                />
              </div>
              {autoRespond && (
                <div className="space-y-2 pt-1">
                  <Input value={autoRespondSubject} onChange={(e) => setAutoRespondSubject(e.target.value)} placeholder="Subject" disabled={!canEdit} />
                  <textarea
                    value={autoRespondMessage}
                    onChange={(e) => setAutoRespondMessage(e.target.value)}
                    disabled={!canEdit}
                    rows={3}
                    className="w-full rounded-md border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    placeholder="Thanks {{name}}, we'll be in touch soon."
                  />
                </div>
              )}
            </div>

            <div className="flex items-center justify-between gap-3">
              <Label htmlFor="form-enabled" className="text-sm">
                Enabled
              </Label>
              <Switch
                id="form-enabled"
                checked={enabled}
                onCheckedChange={setEnabled}
                disabled={!canEdit}
              />
            </div>

            {canEdit && (
              <div className="flex items-center gap-2 pt-1">
                <Button onClick={save} loading={busy}>
                  Save changes
                </Button>
                {saved && <span className="text-sm text-success">Saved</span>}
                <Button variant="ghost" className="ml-auto text-destructive" onClick={remove} disabled={busy}>
                  Delete
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Endpoint</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <code className="block break-all rounded bg-muted px-2 py-1.5 font-mono text-xs">
              {endpoint || "…"}
            </code>
            <div>
              <p className="mb-1 text-xs text-muted-foreground">HTML form</p>
              <pre className="overflow-x-auto rounded bg-muted p-2 text-xs">{htmlForm}</pre>
            </div>
            <div>
              <p className="mb-1 text-xs text-muted-foreground">curl</p>
              <pre className="overflow-x-auto rounded bg-muted p-2 text-xs">{curl}</pre>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label>Live preview</Label>
          <Tabs
            value={theme}
            onValueChange={(v) => canEdit && setTheme(v as "light" | "dark")}
          >
            <TabsList>
              <TabsTrigger value="light" disabled={!canEdit}>
                Light
              </TabsTrigger>
              <TabsTrigger value="dark" disabled={!canEdit}>
                Dark
              </TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
        <EmailPreview html={previewHtml} />
      </div>
    </div>
  );
}
