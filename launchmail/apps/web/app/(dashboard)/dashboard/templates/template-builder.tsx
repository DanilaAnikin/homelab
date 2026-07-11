"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { renderBlocks, renderCustomHtml, type EmailBlock } from "@workspace/templates";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Label } from "@workspace/ui/components/label";
import { Tabs, TabsList, TabsTrigger } from "@workspace/ui/components/tabs";
import {
  FormField,
  FormLabel,
  FormControl,
} from "@workspace/ui/components/form-field";
import { Card, CardContent } from "@workspace/ui/components/card";
import { toast } from "@workspace/ui/components/sonner";
import {
  ArrowUpIcon,
  ArrowDownIcon,
  Trash2Icon,
  Heading1Icon,
  TypeIcon,
  MousePointerClickIcon,
  ImageIcon,
  MinusIcon,
  MoveVerticalIcon,
  TrashIcon,
} from "lucide-react";
import { EmailPreview } from "@/components/email-preview";
import {
  createTemplateAction,
  updateTemplateAction,
  deleteTemplateAction,
  type TemplatePayload,
} from "./actions";

const SAMPLE: Record<string, string> = {
  name: "Jane Doe",
  email: "jane@example.com",
  company: "Acme Inc",
};

type Mode = "blocks" | "html";

interface Existing {
  id: string;
  name: string;
  mode: Mode;
  subject: string | null;
  html: string | null;
  blocks: EmailBlock[] | null;
  accent: string | null;
  theme: "light" | "dark" | null;
}

const nid = () =>
  typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `b_${Math.floor(performance.now() * 1000)}`;

function defaultBlock(type: EmailBlock["type"]): EmailBlock {
  switch (type) {
    case "heading":
      return { id: nid(), type, text: "Welcome, {{name}}" };
    case "text":
      return {
        id: nid(),
        type,
        text: "Write your message here. Use {{name}} or {{email}} for variables.",
      };
    case "button":
      return { id: nid(), type, text: "Get started", url: "https://example.com" };
    case "image":
      return { id: nid(), type, url: "https://placehold.co/560x200", alt: "" };
    case "divider":
      return { id: nid(), type };
    case "spacer":
      return { id: nid(), type, size: 24 };
  }
}

const BLOCK_TYPES: { type: EmailBlock["type"]; label: string; icon: typeof TypeIcon }[] = [
  { type: "heading", label: "Heading", icon: Heading1Icon },
  { type: "text", label: "Text", icon: TypeIcon },
  { type: "button", label: "Button", icon: MousePointerClickIcon },
  { type: "image", label: "Image", icon: ImageIcon },
  { type: "divider", label: "Divider", icon: MinusIcon },
  { type: "spacer", label: "Spacer", icon: MoveVerticalIcon },
];

