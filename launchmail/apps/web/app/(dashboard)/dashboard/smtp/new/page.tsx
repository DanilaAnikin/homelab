import Link from "next/link";
import { apiGet } from "@/lib/api";
import {
  Card,
  CardContent,
} from "@workspace/ui/components/card";
import { ArrowLeftIcon, ServerIcon } from "lucide-react";
import { NewSmtpForm } from "./new-smtp-form";

export const dynamic = "force-dynamic";

interface DomainRow {
  domain: string;
  dkimVerified: boolean;
}

export default async function NewSmtpConfigPage() {
  const domains = (await apiGet<DomainRow[]>("/api/domains")) ?? [];
  const verifiedDomains = domains
    .filter((d) => d.dkimVerified)
    .map((d) => d.domain.toLowerCase());

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="space-y-3">
        <Link
          href="/dashboard/smtp"
          className="inline-flex items-center gap-1 text-caption text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-3.5" />
          SMTP servers
        </Link>
        <div className="flex items-center gap-3">
          <div className="flex size-12 items-center justify-center rounded-xl bg-brand/10 text-brand">
            <ServerIcon className="size-6" />
          </div>
          <div>
            <h1 className="text-display text-foreground">Add SMTP server</h1>
            <p className="text-caption text-muted-foreground">
              Configure a new outgoing mail server
            </p>
          </div>
        </div>
      </div>

      <Card>
        <CardContent className="py-2">
          <NewSmtpForm verifiedDomains={verifiedDomains} />
        </CardContent>
      </Card>
    </div>
  );
}
