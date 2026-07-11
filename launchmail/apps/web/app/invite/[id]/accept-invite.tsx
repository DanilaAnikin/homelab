"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { authClient } from "@/lib/auth-client";
import { Button } from "@workspace/ui/components/button";

export function AcceptInvite({ invitationId }: { invitationId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function accept() {
    setBusy(true);
    setError(null);
    const res = await authClient.organization.acceptInvitation({ invitationId });
    if (res.error) {
      setError(res.error.message ?? "Failed to accept invitation");
      setBusy(false);
      return;
    }
    if (res.data?.invitation.organizationId) {
      await authClient.organization.setActive({
        organizationId: res.data.invitation.organizationId,
      });
    }
    router.push("/dashboard");
    router.refresh();
  }

  return (
    <div className="space-y-3">
      <Button onClick={accept} loading={busy} className="w-full">
        {busy ? "Joining…" : "Accept invitation"}
      </Button>
      {error && (
        <p role="alert" className="text-body text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
