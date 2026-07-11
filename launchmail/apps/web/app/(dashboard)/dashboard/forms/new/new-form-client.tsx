"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import {
  listDesigns,
  renderPreview,
  renderBlocks,
  renderCustomHtml,
  SAMPLE_DATA,
  type EmailBlock,
} from "@workspace/templates";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Label } from "@workspace/ui/components/label";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@workspace/ui/components/select";
import { Tabs, TabsList, TabsTrigger } from "@workspace/ui/components/tabs";
import {
  FormField,
  FormLabel,
  FormControl,
  FormDescription,
} from "@workspace/ui/components/form-field";
import { EmailPreview } from "@/components/email-preview";
import { createFormAction } from "../actions";

const designs = listDesigns();

interface ConfigOption {
  id: string;
  name: string;
  host: string;
  port: number;
}
interface CustomTemplate {
  id: string;
  name: string;
  mode: "html" | "blocks";
  html: string | null;
  blocks: EmailBlock[] | null;
  accent: string | null;
  theme: "light" | "dark" | null;
}

export function NewFormClient({
  configs,
  customTemplates = [],
}: {
  configs: ConfigOption[];
  customTemplates?: CustomTemplate[];
}) {
  // value is "builtin:<key>" or "custom:<id>"
  const [selected, setSelected] = useState(`builtin:${designs[0]!.key}`);
  const [smtpConfigId, setSmtpConfigId] = useState(configs[0]?.id ?? "");
  const [name, setName] = useState("");
  const [recipients, setRecipients] = useState("");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isCustom = selected.startsWith("custom:");
  const customId = isCustom ? selected.slice(7) : null;
  const builtinKey = !isCustom ? selected.slice(8) : designs[0]!.key;

  const previewHtml = useMemo(() => {
    if (isCustom) {
      const t = customTemplates.find((x) => x.id === customId);
      if (!t) return "";
      return t.mode === "html"
        ? renderCustomHtml(t.html ?? "", SAMPLE_DATA)
        : renderBlocks(t.blocks ?? [], {
            accent: t.accent ?? undefined,
            dark: t.theme === "dark",
            data: SAMPLE_DATA,
          });
    }
    return renderPreview(builtinKey, theme).html;
  }, [isCustom, customId, builtinKey, theme, customTemplates]);

  async function create() {
    setBusy(true);
    setError(null);
    const res = await createFormAction({
      name,
      templateKey: isCustom ? "contact" : builtinKey,
      templateId: customId,
      recipients,
      smtpConfigId,
      theme,
    });
    if (res?.error) {
      setError(res.error);
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div className="space-y-5">
        <FormField>
          <FormLabel>Template</FormLabel>
          <Select value={selected} onValueChange={setSelected}>
            <FormControl>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Select a template" />
              </SelectTrigger>
            </FormControl>
            <SelectContent>
              {customTemplates.length > 0 && (
                <SelectGroup>
                  <SelectLabel>Your templates</SelectLabel>
                  {customTemplates.map((t) => (
                    <SelectItem key={t.id} value={`custom:${t.id}`}>
                      {t.name} — {t.mode === "html" ? "HTML" : "Visual"}
                    </SelectItem>
                  ))}
                </SelectGroup>
              )}
              <SelectGroup>
                <SelectLabel>Built-in designs</SelectLabel>
                {designs.map((d) => (
                  <SelectItem key={d.key} value={`builtin:${d.key}`}>
                    {d.name} — {d.category}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          {customTemplates.length === 0 && (
            <FormDescription>
              Build your own in{" "}
              <Link href="/dashboard/templates" className="text-primary underline-offset-2 hover:underline">
                Templates
              </Link>{" "}
              and they&apos;ll appear here.
            </FormDescription>
          )}
        </FormField>

        <FormField>
          <FormLabel>SMTP connection</FormLabel>
          <Select value={smtpConfigId} onValueChange={setSmtpConfigId}>
            <FormControl>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Select a connection" />
              </SelectTrigger>
            </FormControl>
            <SelectContent>
              {configs.map((c) => (
                <SelectItem key={c.id} value={c.id}>
                  {c.name} ({c.host}:{c.port})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FormDescription>
            Submissions are delivered through this connection.
          </FormDescription>
        </FormField>

        <FormField>
          <FormLabel>Form name</FormLabel>
          <FormControl>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Contact form"
            />
          </FormControl>
        </FormField>

        <FormField>
          <FormLabel>Send submissions to</FormLabel>
          <FormControl>
            <Input
              value={recipients}
              onChange={(e) => setRecipients(e.target.value)}
              placeholder="you@example.com (comma-separated; defaults to you)"
            />
          </FormControl>
        </FormField>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <Button
          onClick={create}
          loading={busy}
          disabled={!name.trim() || !smtpConfigId}
        >
          Create form
        </Button>
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label className="text-muted-foreground">Preview</Label>
          {!isCustom && (
            <Tabs
              value={theme}
              onValueChange={(v) => setTheme(v as "light" | "dark")}
            >
              <TabsList>
                <TabsTrigger value="light">Light</TabsTrigger>
                <TabsTrigger value="dark">Dark</TabsTrigger>
              </TabsList>
            </Tabs>
          )}
        </div>
        <EmailPreview html={previewHtml} />
      </div>
    </div>
  );
}
