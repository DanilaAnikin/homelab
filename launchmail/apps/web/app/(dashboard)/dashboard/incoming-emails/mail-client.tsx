"use client";

import { useState, useTransition, useCallback } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Card, CardContent } from "@workspace/ui/components/card";
import { StatusBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/empty-state";
import {
  Sheet,
  SheetContent,
  SheetTitle,
} from "@workspace/ui/components/sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@workspace/ui/components/select";
import { toast } from "@workspace/ui/components/sonner";
import { cn } from "@workspace/ui/lib/utils";
import {
  InboxIcon,
  SendIcon,
  ServerIcon,
  StarIcon,
  PaperclipIcon,
  CornerUpLeftIcon,
  RefreshCwIcon,
  SearchIcon,
  ArchiveIcon,
  ClockIcon,
  AlertCircleIcon,
  HistoryIcon,
} from "lucide-react";
import {
  loadMessageAction,
  loadSentAction,
  markReadAction,
  starAction,
  syncMailboxAction,
  backfillAction,
  type IncomingEmailFull,
  type SentEmailFull,
} from "./actions";
import { MessageReader } from "./message-reader";
import { SentReader } from "./sent-reader";

export type MailTab = "incoming" | "sent";
export type Folder = "inbox" | "starred" | "archived";

export interface Mailbox {
  id: string;
  name: string;
  fromAddress: string;
  imapHost: string;
  unread: number;
  lastSyncAt: string | null;
  lastSyncError: string | null;
}

export interface MessageSummary {
  id: string;
  smtpConfigId: string;
  fromAddress: string;
  fromName: string | null;
  subject: string | null;
  snippet: string | null;
  seen: boolean;
  starred: boolean;
  archived: boolean;
  hasAttachments: boolean;
  repliedAt: string | null;
  receivedAt: string;
}

export interface SentSummary {
  id: string;
  smtpConfigId: string | null;
  to: { email: string; name?: string }[];
  subject: string;
  status: string;
  opens: number;
  clicks: number;
  createdAt: string;
}

const AVATAR_TONES = [
  "bg-brand-100 text-brand-700 dark:bg-brand-subtle dark:text-brand-emphasis",
  "bg-info-tint text-info-on",
  "bg-success-tint text-success-on",
  "bg-warning-tint text-warning-on",
  "bg-pending-tint text-pending-on",
];

function avatarTone(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_TONES[h % AVATAR_TONES.length]!;
}

function initial(name: string): string {
  const c = name.trim()[0];
  return c ? c.toUpperCase() : "?";
}

export function formatWhen(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay)
    return d.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  const sameYear = d.getFullYear() === now.getFullYear();
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

function relativeSync(iso: string | null): string {
  if (!iso) return "never";
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.round(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export function Avatar({ name, className }: { name: string; className?: string }) {
  return (
    <span
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-full text-body-strong",
        avatarTone(name),
        className,
      )}
      aria-hidden
    >
      {initial(name)}
    </span>
  );
}

export function MailClient({
  tab,
  mailboxes,
  selectedId,
  folder,
  q,
  incoming,
  sent,
}: {
  tab: MailTab;
  mailboxes: Mailbox[];
  selectedId: string;
  folder: Folder;
  q: string;
  incoming: MessageSummary[];
  sent: SentSummary[];
}) {
  const router = useRouter();
  const [pending, startNav] = useTransition();
  const [syncing, startSync] = useTransition();
  const [search, setSearch] = useState(q);

  // Reading pane.
  const [open, setOpen] = useState<{ type: MailTab; id: string } | null>(null);
  const [incomingDetail, setIncomingDetail] = useState<IncomingEmailFull | null>(
    null,
  );
  const [sentDetail, setSentDetail] = useState<SentEmailFull | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const current = mailboxes.find((m) => m.id === selectedId);

  // Build a URL preserving the rest of the params.
  const nav = useCallback(
    (patch: Record<string, string | undefined>) => {
      const params = new URLSearchParams();
      const base: Record<string, string | undefined> = {
        tab,
        mailbox: selectedId || undefined,
        folder: folder !== "inbox" ? folder : undefined,
        q: q || undefined,
        ...patch,
      };
      for (const [k, v] of Object.entries(base)) if (v) params.set(k, v);
      const qs = params.toString();
      startNav(() =>
        router.push(
          qs
            ? `/dashboard/incoming-emails?${qs}`
            : "/dashboard/incoming-emails",
        ),
      );
    },
    [router, tab, selectedId, folder, q],
  );

  function submitSearch(e: React.FormEvent) {
    e.preventDefault();
    nav({ q: search.trim() || undefined });
  }

  function sync() {
    if (!selectedId) return;
    startSync(async () => {
      const res = await syncMailboxAction(selectedId);
      if ("error" in res) return toastError(res.error);
      toast.success(res.fetched > 0 ? `${res.fetched} new` : "Up to date");
      router.refresh();
    });
  }

  function loadOlder() {
    if (!selectedId) return;
    startSync(async () => {
      const res = await backfillAction(selectedId);
      if ("error" in res) return toastError(res.error);
      toast.success(
        res.complete
          ? "All history loaded"
          : res.fetched > 0
            ? `Loaded ${res.fetched} older`
            : "No older messages",
      );
      router.refresh();
    });
  }

  async function openIncoming(m: MessageSummary) {
    setOpen({ type: "incoming", id: m.id });
    setIncomingDetail(null);
    setLoadingDetail(true);
    const full = await loadMessageAction(m.id);
    setIncomingDetail(full);
    setLoadingDetail(false);
    if (full && !m.seen) {
      await markReadAction(m.id, true);
      router.refresh();
    }
  }

  async function openSent(s: SentSummary) {
    setOpen({ type: "sent", id: s.id });
    setSentDetail(null);
    setLoadingDetail(true);
    setSentDetail(await loadSentAction(s.id));
    setLoadingDetail(false);
  }

  async function quickStar(m: MessageSummary, e: React.MouseEvent) {
    e.stopPropagation();
    const res = await starAction(m.id, !m.starred);
    if ("error" in res) return toastError(res.error);
    router.refresh();
  }

  return (
    <div className="space-y-4">
      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-border">
        <TabButton active={tab === "incoming"} onClick={() => nav({ tab: "incoming", q: undefined })} icon={InboxIcon}>
          Incoming emails
        </TabButton>
        <TabButton active={tab === "sent"} onClick={() => nav({ tab: "sent", q: undefined })} icon={SendIcon}>
          Sent emails
        </TabButton>
      </div>

      {tab === "incoming" ? (
        mailboxes.length === 0 ? (
          <EmptyState
            icon={InboxIcon}
            title="No receiving mailbox yet"
            description="Add IMAP (incoming mail) details to one of your SMTP servers to read received mail here."
            action={
              <Button asChild size="sm">
                <Link href="/dashboard/smtp">
                  <ServerIcon className="mr-1.5 size-4" />
                  Configure a server
                </Link>
              </Button>
            }
          />
        ) : (
          <>
            {/* Toolbar */}
            <div className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <Select
                  value={selectedId}
                  onValueChange={(id) => nav({ mailbox: id })}
                >
                  <SelectTrigger className="w-[min(360px,100%)]">
                    <SelectValue placeholder="Choose a mailbox" />
                  </SelectTrigger>
                  <SelectContent>
                    {mailboxes.map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        {m.name} · {m.fromAddress}
                        {m.unread > 0 ? `  (${m.unread})` : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                <div className="flex items-center gap-2">
                  <SyncStatus current={current} />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={sync}
                    disabled={syncing || pending}
                  >
                    <RefreshCwIcon
                      className={cn("mr-1.5 size-4", syncing && "animate-spin")}
                    />
                    Sync
                  </Button>
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-1.5">
                  {(["inbox", "starred", "archived"] as Folder[]).map((f) => (
                    <FolderPill
                      key={f}
                      active={folder === f}
                      onClick={() => nav({ folder: f === "inbox" ? undefined : f })}
                      icon={
                        f === "inbox"
                          ? InboxIcon
                          : f === "starred"
                            ? StarIcon
                            : ArchiveIcon
                      }
                    >
                      {f[0]!.toUpperCase() + f.slice(1)}
                    </FolderPill>
                  ))}
                </div>
                <form onSubmit={submitSearch} className="relative w-full sm:w-72">
                  <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Search mail…"
                    className="pl-8"
                  />
                </form>
              </div>
            </div>

            {/* List */}
            {incoming.length === 0 ? (
              <EmptyState
                icon={folder === "archived" ? ArchiveIcon : InboxIcon}
                title={q ? "No matches" : "Nothing here yet"}
                description={
                  q
                    ? `No messages match “${q}”.`
                    : folder === "inbox"
                      ? `New mail for ${current?.fromAddress} appears here automatically.`
                      : `No ${folder} messages.`
                }
              />
            ) : (
              <Card>
                <CardContent className="p-0">
                  <ul className="divide-y divide-border">
                    {incoming.map((m) => (
                      <MessageRow
                        key={m.id}
                        m={m}
                        onClick={() => openIncoming(m)}
                        onStar={(e) => quickStar(m, e)}
                      />
                    ))}
                  </ul>
                </CardContent>
              </Card>
            )}

            {incoming.length > 0 && folder === "inbox" && !q && (
              <div className="flex justify-center">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={loadOlder}
                  disabled={syncing}
                  className="text-muted-foreground"
                >
                  <HistoryIcon className="mr-1.5 size-4" />
                  Load older messages
                </Button>
              </div>
            )}
          </>
        )
      ) : (
        /* Sent tab */
        <>
          <form onSubmit={submitSearch} className="relative w-full sm:w-80">
            <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search sent mail…"
              className="pl-8"
            />
          </form>
          {sent.length === 0 ? (
            <EmptyState
              icon={SendIcon}
              title={q ? "No matches" : "No sent mail yet"}
              description={
                q
                  ? `No sent messages match “${q}”.`
                  : "Emails you send (and replies from the inbox) show up here with their full content."
              }
            />
          ) : (
            <Card>
              <CardContent className="p-0">
                <ul className="divide-y divide-border">
                  {sent.map((s) => (
                    <SentRow key={s.id} s={s} onClick={() => openSent(s)} />
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </>
      )}

      {/* Reading pane */}
      <Sheet
        open={!!open}
        onOpenChange={(o) => {
          if (!o) {
            setOpen(null);
            setIncomingDetail(null);
            setSentDetail(null);
          }
        }}
      >
        <SheetContent className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
          <SheetTitle className="sr-only">Message</SheetTitle>
          {loadingDetail ? (
            <div className="flex flex-1 items-center justify-center text-caption text-muted-foreground">
              Loading…
            </div>
          ) : open?.type === "incoming" && incomingDetail ? (
            <MessageReader
              detail={incomingDetail}
              onChanged={() => router.refresh()}
              onClose={() => setOpen(null)}
              onDetail={setIncomingDetail}
            />
          ) : open?.type === "sent" && sentDetail ? (
            <SentReader detail={sentDetail} />
          ) : (
            <div className="flex flex-1 items-center justify-center text-caption text-muted-foreground">
              Message not found.
            </div>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function toastError(e?: string) {
  toast.error(e ?? "Something went wrong");
}

function TabButton({
  active,
  onClick,
  icon: Icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: typeof InboxIcon;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "relative flex items-center gap-2 px-3 py-2.5 text-body-strong transition-colors",
        active ? "text-foreground" : "text-muted-foreground hover:text-foreground",
      )}
    >
      <Icon className="size-4" />
      {children}
      {active && (
        <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-brand-500" />
      )}
    </button>
  );
}

function FolderPill({
  active,
  onClick,
  icon: Icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: typeof InboxIcon;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-badge px-2.5 py-1 text-caption transition-colors",
        active
          ? "bg-brand-100 text-brand-800 dark:bg-brand-subtle dark:text-brand-emphasis"
          : "text-muted-foreground hover:bg-muted",
      )}
    >
      <Icon className="size-3.5" />
      {children}
    </button>
  );
}

function SyncStatus({ current }: { current?: Mailbox }) {
  if (!current) return null;
  if (current.lastSyncError) {
    return (
      <span
        className="flex items-center gap-1 text-caption text-destructive"
        title={current.lastSyncError}
      >
        <AlertCircleIcon className="size-3.5" />
        Sync error
      </span>
    );
  }
  return (
    <span className="hidden items-center gap-1 text-caption text-muted-foreground sm:flex">
      <ClockIcon className="size-3.5" />
      {relativeSync(current.lastSyncAt)}
    </span>
  );
}

function MessageRow({
  m,
  onClick,
  onStar,
}: {
  m: MessageSummary;
  onClick: () => void;
  onStar: (e: React.MouseEvent) => void;
}) {
  const who = m.fromName || m.fromAddress;
  return (
    <li>
      <div
        role="button"
        tabIndex={0}
        onClick={onClick}
        onKeyDown={(e) => e.key === "Enter" && onClick()}
        className={cn(
          "flex cursor-pointer items-start gap-3 px-3 py-3 transition-colors hover:bg-muted/50",
          !m.seen && "bg-brand-50/40 dark:bg-brand-subtle/20",
        )}
      >
        <button
          type="button"
          onClick={onStar}
          aria-label={m.starred ? "Unstar" : "Star"}
          className="mt-0.5 rounded p-1 text-muted-foreground transition-colors hover:text-warning"
        >
          <StarIcon
            className={cn(
              "size-4",
              m.starred && "fill-warning text-warning",
            )}
          />
        </button>
        <Avatar name={who} className="size-9" />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-3">
            <span
              className={cn(
                "truncate",
                m.seen ? "text-body text-foreground" : "text-body-strong",
              )}
            >
              {who}
            </span>
            <span className="shrink-0 text-caption tabular-nums text-muted-foreground">
              {formatWhen(m.receivedAt)}
            </span>
          </div>
          <p className={cn("truncate", m.seen ? "text-body" : "text-body-strong")}>
            {m.subject || "(no subject)"}
          </p>
          <p className="flex items-center gap-1.5 truncate text-caption text-muted-foreground">
            {m.repliedAt && (
              <CornerUpLeftIcon className="size-3 shrink-0 text-success" />
            )}
            {m.hasAttachments && <PaperclipIcon className="size-3 shrink-0" />}
            <span className="truncate">{m.snippet}</span>
          </p>
        </div>
        {!m.seen && (
          <span className="mt-2 size-2 shrink-0 rounded-full bg-brand-500" aria-hidden />
        )}
      </div>
    </li>
  );
}

function SentRow({ s, onClick }: { s: SentSummary; onClick: () => void }) {
  const to = s.to.map((r) => r.name || r.email).join(", ");
  return (
    <li>
      <div
        role="button"
        tabIndex={0}
        onClick={onClick}
        onKeyDown={(e) => e.key === "Enter" && onClick()}
        className="flex cursor-pointer items-start gap-3 px-3 py-3 transition-colors hover:bg-muted/50"
      >
        <Avatar name={to || "?"} className="size-9" />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-3">
            <span className="truncate text-body">
              <span className="text-muted-foreground">To </span>
              <span className="text-body-strong">{to || "—"}</span>
            </span>
            <span className="shrink-0 text-caption tabular-nums text-muted-foreground">
              {formatWhen(s.createdAt)}
            </span>
          </div>
          <p className="truncate text-body-strong">{s.subject || "(no subject)"}</p>
          <div className="mt-0.5">
            <StatusBadge status={s.status} />
          </div>
        </div>
      </div>
    </li>
  );
}
