import { apiGet } from "@/lib/api";
import Link from "next/link";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@workspace/ui/components/button";
import {
  Card,
  CardContent,
} from "@workspace/ui/components/card";
import { Badge } from "@workspace/ui/components/badge";
import { Separator } from "@workspace/ui/components/separator";
import { cn } from "@workspace/ui/lib/utils";
import {
  PlusIcon,
  ServerIcon,
  ArrowRightIcon,
  CheckCircle2Icon,
  XCircleIcon,
  MailIcon,
} from "lucide-react";

interface ConfigRow {
  id: string;
  name: string;
  host: string;
  port: number;
  fromAddress: string;
  isDefault: boolean;
}

export default async function SmtpConfigsPage() {
  const configs = (await apiGet<ConfigRow[]>("/api/smtp-configs")) ?? [];

  const configsWithStats = await Promise.all(
    configs.map(async (config) => {
      const logs =
        (await apiGet<{ status: string }[]>(
          `/api/smtp-configs/${config.id}/logs`,
        )) ?? [];
      const sent = logs.filter((l) => l.status === "sent").length;
      const failed = logs.filter((l) => l.status === "failed").length;
      return { ...config, sent, failed, totalLogs: logs.length };
    })
  );

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <PageHeader
        title="SMTP servers"
        description="Manage the outgoing mail servers your emails are sent through."
      >
        <Button asChild size="sm">
          <Link href="/dashboard/smtp/new">
            <PlusIcon className="mr-2 size-4" />
            Add server
          </Link>
        </Button>
      </PageHeader>

      {configsWithStats.length === 0 ? (
        <EmptyState
          icon={ServerIcon}
          title="No SMTP servers yet"
          description="Add your first SMTP server to start sending emails."
          action={
            <Button asChild size="sm">
              <Link href="/dashboard/smtp/new">
                <PlusIcon className="mr-2 size-4" />
                Add server
              </Link>
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {configsWithStats.map((config) => (
            <ConfigCard key={config.id} config={config} />
          ))}
        </div>
      )}
    </div>
  );
}

async function ConfigCard({
  config,
}: {
  config: {
    id: string;
    name: string;
    host: string;
    port: number;
    fromAddress: string;
    fromName?: string | null;
    isDefault: boolean;
    sent: number;
    failed: number;
    totalLogs: number;
  };
}) {
  const successRate =
    config.totalLogs > 0
      ? Math.round((config.sent / config.totalLogs) * 100)
      : null;

  return (
    <Link href={`/dashboard/smtp/${config.id}`} className="group">
      <Card className="relative overflow-hidden transition-all hover:border-brand/40">
        <CardContent className="p-5">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-3">
              <div className="flex size-10 items-center justify-center rounded-lg bg-brand/10 text-brand transition-colors group-hover:bg-brand/15">
                <ServerIcon className="size-5" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <p className="text-card-title">{config.name}</p>
                  {config.isDefault && (
                    <Badge variant="brand">Default</Badge>
                  )}
                </div>
                <p className="text-mono text-xs text-muted-foreground">
                  {config.host}:{config.port}
                </p>
              </div>
            </div>
            <ArrowRightIcon className="size-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
          </div>

          <Separator className="my-4" />

          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4 text-caption">
              <div className="flex items-center gap-1.5">
                <CheckCircle2Icon className="size-3.5 text-success" />
                <span className="text-muted-foreground">
                  <span className="font-medium text-foreground tabular-nums">{config.sent}</span> sent
                </span>
              </div>
              {config.failed > 0 && (
                <div className="flex items-center gap-1.5">
                  <XCircleIcon className="size-3.5 text-destructive" />
                  <span className="text-muted-foreground">
                    <span className="font-medium text-foreground tabular-nums">{config.failed}</span> failed
                  </span>
                </div>
              )}
            </div>
            {successRate !== null && (
              <div
                className={cn(
                  "text-caption font-medium tabular-nums",
                  successRate >= 90
                    ? "text-success"
                    : successRate >= 70
                    ? "text-warning"
                    : "text-destructive"
                )}
              >
                {successRate}% success
              </div>
            )}
          </div>

          <div className="mt-3 flex items-center gap-2 text-caption text-muted-foreground">
            <MailIcon className="size-3" />
            <span className="truncate">
              {config.fromName
                ? `${config.fromName} <${config.fromAddress}>`
                : config.fromAddress}
            </span>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
