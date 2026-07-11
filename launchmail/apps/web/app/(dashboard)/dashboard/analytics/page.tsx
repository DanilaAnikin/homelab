import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { AreaChart, type AreaChartPoint } from "@/components/area-chart";
import { RetryFailed } from "./retry-failed";
import { Card, CardContent, CardHeader, CardTitle } from "@workspace/ui/components/card";
import { Stat, StatGroup } from "@workspace/ui/components/stat";

export const dynamic = "force-dynamic";

interface Analytics {
  daily: { date: string; sent: number; failed: number; opens: number; clicks: number }[];
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
interface FailedJob {
  id: string;
  to: { email: string }[];
  subject: string;
  failedReason: string;
  attemptsMade: number;
}

const SERIES = [
  { key: "sent", name: "Sent", color: "var(--chart-1)" },
  { key: "opens", name: "Opens", color: "var(--chart-2)" },
  { key: "clicks", name: "Clicks", color: "var(--chart-3)" },
  { key: "failed", name: "Failed", color: "var(--chart-5)" },
];

export default async function AnalyticsPage() {
  const [analytics, failed] = await Promise.all([
    apiGet<Analytics>("/api/analytics?days=30"),
    apiGet<FailedJob[]>("/api/failed").then((r) => r ?? []),
  ]);

  const t = analytics?.totals ?? {
    sent: 0,
    failed: 0,
    opened: 0,
    clicked: 0,
    deliveryRate: 0,
    openRate: 0,
    clickRate: 0,
  };
  const daily = analytics?.daily ?? [];
  const chart: AreaChartPoint[] = daily.map((d) => ({
    label: new Date(d.date).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    }),
    sent: d.sent,
    opens: d.opens,
    clicks: d.clicks,
    failed: d.failed,
  }));

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <PageHeader
        title="Analytics"
        description="Sending performance over the last 30 days."
      />

      <StatGroup>
        <Stat
          accent
          label="Sent"
          value={t.sent.toLocaleString()}
          sparkline={daily.map((d) => d.sent)}
        />
        <Stat
          label="Delivery rate"
          value={`${t.deliveryRate}%`}
          delta={`${t.failed} failed`}
          deltaDirection="neutral"
        />
        <Stat
          label="Open rate"
          value={`${t.openRate}%`}
          delta={`${t.opened} opened`}
          deltaDirection="neutral"
          sparkline={daily.map((d) => d.opens)}
        />
        <Stat
          label="Click rate"
          value={`${t.clickRate}%`}
          delta={`${t.clicked} clicked`}
          deltaDirection="neutral"
          sparkline={daily.map((d) => d.clicks)}
        />
        <Stat
          label="Failed"
          value={t.failed.toLocaleString()}
          deltaDirection="neutral"
          sparkline={daily.map((d) => d.failed)}
        />
      </StatGroup>

      <Card>
        <CardHeader>
          <CardTitle>Sending volume</CardTitle>
        </CardHeader>
        <CardContent>
          <AreaChart data={chart} series={SERIES} />
        </CardContent>
      </Card>

      <RetryFailed jobs={failed} />
    </div>
  );
}
