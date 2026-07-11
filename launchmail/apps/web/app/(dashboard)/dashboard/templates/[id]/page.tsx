import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeftIcon } from "lucide-react";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { TemplateBuilder } from "../template-builder";
import type { EmailBlock } from "@workspace/templates";

export const dynamic = "force-dynamic";

interface TemplateDetail {
  id: string;
  name: string;
  mode: "html" | "blocks";
  subject: string | null;
  html: string | null;
  blocks: EmailBlock[] | null;
  accent: string | null;
  theme: "light" | "dark" | null;
}

export default async function TemplateDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const template = await apiGet<TemplateDetail>(`/api/templates/${id}`);
  if (!template) notFound();

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <div className="space-y-3">
        <Link
          href="/dashboard/templates"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-4" />
          Templates
        </Link>
        <PageHeader title={template.name} description="Edit your template." />
      </div>
      <TemplateBuilder template={template} />
    </div>
  );
}
