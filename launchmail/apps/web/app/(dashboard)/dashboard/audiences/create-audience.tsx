"use client";

import { useState } from "react";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { toast } from "@workspace/ui/components/sonner";
import { PlusIcon } from "lucide-react";
import { createAudienceAction } from "./actions";

export function CreateAudience() {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    const res = await createAudienceAction(name.trim());
    if (res?.error) {
      toast.error(res.error);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={create} className="flex gap-2">
      <Input
        placeholder="Newsletter subscribers"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="max-w-xs"
      />
      <Button type="submit" disabled={busy}>
        <PlusIcon className="mr-1 size-4" />
        {busy ? "Creating…" : "New audience"}
      </Button>
    </form>
  );
}
