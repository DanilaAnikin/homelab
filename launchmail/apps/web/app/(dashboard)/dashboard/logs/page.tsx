import { apiGet } from "@/lib/api";
import Link from "next/link";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardContent } from "@workspace/ui/components/card";
import { Button } from "@workspace/ui/components/button";
import { Stat, StatGroup } from "@workspace/ui/components/stat";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@workspace/ui/components/table";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@workspace/ui/components/sheet";
import { cn } from "@workspace/ui/lib/utils";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  MailIcon,
  ServerIcon,
} from "lucide-react";

export const dynamic = "force-dynamic";

interface LogRow {
  id: string;
  status: string;
  subject: string;
  to: { email: string; name?: string }[];
  from?: string;
  smtpConfigId: string | null;
  providerMessageId?: string | null;
  html?: string | null;
  text?: string | null;
  opens?: number;
  clicks?: number;
  dkimSigned?: boolean;
  createdAt: string;
  error?: string | null;
}

// Status tone driving the 2px left spine + faint row-wash on the DataTable.
type RowStatus = "destructive" | "warning" | "success" | "info" | "pending";

function logRowStatus(status: string): RowStatus | undefined {
  switch (status.toLowerCase()) {
    case "failed":
    case "bounced":
    case "bounce":
    case "complaint":
    case "error":
      return "destructive";
    case "deferred":
    case "suppressed":
      return "warning";
    case "queued":
    case "processing":
      return "info";
    // "sent" / "delivered" stay un-washed so failures pop.
    default:
      return undefined;
  }
}

// The status spine + faint row-wash recipe shared with the cockpit DataTable.
const statusSpine: Record<RowStatus, string> = {
  destructive: "before:bg-destructive",
  warning: "before:bg-warning",
  success: "before:bg-success",
  info: "before:bg-info",
  pending: "before:bg-pending",
};
const statusWash: Record<RowStatus, string> = {
  destructive: "bg-destructive-tint/40 hover:bg-destructive-tint/60",
  warning: "bg-warning-tint/40 hover:bg-warning-tint/60",
  success: "bg-success-tint/40 hover:bg-success-tint/60",
  info: "bg-info-tint/40 hover:bg-info-tint/60",
  pending: "bg-pending-tint/60 hover:bg-pending-tint/80",
};

const PAGE_SIZE = 25;

// Filter chips. `value` is matched (case-insensitively) against log.status.
const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "All" },
  { value: "sent", label: "Sent" },
  { value: "failed", label: "Failed" },
  { value: "suppressed", label: "Suppressed" },
];

function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  if (id.length <= 14) return id;
  return `${id.slice(0, 8)}…${id.slice(-4)}`;
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

