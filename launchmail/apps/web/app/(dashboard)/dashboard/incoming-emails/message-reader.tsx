"use client";

import { useState } from "react";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@workspace/ui/components/dropdown-menu";
import { toast } from "@workspace/ui/components/sonner";
import { cn } from "@workspace/ui/lib/utils";
import {
  CornerUpLeftIcon,
  ForwardIcon,
  StarIcon,
  ArchiveIcon,
  ArchiveRestoreIcon,
  MailOpenIcon,
  MoreHorizontalIcon,
  Trash2Icon,
  ShieldBanIcon,
  PaperclipIcon,
  DownloadIcon,
  XIcon,
  ImageIcon,
  SendIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { RichTextEditor } from "@/components/rich-text-editor";
import { blockRemoteImages } from "@/lib/email-html";
import { Avatar } from "./mail-client";
import {
  replyAction,
  forwardAction,
  archiveAction,
  deleteMessageAction,
  blockSenderAction,
  starAction,
  markReadAction,
  loadMessageAction,
  type IncomingEmailFull,
  type OutgoingAttachment,
} from "./actions";

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function htmlToText(html: string): string {
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|li|tr)>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .trim();
}

function originalBody(detail: IncomingEmailFull): string {
  if (detail.html) return detail.html;
  if (detail.text)
    return `<pre style="white-space:pre-wrap;font-family:inherit">${escapeHtml(
      detail.text,
    )}</pre>`;
  return "";
}

function quotedReply(detail: IncomingEmailFull): string {
  const when = new Date(detail.receivedAt).toLocaleString();
  const who = detail.fromName
    ? `${detail.fromName} <${detail.fromAddress}>`
    : detail.fromAddress;
  return `<p><br></p><blockquote style="margin:0 0 0 0.8ex;border-left:2px solid #ddd;padding-left:1ex;color:#666">On ${when}, ${escapeHtml(
    who,
  )} wrote:<br>${originalBody(detail)}</blockquote>`;
}

function forwardedBody(detail: IncomingEmailFull): string {
  const when = new Date(detail.receivedAt).toLocaleString();
  const who = detail.fromName
    ? `${detail.fromName} <${detail.fromAddress}>`
    : detail.fromAddress;
  const to = detail.toAddresses.map((t) => t.email).join(", ");
  return `<p><br></p><div style="color:#666">---------- Forwarded message ----------<br>From: ${escapeHtml(
    who,
  )}<br>Date: ${when}<br>Subject: ${escapeHtml(
    detail.subject ?? "",
  )}<br>To: ${escapeHtml(to)}</div><br>${originalBody(detail)}`;
}

