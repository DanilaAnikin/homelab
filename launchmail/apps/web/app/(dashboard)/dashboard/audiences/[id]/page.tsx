import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeftIcon } from "lucide-react";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { AudienceDetail } from "./audience-detail";

export const dynamic = "force-dynamic";

interface Contact {
  id: string;
  email: string;
  firstName: string | null;
  lastName: string | null;
  subscribed: boolean;
}
interface AudienceView {
  id: string;
  name: string;
  contacts: Contact[];
}

export default async function AudienceDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [audience, templates, configs] = await Promise.all([
    apiGet<AudienceView>(`/api/audiences/${id}`),
    apiGet<{ id: string; name: string }[]>("/api/templates").then((r) => r ?? []),
    apiGet<{ id: string; name: string; host: string }[]>(
      "/api/smtp-configs",
    ).then((r) => r ?? []),
  ]);
  if (!audience) notFound();

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <div className="space-y-3">
        <Link
          href="/dashboard/audiences"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-4" />
          Audiences
        </Link>
        <PageHeader
          title={audience.name}
          description={`${audience.contacts.length} contacts · ${audience.contacts.filter((c) => c.subscribed).length} subscribed`}
        />
      </div>
      <AudienceDetail
        audience={audience}
        templates={templates}
        configs={configs}
      />
    </div>
  );
}
