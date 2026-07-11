import Link from "next/link";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { Card, CardContent } from "@workspace/ui/components/card";
import { UsersIcon, ChevronRightIcon } from "lucide-react";
import { CreateAudience } from "./create-audience";

export const dynamic = "force-dynamic";

interface AudienceRow {
  id: string;
  name: string;
  contactCount: number;
}

export default async function AudiencesPage() {
  const audiences = (await apiGet<AudienceRow[]>("/api/audiences")) ?? [];

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <PageHeader
        title="Audiences"
        description="Manage contact lists and send broadcasts to them."
      >
        <CreateAudience />
      </PageHeader>

      {audiences.length === 0 ? (
        <EmptyState
          icon={UsersIcon}
          title="No audiences yet"
          description="Create an audience, add contacts, and send a template to the whole list."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            {audiences.map((a) => (
              <Link
                key={a.id}
                href={`/dashboard/audiences/${a.id}`}
                className="flex items-center justify-between gap-3 border-b px-5 py-4 transition-colors last:border-b-0 hover:bg-muted/50"
              >
                <div className="flex items-center gap-3">
                  <div className="flex size-9 items-center justify-center rounded-lg bg-muted">
                    <UsersIcon className="size-4 text-muted-foreground" />
                  </div>
                  <div>
                    <p className="text-sm font-medium">{a.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {a.contactCount} contact{a.contactCount === 1 ? "" : "s"}
                    </p>
                  </div>
                </div>
                <ChevronRightIcon className="size-4 text-muted-foreground" />
              </Link>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
