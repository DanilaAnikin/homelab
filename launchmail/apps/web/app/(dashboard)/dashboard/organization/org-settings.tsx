"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { authClient } from "@/lib/auth-client";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Label } from "@workspace/ui/components/label";
import { toast } from "@workspace/ui/components/sonner";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@workspace/ui/components/card";

type Role = "admin" | "writer" | "reader";

export function OrgSettings({
  organizationId,
  name,
  slug,
  role,
}: {
  organizationId: string;
  name: string;
  slug: string;
  role: Role;
}) {
  const router = useRouter();
  const [orgName, setOrgName] = useState(name);
  const [busy, setBusy] = useState(false);
  const isAdmin = role === "admin";

  async function save() {
    setBusy(true);
    const res = await authClient.organization.update({
      organizationId,
      data: { name: orgName },
    });
    setBusy(false);
    if (res.error) {
      toast.error(res.error.message ?? "Failed to save organization");
      return;
    }
    toast.success("Organization saved");
    router.refresh();
  }

  async function remove() {
    if (
      !confirm(
        "Delete this organization and all of its data? This cannot be undone.",
      )
    )
      return;
    setBusy(true);
    const res = await authClient.organization.delete({ organizationId });
    setBusy(false);
    if (res.error) {
      toast.error(res.error.message ?? "Failed to delete organization");
      return;
    }
    router.push("/dashboard");
    router.refresh();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-card-title">Settings</CardTitle>
        <CardDescription className="font-mono text-caption">
          {slug}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex items-end gap-2">
          <div className="flex-1 space-y-1.5">
            <Label htmlFor="org-name">Name</Label>
            <Input
              id="org-name"
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              disabled={!isAdmin}
            />
          </div>
          {isAdmin && (
            <Button
              onClick={save}
              disabled={busy || orgName === name}
              loading={busy}
            >
              Save
            </Button>
          )}
        </div>

        {isAdmin && (
          <div className="space-y-3">
            <p className="text-eyebrow text-destructive">Danger zone</p>
            <div className="flex items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive-tint/40 p-4">
              <div className="min-w-0">
                <p className="text-body-strong">Delete organization</p>
                <p className="text-caption text-muted-foreground">
                  Permanently remove this organization and its data.
                </p>
              </div>
              <Button
                variant="destructive"
                size="sm"
                onClick={remove}
                disabled={busy}
              >
                Delete
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
