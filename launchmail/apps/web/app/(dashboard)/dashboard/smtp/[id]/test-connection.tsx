"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@workspace/ui/components/card";
import { Button } from "@workspace/ui/components/button";
import { testSmtpConnectionAction } from "./actions";
import { Badge } from "@workspace/ui/components/badge";
import { CheckCircle2Icon, XCircleIcon } from "lucide-react";

export function TestConnection({ configId }: { configId: string }) {
  const [status, setStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [message, setMessage] = useState("");

  async function handleTest() {
    setStatus("loading");
    setMessage("");
    const result = await testSmtpConnectionAction(configId);
    if (result.success) {
      setStatus("success");
      setMessage("Connection successful");
    } else {
      setStatus("error");
      setMessage(result.error ?? "Connection failed");
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div className="space-y-0.5">
          <CardTitle>Test connection</CardTitle>
          <p className="text-caption text-muted-foreground">
            Verify the server accepts your credentials
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={handleTest}
          loading={status === "loading"}
        >
          {status === "loading" ? "Testing..." : "Test connection"}
        </Button>
      </CardHeader>
      {status !== "idle" && status !== "loading" && (
        <CardContent>
          <div className="flex items-center gap-2">
            {status === "success" ? (
              <>
                <CheckCircle2Icon className="size-4 text-success" />
                <Badge variant="success">{message}</Badge>
              </>
            ) : (
              <>
                <XCircleIcon className="size-4 text-destructive" />
                <span className="text-body text-destructive">{message}</span>
              </>
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}