// Quote a CSV field per RFC 4180 (escape embedded quotes, wrap if needed).
function csvField(value: unknown): string {
  const s = value == null ? "" : String(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// Build a self-contained CSV download href from the fetched window. No backend
// export endpoint exists, so we render a data: URI — fully server-side, no
// client JS, and the data never leaves what the page already loaded.
function buildCsvHref(rows: LogRow[]): string {
  const header = [
    "status",
    "to",
    "subject",
    "message_id",
    "opens",
    "clicks",
    "error",
    "created_at",
  ];
  const lines = rows.map((log) =>
    [
      log.status,
      log.to.map((r) => r.email).join("; "),
      log.subject,
      log.providerMessageId ?? "",
      log.opens ?? 0,
      log.clicks ?? 0,
      log.error ?? "",
      log.createdAt,
    ]
      .map(csvField)
      .join(","),
  );
  const csv = [header.join(","), ...lines].join("\r\n");
  return `data:text/csv;charset=utf-8,${encodeURIComponent(csv)}`;
}

// Build an href that toggles one search param while preserving the rest.
function buildHref(
  base: Record<string, string | undefined>,
  patch: Record<string, string | undefined>,
): string {
  const params = new URLSearchParams();
  const merged = { ...base, ...patch };
  for (const [key, val] of Object.entries(merged)) {
    if (val && val !== "all" && !(key === "page" && val === "1")) {
      params.set(key, val);
    }
  }
  const qs = params.toString();
  return qs ? `/dashboard/logs?${qs}` : "/dashboard/logs";
}

export default async function EmailLogsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const statusFilter =
    typeof sp.status === "string" ? sp.status.toLowerCase() : "all";
  const pageParam = typeof sp.page === "string" ? Number(sp.page) : 1;

  const [configs, logs] = await Promise.all([
    apiGet<{ id: string; name: string }[]>("/api/smtp-configs").then(
      (r) => r ?? [],
    ),
    apiGet<LogRow[]>("/api/logs?limit=100").then((r) => r ?? []),
  ]);

  const configMap = new Map(configs.map((c) => [c.id, c]));

  // Counts are over the full fetched window, independent of the active filter.
  const sentCount = logs.filter((l) => l.status === "sent").length;
  const failedCount = logs.filter((l) => l.status === "failed").length;
  const deliveryRate =
    logs.length > 0 ? Math.round((sentCount / logs.length) * 100) : 0;

  // Apply the status chip, then paginate — purely over the fetched window,
  // since the API exposes only `limit` (no server-side filter/offset).
  const filtered =
    statusFilter === "all"
      ? logs
      : logs.filter((l) => l.status.toLowerCase() === statusFilter);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(Math.max(1, pageParam || 1), totalPages);
  const start = (currentPage - 1) * PAGE_SIZE;
  const pageRows = filtered.slice(start, start + PAGE_SIZE);

  const baseParams = {
    status: statusFilter,
    page: String(currentPage),
  };

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <PageHeader
        title="Email logs"
        description="Every message your SMTP transport has handled"
      >
        {logs.length > 0 && (
          <Button asChild size="sm" variant="outline">
            <a href={buildCsvHref(logs)} download="email-logs.csv">
              <DownloadIcon className="mr-1.5 size-3.5" />
              Export CSV
            </a>
          </Button>
        )}
      </PageHeader>

      <StatGroup className="sm:grid-cols-3 lg:grid-cols-3">
        <Stat label="Total" value={logs.length.toLocaleString()} />
        <Stat
          label="Sent"
          value={sentCount.toLocaleString()}
          delta={logs.length > 0 ? `${deliveryRate}%` : undefined}
          deltaDirection="neutral"
          accent
        />
        <Stat
          label="Failed"
          value={failedCount.toLocaleString()}
          deltaDirection={failedCount > 0 ? "down" : "neutral"}
        />
      </StatGroup>

      {logs.length === 0 ? (
        <EmptyState
          icon={MailIcon}
          title="No emails sent yet"
          description="Connect an SMTP server and send your first email to see delivery logs here."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            {/* Filter chips — active chip carries the brand tint. */}
            <div className="flex flex-wrap items-center gap-1.5 px-3 py-3">
              {STATUS_FILTERS.map((chip) => {
                const active = statusFilter === chip.value;
                return (
                  <Button
                    key={chip.value}
                    asChild
                    size="xs"
                    variant="ghost"
                    className={cn(
                      "rounded-badge",
                      active
                        ? "bg-brand-100 text-brand-800 hover:bg-brand-100 hover:text-brand-800"
                        : "text-muted-foreground",
                    )}
                  >
                    <Link
                      href={buildHref(baseParams, {
                        status: chip.value,
                        page: "1",
                      })}
                      aria-current={active ? "true" : undefined}
                    >
                      {chip.label}
                    </Link>
                  </Button>
                );
              })}
            </div>

            <Table className="text-body">
              <TableHeader>
                <TableRow className="border-b-0 hover:bg-transparent">
                  <TableHead className="h-auto border-b border-border-strong px-3 py-2 text-eyebrow">
                    Status
                  </TableHead>
                  <TableHead className="h-auto border-b border-border-strong px-3 py-2 text-eyebrow">
                    Recipient
                  </TableHead>
                  <TableHead className="h-auto border-b border-border-strong px-3 py-2 text-eyebrow">
                    Subject
                  </TableHead>
                  <TableHead className="h-auto border-b border-border-strong px-3 py-2 text-right text-eyebrow">
                    Message-ID
                  </TableHead>
                  <TableHead className="h-auto border-b border-border-strong px-3 py-2 text-right text-eyebrow">
                    Opens
                  </TableHead>
                  <TableHead className="h-auto border-b border-border-strong px-3 py-2 text-right text-eyebrow">
                    Sent
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pageRows.length === 0 ? (
                  <TableRow className="hover:bg-transparent">
                    <TableCell
                      colSpan={6}
                      className="px-3 py-10 text-center text-caption text-muted-foreground"
                    >
                      No logs match this filter.
                    </TableCell>
                  </TableRow>
                ) : (
                  pageRows.map((log) => {
                    const config = log.smtpConfigId
                      ? configMap.get(log.smtpConfigId)
                      : null;
                    const status = logRowStatus(log.status);
                    const recipients = log.to.map((r) => r.email).join(", ");

                    return (
                      // Each row is its own uncontrolled Sheet trigger — clicking
                      // opens the detail drawer. Radix owns the open state, so this
                      // stays renderable from the server component.
                      <Sheet key={log.id}>
                        <SheetTrigger asChild>
                          <TableRow
                            data-status={status}
                            className={cn(
                              "relative cursor-pointer hover:bg-muted",
                              status &&
                                "before:absolute before:inset-y-0 before:left-0 before:w-0.5",
                              status && statusSpine[status],
                              status && statusWash[status],
                            )}
                          >
                            <TableCell className="px-3 py-3 align-middle">
                              <StatusBadge status={log.status} />
                            </TableCell>
                            <TableCell className="max-w-[180px] truncate px-3 py-3 align-middle font-mono text-mono">
                              {recipients}
                            </TableCell>
                            <TableCell className="max-w-[220px] px-3 py-3 align-middle">
                              <span className="block truncate text-body-strong">
                                {log.subject || "(no subject)"}
                              </span>
                            </TableCell>
                            <TableCell className="px-3 py-3 text-right align-middle font-mono tabular-nums text-mono text-muted-foreground">
                              {shortId(log.providerMessageId)}
                            </TableCell>
                            <TableCell className="px-3 py-3 text-right align-middle font-mono tabular-nums text-muted-foreground">
                              {log.opens ?? 0}
                            </TableCell>
                            <TableCell className="px-3 py-3 text-right align-middle font-mono tabular-nums text-muted-foreground">
                              {formatTimestamp(log.createdAt)}
                            </TableCell>
                          </TableRow>
                        </SheetTrigger>
                        <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-md">
                          <SheetHeader className="gap-2 border-b border-border">
                            <div className="flex items-center gap-2">
                              <StatusBadge status={log.status} />
                              {config && (
                                <span className="inline-flex items-center gap-1 text-caption text-muted-foreground">
                                  <ServerIcon className="size-3" />
                                  {config.name}
                                </span>
                              )}
                            </div>
                            <SheetTitle className="text-card-title">
                              {log.subject || "(no subject)"}
                            </SheetTitle>
                            <SheetDescription className="text-caption">
                              {formatTimestamp(log.createdAt)}
                            </SheetDescription>
                          </SheetHeader>

                          <div className="flex flex-col gap-5 p-4">
                            <DetailField label="To" mono>
                              {recipients || "—"}
                            </DetailField>
                            {log.from && (
                              <DetailField label="From" mono>
                                {log.from}
                              </DetailField>
                            )}
                            <DetailField label="Message-ID" mono>
                              {log.providerMessageId || "—"}
                            </DetailField>
                            <div className="grid grid-cols-2 gap-4">
                              <DetailField label="Opens" mono>
                                {log.opens ?? 0}
                              </DetailField>
                              <DetailField label="Clicks" mono>
                                {log.clicks ?? 0}
                              </DetailField>
                            </div>
                            <DetailField label="DKIM signed">
                              {log.dkimSigned ? "Yes" : "No"}
                            </DetailField>
                            {(log.html || log.text) && (
                              <div className="space-y-1">
                                <p className="text-eyebrow text-muted-foreground">
                                  Message
                                </p>
                                {log.html ? (
                                  // Sandboxed (no scripts, opaque origin) so the
                                  // stored HTML body can't execute in the app.
                                  <iframe
                                    title="Sent message"
                                    sandbox=""
                                    srcDoc={log.html}
                                    className="h-80 w-full rounded-md border border-border bg-white"
                                  />
                                ) : (
                                  <pre className="overflow-x-auto rounded-md border border-border bg-muted/30 p-3 font-sans text-body whitespace-pre-wrap break-words">
                                    {log.text}
                                  </pre>
                                )}
                              </div>
                            )}
                            {config ? (
                              <DetailField label="SMTP connection">
                                <Link
                                  href={`/dashboard/smtp/${config.id}`}
                                  className="inline-flex items-center gap-1 text-brand hover:underline"
                                >
                                  <ServerIcon className="size-3.5" />
                                  {config.name}
                                </Link>
                              </DetailField>
                            ) : null}
                            {log.error && (
                              <DetailField label="Error">
                                <pre className="overflow-x-auto rounded-md border border-destructive/30 bg-destructive-tint/40 p-3 font-mono text-mono whitespace-pre-wrap text-destructive-on">
                                  {log.error}
                                </pre>
                              </DetailField>
                            )}
                          </div>

                          <div className="mt-auto flex justify-end gap-2 border-t border-border p-4">
                            <SheetClose asChild>
                              <Button size="sm" variant="outline">
                                Close
                              </Button>
                            </SheetClose>
                          </div>
                        </SheetContent>
                      </Sheet>
                    );
                  })
                )}
              </TableBody>
            </Table>

            {/* Pagination — mono counts, prev/next links preserve the filter. */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-3 py-3">
              <p className="text-caption tabular-nums text-muted-foreground">
                {filtered.length === 0
                  ? "No rows"
                  : `Rows ${start + 1}–${Math.min(
                      start + PAGE_SIZE,
                      filtered.length,
                    )} of ${filtered.length.toLocaleString()}`}
              </p>
              <div className="flex items-center gap-1.5">
                <Button
                  asChild={currentPage > 1}
                  size="sm"
                  variant="outline"
                  disabled={currentPage <= 1}
                  className="gap-1"
                >
                  {currentPage > 1 ? (
                    <Link
                      href={buildHref(baseParams, {
                        page: String(currentPage - 1),
                      })}
                    >
                      <ChevronLeftIcon className="size-3.5" />
                      Prev
                    </Link>
                  ) : (
                    <span>
                      <ChevronLeftIcon className="size-3.5" />
                      Prev
                    </span>
                  )}
                </Button>
                <span className="px-1 text-caption tabular-nums text-muted-foreground">
                  Page {currentPage} of {totalPages}
                </span>
                <Button
                  asChild={currentPage < totalPages}
                  size="sm"
                  variant="outline"
                  disabled={currentPage >= totalPages}
                  className="gap-1"
                >
                  {currentPage < totalPages ? (
                    <Link
                      href={buildHref(baseParams, {
                        page: String(currentPage + 1),
                      })}
                    >
                      Next
                      <ChevronRightIcon className="size-3.5" />
                    </Link>
                  ) : (
                    <span>
                      Next
                      <ChevronRightIcon className="size-3.5" />
                    </span>
                  )}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function DetailField({
  label,
  mono,
  children,
}: {
  label: string;
  mono?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <p className="text-eyebrow text-muted-foreground">{label}</p>
      <div
        className={cn(
          "break-words text-body",
          mono && "font-mono tabular-nums text-mono",
        )}
      >
        {children}
      </div>
    </div>
  );
}
