import Link from "next/link";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { StatusBadge } from "@/components/status-badge";
import { Card, CardContent } from "@workspace/ui/components/card";
import { GlobeIcon, ChevronRightIcon } from "lucide-react";
import { AddDomain } from "./add-domain";

export const dynamic = "force-dynamic";

interface DomainRow {
  id: string;
  domain: string;
  verified: boolean;
}

export default async function DomainsPage() {
  const domains = (await apiGet<DomainRow[]>("/api/domains")) ?? [];

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <PageHeader
        title="Domains"
        description="Authenticate your sending domains with SPF, DKIM and DMARC for inbox placement."
      >
        <AddDomain />
      </PageHeader>

      {domains.length === 0 ? (
        <EmptyState
          icon={GlobeIcon}
          title="No domains yet"
          description="Add a domain to generate DKIM keys and the DNS records you need to authenticate it."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="border-t-0">
              {domains.map((d) => (
                <Link
                  key={d.id}
                  href={`/dashboard/domains/${d.id}`}
                  className="flex items-center justify-between gap-3 border-b px-5 py-4 transition-colors last:border-b-0 hover:bg-muted/50"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex size-9 items-center justify-center rounded-lg bg-brand/10 text-brand">
                      <GlobeIcon className="size-4" />
                    </div>
                    <p className="text-mono text-sm">{d.domain}</p>
                  </div>
                  <div className="flex items-center gap-3">
                    <StatusBadge status={d.verified ? "verified" : "pending"} />
                    <ChevronRightIcon className="size-4 text-muted-foreground" />
                  </div>
                </Link>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
