import { apiGet } from "@/lib/api";
import Link from "next/link";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { VolumeChart, type VolumePoint } from "./volume-chart";
import {
  RecentActivityTable,
  type EmailLogRow,
} from "./recent-activity-table";
import { Card, CardContent } from "@workspace/ui/components/card";
import { Badge } from "@workspace/ui/components/badge";
import { Button } from "@workspace/ui/components/button";
import { Stat, StatGroup } from "@workspace/ui/components/stat";
import {
  ArrowRightIcon,
  PlusIcon,
  ServerIcon,
  SendIcon,
} from "lucide-react";

interface SmtpConfigRow {
  id: string;
  name: string;
  host: string;
  port: number;
  isDefault: boolean;
}
interface AnalyticsDay {
  date: string;
  sent: number;
  failed: number;
  opens: number;
  clicks: number;
}
interface Analytics {
  daily: AnalyticsDay[];
  totals: {
    sent: number;
    failed: number;
    opened: number;
    clicked: number;
    deliveryRate: number;
    openRate: number;
    clickRate: number;
  };
}

/** Signed percent-delta of the latest day vs the previous day. */
function signedDelta(series: number[]): string | undefined {
  if (series.length < 2) return undefined;
  const latest = series[series.length - 1] ?? 0;
  const prev = series[series.length - 2] ?? 0;
  if (prev === 0) return latest > 0 ? "+100%" : undefined;
  const pct = Math.round(((latest - prev) / prev) * 100);
  if (pct === 0) return undefined;
  return `${pct > 0 ? "+" : ""}${pct}%`;
}

export default async function DashboardPage() {
  const [configs, logs, analytics] = await Promise.all([
    apiGet<SmtpConfigRow[]>("/api/smtp-configs").then((r) => r ?? []),
    apiGet<EmailLogRow[]>("/api/logs?limit=200").then((r) => r ?? []),
    apiGet<Analytics>("/api/analytics?days=7"),
  ]);

  const total = logs.length;
  const sent = logs.filter((l) => l.status === "sent").length;
  const failed = logs.filter((l) => l.status === "failed").length;
  const rate = total ? Math.round((sent / total) * 100) : 0;
  const recentLogs = logs.slice(0, 6);

  const daily = analytics?.daily ?? [];
  const totals = analytics?.totals;
  const volume: VolumePoint[] = daily.map((d) => ({
    label: new Date(d.date).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    }),
    sent: d.sent,
    failed: d.failed,
    opens: d.opens,
    clicks: d.clicks,
  }));

  const sentSeries = daily.map((d) => d.sent);
  const failedSeries = daily.map((d) => d.failed);
  const openSeries = daily.map((d) => d.opens);

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <div className="-mx-6 -mt-6 rounded-b-xl bg-[radial-gradient(120%_140%_at_0%_0%,var(--brand-50),transparent_60%)] px-6 pt-6 pb-8 lg:-mx-8 lg:px-8 dark:bg-[radial-gradient(120%_140%_at_0%_0%,var(--brand-subtle),transparent_55%)]">
        <PageHeader
          title="Overview"
          description="Your sending at a glance · last 7 days"
        >
          <Button asChild size="sm">
            <Link href="/dashboard/smtp/new">
              <PlusIcon className="mr-2 size-4" />
              New SMTP config
            </Link>
          </Button>
        </PageHeader>
      </div>

      <StatGroup>
        <Stat
          accent
          label="Delivered"
          value={(totals?.sent ?? sent).toLocaleString()}
          delta={signedDelta(sentSeries)}
          sparkline={sentSeries}
        />
        <Stat
          label="Failed"
          value={(totals?.failed ?? failed).toLocaleString()}
          delta={signedDelta(failedSeries)}
          deltaDirection={
            (failedSeries[failedSeries.length - 1] ?? 0) >
            (failedSeries[failedSeries.length - 2] ?? 0)
              ? "down"
              : undefined
          }
          sparkline={failedSeries}
        />
        <Stat
          label="Delivery rate"
          value={`${totals?.deliveryRate ?? rate}%`}
        />
        <Stat
          label="Open rate"
          value={`${totals?.openRate ?? 0}%`}
          sparkline={openSeries}
        />
        <Stat label="SMTP servers" value={configs.length.toLocaleString()} />
      </StatGroup>

      <Card>
        <CardContent className="space-y-4 p-5">
          <div className="flex items-baseline justify-between gap-3">
            <div className="space-y-0.5">
              <p className="text-card-title">Sending volume</p>
              <p className="text-caption text-muted-foreground">
                Sent · failed · opens · clicks over the last 7 days
              </p>
            </div>
          </div>
          {volume.length === 0 ? (
            <EmptyState
              icon={SendIcon}
              title="No sending data yet"
              description="Once you start sending, daily volume will chart here."
            />
          ) : (
            <VolumeChart data={volume} />
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardContent className="p-0">
            <div className="flex items-center justify-between px-5 py-4">
              <div>
                <p className="text-card-title">Recent activity</p>
                <p className="text-caption text-muted-foreground">
                  Latest emails sent through your configs
                </p>
              </div>
              <Button variant="ghost" size="sm" asChild>
                <Link href="/dashboard/logs">
                  View all logs
                  <ArrowRightIcon className="ml-1 size-3" />
                </Link>
              </Button>
            </div>
            {recentLogs.length === 0 ? (
              <div className="p-4 pt-0">
                <EmptyState
                  icon={SendIcon}
                  title="No emails sent yet"
                  description="Add an SMTP server and send your first email to see it here."
                />
              </div>
            ) : (
              <RecentActivityTable logs={recentLogs} />
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardContent className="p-0">
            <div className="flex items-center justify-between px-5 py-4">
              <p className="text-card-title">SMTP servers</p>
              <Button variant="ghost" size="sm" asChild>
                <Link href="/dashboard/smtp">
                  Manage
                  <ArrowRightIcon className="ml-1 size-3" />
                </Link>
              </Button>
            </div>
            {configs.length === 0 ? (
              <div className="p-4 pt-0">
                <EmptyState
                  icon={ServerIcon}
                  title="No servers yet"
                  description="Connect an SMTP server to start sending."
                  action={
                    <Button size="sm" asChild>
                      <Link href="/dashboard/smtp/new">
                        <PlusIcon className="mr-1 size-3" />
                        Add server
                      </Link>
                    </Button>
                  }
                />
              </div>
            ) : (
              <div className="border-t">
                {configs.map((config) => (
                  <Link
                    key={config.id}
                    href={`/dashboard/smtp/${config.id}`}
                    className="flex items-center justify-between gap-3 border-b px-5 py-3 transition-colors last:border-b-0 hover:bg-muted/50"
                  >
                    <div className="flex min-w-0 items-center gap-3">
                      <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-brand-100 text-brand-700 dark:bg-brand-subtle dark:text-brand-emphasis">
                        <ServerIcon className="size-4" />
                      </div>
                      <div className="min-w-0">
                        <p className="flex items-center gap-2 truncate text-body-strong">
                          {config.name}
                          {config.isDefault && (
                            <Badge variant="brand" className="text-[10px]">
                              Default
                            </Badge>
                          )}
                        </p>
                        <p className="truncate font-mono text-caption text-muted-foreground">
                          {config.host}:{config.port}
                        </p>
                      </div>
                    </div>
                    <ArrowRightIcon className="size-4 shrink-0 text-muted-foreground" />
                  </Link>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
