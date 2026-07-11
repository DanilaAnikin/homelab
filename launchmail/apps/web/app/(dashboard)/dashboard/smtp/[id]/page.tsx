import { apiGet } from "@/lib/api";
import type { SmtpConfigDetail } from "@/lib/types";
import { notFound } from "next/navigation";
import Link from "next/link";
import { EditableConfig } from "./editable-config";
import { TestConnection } from "./test-connection";
import { TestSend } from "./test-send";
import { ApiTokenManager } from "./api-token-manager";
import { ConfigLogs } from "./config-logs";
import { Card, CardContent } from "@workspace/ui/components/card";
import { Badge } from "@workspace/ui/components/badge";
import { cn } from "@workspace/ui/lib/utils";
import { ArrowLeftIcon, ServerIcon, MailIcon, KeyIcon } from "lucide-react";

export default async function SmtpConfigDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const config = await apiGet<SmtpConfigDetail>(`/api/smtp-configs/${id}`);
  if (!config) notFound();

  return (
    <div className="mx-auto max-w-3xl space-y-10">
      <div className="space-y-3">
        <Link
          href="/dashboard/smtp"
          className="inline-flex items-center gap-1 text-caption text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-3.5" />
          SMTP servers
        </Link>
        <div className="flex items-center gap-4">
          <div className="flex size-12 items-center justify-center rounded-xl bg-brand/10 text-brand">
            <ServerIcon className="size-6" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-display text-foreground">{config.name}</h1>
              {config.isDefault && <Badge variant="brand">Default</Badge>}
            </div>
            <p className="text-mono text-sm text-muted-foreground">
              {config.host}:{config.port}
            </p>
          </div>
        </div>
      </div>

      <Section eyebrow="Overview" title="Sender identity">
        <div className="grid gap-4 sm:grid-cols-2">
          <InfoCard
            icon={MailIcon}
            label="From address"
            value={
              config.fromName
                ? `${config.fromName} <${config.fromAddress}>`
                : config.fromAddress
            }
          />
          <InfoCard
            icon={KeyIcon}
            label="Username"
            value={config.username}
            mono
          />
        </div>
      </Section>

      <Section eyebrow="Settings" title="Configuration">
        <EditableConfig config={config} />
      </Section>

      <Section eyebrow="Testing" title="Verify delivery">
        <div className="grid gap-4">
          <TestConnection configId={config.id} />
          <TestSend configId={config.id} />
        </div>
      </Section>

      <Section eyebrow="API keys" title="API tokens">
        <ApiTokenManager configId={config.id} />
      </Section>

      <Section eyebrow="Logs" title="Recent activity">
        <ConfigLogs configId={config.id} />
      </Section>
    </div>
  );
}

function Section({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-4">
      <div className="space-y-1">
        <p className="text-eyebrow">{eyebrow}</p>
        <h2 className="text-section text-foreground">{title}</h2>
      </div>
      {children}
    </section>
  );
}

function InfoCard({
  icon: Icon,
  label,
  value,
  mono = false,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div className="flex size-9 items-center justify-center rounded-lg bg-muted">
          <Icon className="size-4 text-muted-foreground" />
        </div>
        <div className="min-w-0">
          <p className="text-eyebrow">{label}</p>
          <p
            className={cn(
              "truncate text-body-strong",
              mono && "text-mono text-sm"
            )}
          >
            {value}
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
