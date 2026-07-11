import Link from "next/link";
import { ArrowLeftIcon } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { TemplateBuilder } from "../template-builder";

export default function NewTemplatePage() {
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
        <PageHeader
          title="New template"
          description="Switch between the visual builder and HTML — the preview updates live."
        />
      </div>
      <TemplateBuilder />
    </div>
  );
}
