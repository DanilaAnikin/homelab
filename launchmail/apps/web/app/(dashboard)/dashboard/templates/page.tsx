import Link from "next/link";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@workspace/ui/components/button";
import { Badge } from "@workspace/ui/components/badge";
import { Card, CardContent } from "@workspace/ui/components/card";
import { PlusIcon, LayoutTemplateIcon, CodeIcon, BlocksIcon } from "lucide-react";

export const dynamic = "force-dynamic";

interface TemplateRow {
  id: string;
  name: string;
  mode: "html" | "blocks";
  updatedAt: string;
}

export default async function TemplatesPage() {
  const templates = (await apiGet<TemplateRow[]>("/api/templates")) ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <PageHeader
        title="Templates"
        description="Design reusable email templates with the visual builder or raw HTML."
      >
        <Button asChild size="sm">
          <Link href="/dashboard/templates/new">
            <PlusIcon className="mr-2 size-4" />
            New template
          </Link>
        </Button>
      </PageHeader>

      {templates.length === 0 ? (
        <EmptyState
          icon={LayoutTemplateIcon}
          title="No templates yet"
          description="Create a reusable email template — drag blocks together or write HTML."
          action={
            <Button asChild size="sm">
              <Link href="/dashboard/templates/new">Build your first template</Link>
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {templates.map((t) => (
            <Link key={t.id} href={`/dashboard/templates/${t.id}`} className="group">
              <Card className="h-full">
                <CardContent className="space-y-3 p-5">
                  <div className="flex items-start justify-between">
                    <div className="flex size-9 items-center justify-center rounded-lg bg-brand/10 text-brand transition-colors group-hover:bg-brand/15">
                      {t.mode === "html" ? (
                        <CodeIcon className="size-4" />
                      ) : (
                        <BlocksIcon className="size-4" />
                      )}
                    </div>
                    <Badge variant="secondary">
                      {t.mode === "html" ? "HTML" : "Visual"}
                    </Badge>
                  </div>
                  <div>
                    <p className="text-card-title truncate">{t.name}</p>
                    <p className="text-caption text-muted-foreground">
                      Updated{" "}
                      {new Date(t.updatedAt).toLocaleDateString(undefined, {
                        month: "short",
                        day: "numeric",
                      })}
                    </p>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
