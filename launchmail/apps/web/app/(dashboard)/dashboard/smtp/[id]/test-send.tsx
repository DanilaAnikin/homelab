"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@workspace/ui/components/card";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { FormField, FormLabel, FormControl } from "@workspace/ui/components/form-field";
import { testSendEmailAction } from "./actions";
import { CheckCircle2Icon, XCircleIcon } from "lucide-react";

export function TestSend({ configId }: { configId: string }) {
  const [to, setTo] = useState("");
  const [status, setStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [message, setMessage] = useState("");

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    if (!to) return;
    setStatus("loading");
    setMessage("");
    const result = await testSendEmailAction(configId, to);
    if (result.success) {
      setStatus("success");
      setMessage(
        result.messageId ? `Sent! Message ID: ${result.messageId}` : "Sent!",
      );
    } else {
      setStatus("error");
      setMessage(result.error ?? "Send failed");
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Send test email</CardTitle>
        <p className="text-caption text-muted-foreground">
          Dispatch a one-off message to confirm end-to-end delivery
        </p>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSend} className="flex items-end gap-3">
          <FormField className="flex-1">
            <FormLabel>Recipient email</FormLabel>
            <FormControl>
              <Input
                type="email"
                placeholder="you@example.com"
                value={to}
                onChange={(e) => setTo(e.target.value)}
                required
              />
            </FormControl>
          </FormField>
          <Button type="submit" loading={status === "loading"}>
            {status === "loading" ? "Sending..." : "Send test"}
          </Button>
        </form>
        {status !== "idle" && status !== "loading" && (
          <div className="mt-3 flex items-center gap-2">
            {status === "success" ? (
              <>
                <CheckCircle2Icon className="size-4 text-success" />
                <span className="text-body text-success break-all">{message}</span>
              </>
            ) : (
              <>
                <XCircleIcon className="size-4 shrink-0 text-destructive" />
                <span className="text-body text-destructive">{message}</span>
              </>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
