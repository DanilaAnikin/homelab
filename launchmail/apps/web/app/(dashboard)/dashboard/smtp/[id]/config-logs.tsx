import { apiGet } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@workspace/ui/components/card";
import { Badge } from "@workspace/ui/components/badge";
import { cn } from "@workspace/ui/lib/utils";
import {
  MailIcon,
  SendIcon,
  XCircleIcon,
} from "lucide-react";

interface LogRow {
  id: string;
  status: string;
  subject: string;
  to: { email: string }[];
  createdAt: string;
  error?: string | null;
}

export async function ConfigLogs({ configId }: { configId: string }) {
  const logs =
    (await apiGet<LogRow[]>(`/api/smtp-configs/${configId}/logs`)) ?? [];

  return (
    <Card>
      <CardHeader className="border-b pb-4">
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Email logs</CardTitle>
            <p className="text-caption text-muted-foreground">
              Recent emails sent through this configuration
            </p>
          </div>
          <Badge variant="secondary" className="tabular-nums">
            {logs.length} total
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {logs.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <MailIcon className="size-8 text-muted-foreground/40" />
            <p className="text-body text-muted-foreground">No emails sent yet</p>
            <p className="text-caption text-muted-foreground/70">
              Send an email using this config to see logs here
            </p>
          </div>
        ) : (
          <div className="divide-y">
            {logs.map((log) => {
              const sent = log.status === "sent";
              return (
                <div
                  key={log.id}
                  className={cn(
                    "flex items-center justify-between px-5 py-3",
                    !sent && "bg-destructive-tint/40"
                  )}
                >
                  <div className="flex min-w-0 items-center gap-3">
                    <div
                      className={cn(
                        "flex size-8 shrink-0 items-center justify-center rounded-full",
                        sent ? "bg-success/10" : "bg-destructive/10"
                      )}
                    >
                      {sent ? (
                        <SendIcon className="size-3.5 text-success" />
                      ) : (
                        <XCircleIcon className="size-3.5 text-destructive" />
                      )}
                    </div>
                    <div className="min-w-0">
                      <p className="truncate text-body-strong">{log.subject}</p>
                      <p className="truncate text-caption text-muted-foreground">
                        To:{" "}
                        {(log.to as { email: string }[])
                          .map((r) => r.email)
                          .join(", ")}
                      </p>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    {log.error && (
                      <span
                        className="max-w-[150px] truncate text-caption text-destructive"
                        title={log.error}
                      >
                        {log.error}
                      </span>
                    )}
                    <Badge variant={sent ? "success" : "destructive-tonal"}>
                      {log.status}
                    </Badge>
                    <span className="text-mono text-xs whitespace-nowrap text-muted-foreground">
                      {new Date(log.createdAt).toLocaleString(undefined, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
