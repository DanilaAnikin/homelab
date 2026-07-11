"use client";

import { useRef, useEffect } from "react";
import DOMPurify from "dompurify";
import { cn } from "@workspace/ui/lib/utils";
import {
  BoldIcon,
  ItalicIcon,
  UnderlineIcon,
  LinkIcon,
  ListIcon,
  ListOrderedIcon,
} from "lucide-react";

/**
 * Minimal dependency-free rich-text editor (contentEditable + execCommand).
 * Emits HTML via onChange. `initialHtml` seeds the content once (e.g. a quoted
 * reply / forwarded body); the component is otherwise uncontrolled so typing
 * never fights the cursor.
 */
export function RichTextEditor({
  initialHtml = "",
  onChange,
  placeholder = "Write your message…",
  className,
  minHeight = 180,
}: {
  initialHtml?: string;
  onChange: (html: string) => void;
  placeholder?: string;
  className?: string;
  minHeight?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (ref.current && initialHtml) {
      // initialHtml can contain untrusted email HTML (a quoted/forwarded
      // original). It's set via innerHTML into a live (non-sandboxed)
      // contentEditable, so it MUST be sanitized — strip scripts, event
      // handlers (onerror/onclick…), and javascript: URLs — to prevent XSS in
      // the app origin. DOMPurify runs client-side (this effect never runs on
      // the server). The sanitized HTML is also what gets sent.
      const clean = DOMPurify.sanitize(initialHtml);
      ref.current.innerHTML = clean;
      onChange(clean);
    }
    // Seed once on mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function exec(command: string, value?: string) {
    ref.current?.focus();
    document.execCommand(command, false, value);
    if (ref.current) onChange(ref.current.innerHTML);
  }

  function addLink() {
    const url = window.prompt("Link URL");
    if (url) exec("createLink", url);
  }

  const tools = [
    { icon: BoldIcon, label: "Bold", action: () => exec("bold") },
    { icon: ItalicIcon, label: "Italic", action: () => exec("italic") },
    { icon: UnderlineIcon, label: "Underline", action: () => exec("underline") },
    { icon: LinkIcon, label: "Link", action: addLink },
    {
      icon: ListIcon,
      label: "Bulleted list",
      action: () => exec("insertUnorderedList"),
    },
    {
      icon: ListOrderedIcon,
      label: "Numbered list",
      action: () => exec("insertOrderedList"),
    },
  ];

  return (
    <div
      className={cn(
        "overflow-hidden rounded-md border border-border bg-background focus-within:border-brand-400 focus-within:ring-1 focus-within:ring-brand-400",
        className,
      )}
    >
      <div className="flex flex-wrap items-center gap-0.5 border-b border-border bg-muted/40 px-1.5 py-1">
        {tools.map((t) => (
          <button
            key={t.label}
            type="button"
            aria-label={t.label}
            title={t.label}
            // Keep focus in the editable region so execCommand applies.
            onMouseDown={(e) => e.preventDefault()}
            onClick={t.action}
            className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <t.icon className="size-4" />
          </button>
        ))}
      </div>
      <div
        ref={ref}
        contentEditable
        suppressContentEditableWarning
        role="textbox"
        aria-multiline="true"
        data-placeholder={placeholder}
        onInput={(e) => onChange((e.target as HTMLDivElement).innerHTML)}
        className="prose-email max-w-none px-3 py-2.5 text-body outline-none empty:before:text-muted-foreground empty:before:content-[attr(data-placeholder)]"
        style={{ minHeight }}
      />
    </div>
  );
}
