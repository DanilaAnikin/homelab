import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeftIcon } from "lucide-react";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { DomainDetail } from "./domain-detail";

export const dynamic = "force-dynamic";

interface DomainView {
  id: string;
  domain: string;
  verified: boolean;
  lastCheckedAt: string | null;
  records: {
    type: string;
    name: string;
    value: string;
    purpose: string;
    verified: boolean;
  }[];
}

export default async function DomainDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const domain = await apiGet<DomainView>(`/api/domains/${id}`);
  if (!domain) notFound();

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div className="space-y-3">
        <Link
          href="/dashboard/domains"
          className="inline-flex items-center gap-1 text-caption text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-3.5" />
          Domains
        </Link>
        <PageHeader
          title={domain.domain}
          description="DNS authentication records for this sending domain."
        />
      </div>
      <DomainDetail domain={domain} />
    </div>
  );
}
