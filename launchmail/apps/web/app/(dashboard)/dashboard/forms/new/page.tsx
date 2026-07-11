import Link from "next/link";
import { ArrowLeftIcon, MailWarningIcon } from "lucide-react";
import { apiGet } from "@/lib/api";
import { Button } from "@workspace/ui/components/button";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { NewFormClient } from "./new-form-client";
import type { EmailBlock } from "@workspace/templates";

export const dynamic = "force-dynamic";

interface ConfigRow {
  id: string;
  name: string;
  host: string;
  port: number;
}
interface TemplateRow {
  id: string;
  name: string;
  mode: "html" | "blocks";
  html: string | null;
  blocks: EmailBlock[] | null;
  accent: string | null;
  theme: "light" | "dark" | null;
}

export default async function NewFormPage() {
  const [configs, templates] = await Promise.all([
    apiGet<ConfigRow[]>("/api/smtp-configs").then((r) => r ?? []),
    apiGet<TemplateRow[]>("/api/templates").then((r) => r ?? []),
  ]);

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      <div className="space-y-3">
        <Link
          href="/dashboard/forms"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-4" />
          Forms
        </Link>
        <PageHeader
          title="New form"
          description="Pick a template, choose how submissions are delivered, and get an endpoint."
        />
      </div>

      {configs.length === 0 ? (
        <EmptyState
          icon={MailWarningIcon}
          title="No SMTP connection yet"
          description="A form needs an SMTP connection to send submissions. Add one first."
          action={
            <Button asChild size="sm">
              <Link href="/dashboard/smtp/new">Add SMTP connection</Link>
            </Button>
          }
        />
      ) : (
        <NewFormClient
          configs={configs.map((c) => ({
            id: c.id,
            name: c.name,
            host: c.host,
            port: c.port,
          }))}
          customTemplates={templates}
        />
      )}
    </div>
  );
}
