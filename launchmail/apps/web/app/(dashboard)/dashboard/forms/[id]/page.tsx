import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeftIcon } from "lucide-react";
import { requireOrg } from "@/lib/org";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { StatusBadge } from "@/components/status-badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@workspace/ui/components/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@workspace/ui/components/table";
import { FormEditor } from "./form-editor";
import { ExportSubmissions } from "./export-submissions";

export const dynamic = "force-dynamic";

interface FormDetail {
  id: string;
  name: string;
  templateKey: string;
  endpointToken: string;
  recipients: string[];
  subject: string | null;
  replyToField: string | null;
  branding: {
    brandName?: string;
    accent?: string;
    footer?: string;
    theme?: "light" | "dark";
  } | null;
  settings: {
    honeypot?: boolean;
    redirectUrl?: string;
    allowedOrigins?: string[];
    storeSubmissions?: boolean;
    maxPerHour?: number;
  } | null;
  enabled: boolean;
}
interface SubmissionRow {
  id: string;
  data: Record<string, string>;
  emailStatus: string;
  createdAt: string;
}

export default async function FormDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const ctx = await requireOrg();
  const { id } = await params;
  const form = await apiGet<FormDetail>(`/api/forms/${id}`);
  if (!form) notFound();

  const submissions =
    (await apiGet<SubmissionRow[]>(`/api/forms/${id}/submissions`)) ?? [];

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="space-y-3">
        <Link
          href="/dashboard/forms"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeftIcon className="size-4" />
          Forms
        </Link>
        <PageHeader title={form.name} description={`/f/${form.endpointToken}`} />
      </div>

      <FormEditor
        form={{
          id: form.id,
          name: form.name,
          templateKey: form.templateKey,
          endpointToken: form.endpointToken,
          recipients: form.recipients,
          subject: form.subject,
          replyToField: form.replyToField,
          branding: form.branding ?? {},
          settings: form.settings ?? {},
          enabled: form.enabled,
        }}
        role={ctx.role}
      />

      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">
              Submissions ({submissions.length})
            </CardTitle>
            {submissions.length > 0 && (
              <ExportSubmissions submissions={submissions} formName={form.name} />
            )}
          </div>
        </CardHeader>
        <CardContent>
          {submissions.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No submissions yet. POST to the endpoint above to test.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Submission</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead className="text-right">When</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {submissions.map((s) => (
                  <TableRow key={s.id}>
                    <TableCell className="max-w-[360px] truncate text-xs">
                      {Object.entries(s.data)
                        .filter(([k]) => !k.startsWith("_"))
                        .map(([k, v]) => `${k}: ${v}`)
                        .join(" · ")}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={s.emailStatus} />
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs text-muted-foreground tabular-nums">
                      {new Date(s.createdAt).toLocaleString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
