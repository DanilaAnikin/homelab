import Link from "next/link";
import { apiGet } from "@/lib/api";
import { getDesign } from "@workspace/templates";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@workspace/ui/components/button";
import { Card, CardContent } from "@workspace/ui/components/card";
import { PlusIcon, FileTextIcon } from "lucide-react";

export const dynamic = "force-dynamic";

interface FormRow {
  id: string;
  name: string;
  enabled: boolean;
  templateKey: string;
  endpointToken: string;
}

export default async function FormsPage() {
  const forms = (await apiGet<FormRow[]>("/api/forms")) ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <PageHeader
        title="Forms"
        description="Formspree-style endpoints — point any HTML form at them and get the submission emailed to you."
      >
        <Button asChild size="sm">
          <Link href="/dashboard/forms/new">
            <PlusIcon className="mr-2 size-4" />
            New form
          </Link>
        </Button>
      </PageHeader>

      {forms.length === 0 ? (
        <EmptyState
          icon={FileTextIcon}
          title="No forms yet"
          description="Create a form to get an endpoint you can point any HTML form at."
          action={
            <Button asChild size="sm">
              <Link href="/dashboard/forms/new">Create your first form</Link>
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {forms.map((form) => {
            const design = getDesign(form.templateKey);
            return (
              <Link key={form.id} href={`/dashboard/forms/${form.id}`} className="group">
                <Card className="h-full">
                  <CardContent className="space-y-3 p-5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex size-9 items-center justify-center rounded-lg bg-brand/10 text-brand transition-colors group-hover:bg-brand/15">
                        <FileTextIcon className="size-4" />
                      </div>
                      <StatusBadge
                        status={form.enabled ? "active" : "disabled"}
                      />
                    </div>
                    <div>
                      <p className="text-card-title truncate">{form.name}</p>
                      <p className="text-caption text-muted-foreground">
                        {design?.name ?? form.templateKey}
                      </p>
                    </div>
                    <p className="text-mono truncate rounded-md bg-muted px-2 py-1 text-muted-foreground">
                      /f/{form.endpointToken}
                    </p>
                  </CardContent>
                </Card>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
