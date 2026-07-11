"use client";

import { Button } from "@workspace/ui/components/button";
import { DownloadIcon } from "lucide-react";

interface Submission {
  data: Record<string, string>;
  emailStatus: string;
  createdAt: string;
}

function csvCell(v: string): string {
  if (/[",\n]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
  return v;
}

export function ExportSubmissions({
  submissions,
  formName,
}: {
  submissions: Submission[];
  formName: string;
}) {
  function exportCsv() {
    const keys = [
      ...new Set(
        submissions.flatMap((s) =>
          Object.keys(s.data).filter((k) => !k.startsWith("_")),
        ),
      ),
    ];
    const header = ["submitted_at", "status", ...keys];
    const rows = submissions.map((s) => [
      new Date(s.createdAt).toISOString(),
      s.emailStatus,
      ...keys.map((k) => s.data[k] ?? ""),
    ]);
    const csv = [header, ...rows]
      .map((r) => r.map((c) => csvCell(String(c))).join(","))
      .join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${formName.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-submissions.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Button variant="outline" size="sm" onClick={exportCsv}>
      <DownloadIcon className="mr-1.5 size-3.5" />
      Export CSV
    </Button>
  );
}
