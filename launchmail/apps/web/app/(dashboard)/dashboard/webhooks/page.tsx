import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { WebhooksManager } from "./webhooks-manager";
import type { WebhookEvent as Event } from "@workspace/db/schemas";

export const dynamic = "force-dynamic";

interface Hook {
  id: string;
  url: string;
  events: Event[];
  secret: string;
  enabled: boolean;
  lastStatus: string | null;
}

export default async function WebhooksPage() {
  const hooks = (await apiGet<Hook[]>("/api/webhooks")) ?? [];
  const registry =
    (await apiGet<{ events: Event[] }>("/api/webhooks/events"))?.events ?? [];
  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <PageHeader
        title="Webhooks"
        description="Receive signed event notifications at your own endpoints."
      />
      <WebhooksManager hooks={hooks} availableEvents={registry} />
    </div>
  );
}