export function TemplateBuilder({ template }: { template?: Existing }) {
  const router = useRouter();
  const [name, setName] = useState(template?.name ?? "Untitled template");
  const [mode, setMode] = useState<Mode>(template?.mode ?? "blocks");
  const [subject, setSubject] = useState(template?.subject ?? "");
  const [accent, setAccent] = useState(template?.accent ?? "#5b5bd6");
  const [theme, setTheme] = useState<"light" | "dark">(template?.theme ?? "light");
  const [blocks, setBlocks] = useState<EmailBlock[]>(
    template?.blocks ?? [
      defaultBlock("heading"),
      defaultBlock("text"),
      defaultBlock("button"),
    ],
  );
  const [html, setHtml] = useState(
    template?.html ??
      "<h1>Hello {{name}}</h1>\n<p>Write your email in HTML. Variables like {{email}} are substituted at send time.</p>",
  );
  const [busy, setBusy] = useState(false);

  const previewHtml = useMemo(() => {
    if (mode === "html") return renderCustomHtml(html, SAMPLE);
    return renderBlocks(blocks, {
      accent,
      dark: theme === "dark",
      brandName: "LaunchMail",
      data: SAMPLE,
    });
  }, [mode, html, blocks, accent, theme]);

  function addBlock(type: EmailBlock["type"]) {
    setBlocks((b) => [...b, defaultBlock(type)]);
  }
  function patchBlock(id: string, patch: Partial<EmailBlock>) {
    setBlocks((b) =>
      b.map((x) => (x.id === id ? ({ ...x, ...patch } as EmailBlock) : x)),
    );
  }
  function move(id: string, dir: -1 | 1) {
    setBlocks((b) => {
      const i = b.findIndex((x) => x.id === id);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= b.length) return b;
      const copy = [...b];
      [copy[i], copy[j]] = [copy[j]!, copy[i]!];
      return copy;
    });
  }
  function remove(id: string) {
    setBlocks((b) => b.filter((x) => x.id !== id));
  }

  async function save() {
    setBusy(true);
    const payload: TemplatePayload = {
      name: name.trim() || "Untitled template",
      mode,
      subject: subject || null,
      accent,
      theme,
      html: mode === "html" ? html : null,
      blocks: mode === "blocks" ? blocks : null,
    };
    const res = template
      ? await updateTemplateAction(template.id, payload)
      : await createTemplateAction(payload);
    if (res?.error) {
      toast.error(res.error);
      setBusy(false);
      return;
    }
    toast.success(template ? "Template saved" : "Template created");
    setBusy(false);
    router.refresh();
  }

  async function destroy() {
    if (!template) return;
    if (!confirm("Delete this template?")) return;
    setBusy(true);
    const res = await deleteTemplateAction(template.id);
    if (res?.error) {
      toast.error(res.error);
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_minmax(360px,460px)]">
      <div className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <FormField>
            <FormLabel>Template name</FormLabel>
            <FormControl>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </FormControl>
          </FormField>
          <FormField>
            <FormLabel>Subject</FormLabel>
            <FormControl>
              <Input
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                placeholder="e.g. Welcome to {{company}}"
              />
            </FormControl>
          </FormField>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Tabs value={mode} onValueChange={(v) => setMode(v as Mode)}>
            <TabsList>
              <TabsTrigger value="blocks">Visual</TabsTrigger>
              <TabsTrigger value="html">HTML</TabsTrigger>
            </TabsList>
          </Tabs>
          <Tabs
            value={theme}
            onValueChange={(v) => setTheme(v as "light" | "dark")}
          >
            <TabsList>
              <TabsTrigger value="light">Light</TabsTrigger>
              <TabsTrigger value="dark">Dark</TabsTrigger>
            </TabsList>
          </Tabs>
          <div className="flex items-center gap-2">
            <Label htmlFor="accent" className="text-xs text-muted-foreground">
              Accent
            </Label>
            <label className="relative size-8 shrink-0 overflow-hidden rounded-md border border-input">
              <span
                className="block size-full"
                style={{ backgroundColor: accent }}
              />
              <input
                id="accent"
                type="color"
                value={accent}
                onChange={(e) => setAccent(e.target.value)}
                aria-label="Accent color"
                className="absolute inset-0 size-full cursor-pointer opacity-0"
              />
            </label>
            <Input
              value={accent}
              onChange={(e) => setAccent(e.target.value)}
              aria-label="Accent hex"
              className="h-8 w-28 font-mono text-xs"
            />
          </div>
        </div>

        {mode === "html" ? (
          <FormField>
            <FormLabel>HTML</FormLabel>
            <FormControl>
              <textarea
                value={html}
                onChange={(e) => setHtml(e.target.value)}
                spellCheck={false}
                className="h-[420px] w-full rounded-lg border bg-background p-3 font-mono text-xs leading-relaxed shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            </FormControl>
            <p className="text-xs text-muted-foreground">
              Use <code className="rounded bg-muted px-1">{"{{name}}"}</code>,{" "}
              <code className="rounded bg-muted px-1">{"{{email}}"}</code> etc. —
              substituted with submission/recipient data at send time.
            </p>
          </FormField>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-1.5">
              {BLOCK_TYPES.map((b) => (
                <Button
                  key={b.type}
                  variant="outline"
                  size="sm"
                  onClick={() => addBlock(b.type)}
                >
                  <b.icon className="mr-1 size-3.5" />
                  {b.label}
                </Button>
              ))}
            </div>
            <div className="space-y-2">
              {blocks.length === 0 && (
                <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                  Add blocks above to start building.
                </p>
              )}
              {blocks.map((block, i) => (
                <Card key={block.id} size="sm">
                  <CardContent className="space-y-2 p-3">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        {block.type}
                      </span>
                      <div className="flex items-center gap-0.5">
                        <button
                          onClick={() => move(block.id, -1)}
                          disabled={i === 0}
                          className="rounded p-1 text-muted-foreground hover:bg-muted disabled:opacity-30"
                        >
                          <ArrowUpIcon className="size-3.5" />
                        </button>
                        <button
                          onClick={() => move(block.id, 1)}
                          disabled={i === blocks.length - 1}
                          className="rounded p-1 text-muted-foreground hover:bg-muted disabled:opacity-30"
                        >
                          <ArrowDownIcon className="size-3.5" />
                        </button>
                        <button
                          onClick={() => remove(block.id)}
                          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive"
                        >
                          <Trash2Icon className="size-3.5" />
                        </button>
                      </div>
                    </div>
                    <BlockFields block={block} onChange={(p) => patchBlock(block.id, p)} />
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 border-t pt-4">
          <Button onClick={save} loading={busy}>
            {template ? "Save template" : "Create template"}
          </Button>
          {template && (
            <Button
              variant="ghost"
              className="ml-auto text-destructive hover:text-destructive"
              onClick={destroy}
              disabled={busy}
            >
              <TrashIcon className="mr-1 size-4" />
              Delete
            </Button>
          )}
        </div>
      </div>

      <div className="space-y-2">
        <Label className="text-muted-foreground">Live preview</Label>
        <div className="lg:sticky lg:top-8">
          <EmailPreview html={previewHtml} subject={subject || name} />
        </div>
      </div>
    </div>
  );
}

function BlockFields({
  block,
  onChange,
}: {
  block: EmailBlock;
  onChange: (patch: Partial<EmailBlock>) => void;
}) {
  if (block.type === "heading" || block.type === "text") {
    return (
      <textarea
        value={block.text}
        onChange={(e) => onChange({ text: e.target.value } as Partial<EmailBlock>)}
        rows={block.type === "text" ? 3 : 1}
        className="w-full resize-none rounded-md border bg-background px-2.5 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      />
    );
  }
  if (block.type === "button") {
    return (
      <div className="grid grid-cols-2 gap-2">
        <Input
          value={block.text}
          placeholder="Label"
          onChange={(e) => onChange({ text: e.target.value } as Partial<EmailBlock>)}
        />
        <Input
          value={block.url}
          placeholder="https://"
          onChange={(e) => onChange({ url: e.target.value } as Partial<EmailBlock>)}
        />
      </div>
    );
  }
  if (block.type === "image") {
    return (
      <div className="grid grid-cols-2 gap-2">
        <Input
          value={block.url}
          placeholder="Image URL"
          onChange={(e) => onChange({ url: e.target.value } as Partial<EmailBlock>)}
        />
        <Input
          value={block.alt ?? ""}
          placeholder="Alt text"
          onChange={(e) => onChange({ alt: e.target.value } as Partial<EmailBlock>)}
        />
      </div>
    );
  }
  if (block.type === "spacer") {
    return (
      <div className="flex items-center gap-2">
        <Label className="text-xs text-muted-foreground">Height</Label>
        <Input
          type="number"
          value={block.size ?? 24}
          onChange={(e) =>
            onChange({ size: Number(e.target.value) } as Partial<EmailBlock>)
          }
          className="w-24"
        />
      </div>
    );
  }
  return <p className="text-xs text-muted-foreground">No options</p>;
}
