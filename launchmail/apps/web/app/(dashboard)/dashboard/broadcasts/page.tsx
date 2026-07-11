import Link from "next/link";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@workspace/ui/components/button";
import { Card, CardContent } from "@workspace/ui/components/card";
import { BroadcastsTable, type BroadcastRow } from "./broadcasts-table";
import { SendIcon, UsersIcon } from "lucide-react";

export const dynamic = "force-dynamic";

export default async function BroadcastsPage() {
  const broadcasts = (await apiGet<BroadcastRow[]>("/api/broadcasts")) ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <PageHeader
        title="Broadcasts"
        description="One-off emails sent to an audience. Start one from an audience."
      >
        <Button asChild size="sm" variant="outline">
          <Link href="/dashboard/audiences">
            <UsersIcon className="mr-2 size-4" />
            Audiences
          </Link>
        </Button>
      </PageHeader>

      {broadcasts.length === 0 ? (
        <EmptyState
          icon={SendIcon}
          title="No broadcasts yet"
          description="Open an audience and send a template to its contacts — it'll show up here."
          action={
            <Button asChild size="sm">
              <Link href="/dashboard/audiences">Go to audiences</Link>
            </Button>
          }
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <BroadcastsTable broadcasts={broadcasts} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
