"use client";

import { useState } from "react";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { toast } from "@workspace/ui/components/sonner";
import { PlusIcon } from "lucide-react";
import { createDomainAction } from "./actions";

export function AddDomain() {
  const [domain, setDomain] = useState("");
  const [busy, setBusy] = useState(false);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!domain.trim()) return;
    setBusy(true);
    const res = await createDomainAction(
      domain.trim().replace(/^https?:\/\//, "").replace(/\/.*$/, ""),
    );
    if (res?.error) {
      toast.error(res.error);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={add} className="flex gap-2">
      <Input
        placeholder="yourdomain.com"
        value={domain}
        onChange={(e) => setDomain(e.target.value)}
        className="max-w-xs"
      />
      <Button type="submit" loading={busy}>
        {!busy && <PlusIcon className="mr-1 size-4" />}
        {busy ? "Adding…" : "Add domain"}
      </Button>
    </form>
  );
}