function fileToAttachment(file: File): Promise<OutgoingAttachment> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const res = String(r.result);
      resolve({
        filename: file.name,
        content: res.slice(res.indexOf(",") + 1),
        contentType: file.type || undefined,
      });
    };
    r.onerror = reject;
    r.readAsDataURL(file);
  });
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function MessageReader({
  detail,
  onChanged,
  onClose,
  onDetail,
}: {
  detail: IncomingEmailFull;
  onChanged: () => void;
  onClose: () => void;
  onDetail: (d: IncomingEmailFull) => void;
}) {
  const [showImages, setShowImages] = useState(false);
  const [compose, setCompose] = useState<"reply" | "forward" | null>(null);

  const who = detail.fromName || detail.fromAddress;
  const blockedResult = detail.html
    ? blockRemoteImages(detail.html)
    : { html: "", blocked: false };

  async function act(fn: () => Promise<{ error?: string } | unknown>, close?: boolean) {
    const res = (await fn()) as { error?: string };
    if (res?.error) return toast.error(res.error);
    onChanged();
    if (close) onClose();
  }

  async function reloadDetail() {
    const updated = await loadMessageAction(detail.id);
    if (updated) onDetail(updated);
    onChanged();
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-card-title leading-snug">
          {detail.subject || "(no subject)"}
        </h2>
        <div className="mt-3 flex items-start gap-3">
          <Avatar name={who} className="size-10" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-body-strong">{who}</p>
            <p className="truncate text-caption text-muted-foreground">
              {detail.fromAddress}
            </p>
            <p className="mt-0.5 truncate text-caption text-muted-foreground">
              to {detail.toAddresses.map((t) => t.email).join(", ") || "you"} ·{" "}
              {new Date(detail.receivedAt).toLocaleString()}
            </p>
          </div>
        </div>

        {/* Action toolbar */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <Button size="sm" onClick={() => setCompose("reply")}>
            <CornerUpLeftIcon className="mr-1.5 size-4" />
            Reply
          </Button>
          <Button size="sm" variant="outline" onClick={() => setCompose("forward")}>
            <ForwardIcon className="mr-1.5 size-4" />
            Forward
          </Button>
          <IconBtn
            label={detail.starred ? "Unstar" : "Star"}
            onClick={() => act(() => starAction(detail.id, !detail.starred)).then(reloadDetail)}
          >
            <StarIcon
              className={cn("size-4", detail.starred && "fill-warning text-warning")}
            />
          </IconBtn>
          <IconBtn
            label={detail.archived ? "Unarchive" : "Archive"}
            // Always close after (un)archiving: the message leaves the current
            // folder view, so keeping the drawer open would show a stale row.
            onClick={() => act(() => archiveAction(detail.id, !detail.archived), true)}
          >
            {detail.archived ? (
              <ArchiveRestoreIcon className="size-4" />
            ) : (
              <ArchiveIcon className="size-4" />
            )}
          </IconBtn>
          <IconBtn
            label="Mark unread"
            onClick={() => act(() => markReadAction(detail.id, false), true)}
          >
            <MailOpenIcon className="size-4" />
          </IconBtn>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label="More actions">
                <MoreHorizontalIcon className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() =>
                  act(() => blockSenderAction(detail.id), true)
                }
              >
                <ShieldBanIcon className="mr-2 size-4" />
                Block sender
              </DropdownMenuItem>
              <DropdownMenuItem
                variant="destructive"
                onClick={() => {
                  if (confirm("Delete this message?"))
                    act(() => deleteMessageAction(detail.id), true);
                }}
              >
                <Trash2Icon className="mr-2 size-4" />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {detail.repliedAt && (
            <span className="ml-auto flex items-center gap-1 text-caption text-success">
              <CornerUpLeftIcon className="size-3.5" />
              Replied
            </span>
          )}
        </div>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {detail.contentTruncated && (
          <div
            role="status"
            className="flex items-start gap-2 border-b border-border bg-warning-tint/40 px-5 py-2 text-caption text-warning-on"
          >
            <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
            <span>
              {detail.sourceTruncated
                ? "This message exceeded the safe receive limit. Only a bounded preview is shown, and attachment downloads are unavailable."
                : "Long message content was shortened to keep the inbox response safe."}
            </span>
          </div>
        )}
        {blockedResult.blocked && !showImages && (
          <div className="flex items-center justify-between gap-3 border-b border-border bg-warning-tint/40 px-5 py-2 text-caption">
            <span className="flex items-center gap-1.5 text-warning-on">
              <ImageIcon className="size-3.5" />
              Remote images blocked for your privacy.
            </span>
            <button
              type="button"
              onClick={() => setShowImages(true)}
              className="font-medium text-brand hover:underline"
            >
              Show images
            </button>
          </div>
        )}
        {detail.html ? (
          <iframe
            title="Email body"
            sandbox=""
            srcDoc={showImages ? detail.html : blockedResult.html}
            className="h-[48vh] w-full border-0 bg-white"
          />
        ) : (
          <pre className="px-5 py-4 font-sans text-body whitespace-pre-wrap break-words">
            {detail.text || "(empty message)"}
          </pre>
        )}

        {detail.attachments && detail.attachments.length > 0 && (
          <div className="space-y-2 border-t border-border px-5 py-4">
            <p className="text-eyebrow text-muted-foreground">
              {detail.attachments.length} attachment
              {detail.attachments.length > 1 ? "s" : ""}
            </p>
            <div className="flex flex-wrap gap-2">
              {detail.attachments.map((a, i) => {
                const content = (
                  <>
                    <PaperclipIcon className="size-4 shrink-0 text-muted-foreground" />
                    <span className="max-w-[14rem] truncate">{a.filename}</span>
                    <span className="text-caption tabular-nums text-muted-foreground">
                      {formatBytes(a.size)}
                    </span>
                    {!detail.sourceTruncated && (
                      <DownloadIcon className="size-3.5 text-muted-foreground" />
                    )}
                  </>
                );
                return detail.sourceTruncated ? (
                  <span
                    key={i}
                    title="Download unavailable because the source was truncated"
                    className="flex cursor-not-allowed items-center gap-2 rounded-md border border-border px-3 py-2 text-body opacity-60"
                  >
                    {content}
                  </span>
                ) : (
                  <a
                    key={i}
                    href={`/dashboard/incoming-emails/${detail.id}/attachments/${i}`}
                    className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-body transition-colors hover:bg-muted/50"
                  >
                    {content}
                  </a>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Composer */}
      {compose && (
        <Composer
          mode={compose}
          detail={detail}
          onCancel={() => setCompose(null)}
          onSent={async () => {
            setCompose(null);
            if (compose === "reply") await reloadDetail();
            else onChanged();
          }}
        />
      )}
    </div>
  );
}

function IconBtn({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Button
      size="icon-sm"
      variant="ghost"
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}

function Composer({
  mode,
  detail,
  onCancel,
  onSent,
}: {
  mode: "reply" | "forward";
  detail: IncomingEmailFull;
  onCancel: () => void;
  onSent: () => void;
}) {
  const [to, setTo] = useState("");
  const [cc, setCc] = useState("");
  const [bcc, setBcc] = useState("");
  const [showCcBcc, setShowCcBcc] = useState(false);
  const [html, setHtml] = useState("");
  const [files, setFiles] = useState<OutgoingAttachment[]>([]);
  const [sending, setSending] = useState(false);

  const initial = mode === "reply" ? quotedReply(detail) : forwardedBody(detail);

  async function addFiles(list: FileList | null) {
    if (!list) return;
    const added = await Promise.all(Array.from(list).map(fileToAttachment));
    setFiles((f) => [...f, ...added]);
  }

  async function send() {
    const text = htmlToText(html);
    if (!text.trim() && mode === "reply") {
      toast.error("Write a reply first");
      return;
    }
    if (mode === "forward" && !to.trim()) {
      toast.error("Add at least one recipient");
      return;
    }
    setSending(true);
    const payload = {
      html,
      text,
      cc: cc || undefined,
      bcc: bcc || undefined,
      attachments: files.length ? files : undefined,
    };
    const res =
      mode === "reply"
        ? await replyAction(detail.id, payload)
        : await forwardAction(detail.id, { to, ...payload });
    setSending(false);
    if ("error" in res) return toast.error(res.error ?? "Failed to send");
    toast.success(mode === "reply" ? "Reply sent" : "Forwarded");
    onSent();
  }

  return (
    <div className="space-y-2.5 border-t border-border bg-muted/30 px-5 py-3">
      <div className="flex items-center justify-between">
        <p className="flex items-center gap-1.5 text-caption font-medium text-foreground">
          {mode === "reply" ? (
            <>
              <CornerUpLeftIcon className="size-3.5" /> Reply to {detail.fromName || detail.fromAddress}
            </>
          ) : (
            <>
              <ForwardIcon className="size-3.5" /> Forward
            </>
          )}
        </p>
        <button
          type="button"
          onClick={onCancel}
          aria-label="Close composer"
          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <XIcon className="size-4" />
        </button>
      </div>

      {mode === "forward" && (
        <Input
          value={to}
          onChange={(e) => setTo(e.target.value)}
          placeholder="To (comma-separated)"
        />
      )}
      {showCcBcc ? (
        <div className="grid grid-cols-2 gap-2">
          <Input value={cc} onChange={(e) => setCc(e.target.value)} placeholder="Cc" />
          <Input value={bcc} onChange={(e) => setBcc(e.target.value)} placeholder="Bcc" />
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setShowCcBcc(true)}
          className="text-caption text-muted-foreground hover:text-foreground"
        >
          + Cc / Bcc
        </button>
      )}

      <RichTextEditor
        initialHtml={initial}
        onChange={setHtml}
        minHeight={150}
        placeholder={mode === "reply" ? "Write your reply…" : "Add a message…"}
      />

      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {files.map((f, i) => (
            <span
              key={i}
              className="flex items-center gap-1.5 rounded-badge border border-border bg-background px-2 py-1 text-caption"
            >
              <PaperclipIcon className="size-3" />
              <span className="max-w-[10rem] truncate">{f.filename}</span>
              <button
                type="button"
                onClick={() => setFiles((arr) => arr.filter((_, j) => j !== i))}
                aria-label="Remove attachment"
                className="text-muted-foreground hover:text-destructive"
              >
                <XIcon className="size-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between">
        <label className="flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-caption text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
          <PaperclipIcon className="size-4" />
          Attach
          <input
            type="file"
            multiple
            className="hidden"
            onChange={(e) => addFiles(e.target.files)}
          />
        </label>
        <Button size="sm" onClick={send} disabled={sending} loading={sending}>
          <SendIcon className="mr-1.5 size-4" />
          {sending ? "Sending…" : "Send"}
        </Button>
      </div>
    </div>
  );
}
