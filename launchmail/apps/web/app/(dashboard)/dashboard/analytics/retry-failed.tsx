"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@workspace/ui/components/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@workspace/ui/components/card";
import { Badge } from "@workspace/ui/components/badge";
import { EmptyState } from "@/components/empty-state";
import { toast } from "@workspace/ui/components/sonner";
import { RotateCcwIcon, CheckCircle2Icon } from "lucide-react";
import { retryJobAction } from "./actions";

interface FailedJob {
  id: string;
  to: { email: string }[];
  subject: string;
  failedReason: string;
  attemptsMade: number;
}

export function RetryFailed({ jobs }: { jobs: FailedJob[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);

  async function retry(id: string) {
    setBusy(id);
    const res = await retryJobAction(id);
    setBusy(null);
    if (res?.error) return toast.error(res.error);
    toast.success("Re-queued for delivery");
    router.refresh();
  }

  if (jobs.length === 0) {
    return (
      <EmptyState
        icon={CheckCircle2Icon}
        title="No failed deliveries"
        description="Jobs that exhaust their retries show up here so you can re-queue them."
      />
    );
  }

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b px-4 py-4">
        <CardTitle>Failed deliveries</CardTitle>
        <CardDescription className="text-caption">
          {jobs.length} {jobs.length === 1 ? "job" : "jobs"} exhausted their
          retries.
        </CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        <ul>
          {jobs.map((j) => (
            <li
              key={j.id}
              className="relative flex items-center justify-between gap-3 border-b border-border bg-destructive-tint/40 py-3 pr-4 pl-4 last:border-b-0 before:absolute before:inset-y-0 before:left-0 before:w-0.5 before:bg-destructive"
            >
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <p className="truncate text-body-strong text-foreground">
                    {j.subject || "(no subject)"}
                  </p>
                  <Badge variant="destructive-tonal" className="shrink-0">
                    {j.attemptsMade}{" "}
                    {j.attemptsMade === 1 ? "attempt" : "attempts"}
                  </Badge>
                </div>
                <p className="truncate text-caption text-muted-foreground">
                  <span className="text-mono">
                    {j.to.map((r) => r.email).join(", ")}
                  </span>{" "}
                  · <span className="text-destructive-on">{j.failedReason}</span>
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => retry(j.id)}
                disabled={busy === j.id}
              >
                <RotateCcwIcon className="mr-1 size-3.5" />
                Retry
              </Button>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
