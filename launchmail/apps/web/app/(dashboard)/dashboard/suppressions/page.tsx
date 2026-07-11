import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { SuppressionsManager } from "./suppressions-manager";

export const dynamic = "force-dynamic";

interface Row {
  id: string;
  email: string;
  reason: string;
  createdAt: string;
}

export default async function SuppressionsPage() {
  const rows = (await apiGet<Row[]>("/api/suppressions")) ?? [];
  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <PageHeader
        title="Suppressions"
        description="Addresses blocked from receiving email. Hard bounces are added automatically."
      />
      <SuppressionsManager rows={rows} />
    </div>
  );
}
