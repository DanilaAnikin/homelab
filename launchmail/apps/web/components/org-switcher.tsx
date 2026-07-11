"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { authClient } from "@/lib/auth-client";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@workspace/ui/components/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@workspace/ui/components/dialog";
import { Label } from "@workspace/ui/components/label";
import { toast } from "@workspace/ui/components/sonner";
import { BuildingIcon, CheckIcon, ChevronsUpDownIcon, PlusIcon } from "lucide-react";

export function OrgSwitcher() {
  const router = useRouter();
  const { data: organizations } = authClient.useListOrganizations();
  const { data: activeOrg } = authClient.useActiveOrganization();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  async function switchTo(organizationId: string) {
    const res = await authClient.organization.setActive({ organizationId });
    if (res.error) {
      toast.error(res.error.message ?? "Failed to switch organization");
      return;
    }
    router.refresh();
  }

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    const slug = `${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${Math.random()
      .toString(36)
      .slice(2, 8)}`;
    const res = await authClient.organization.create({ name: name.trim(), slug });
    if (res.error || !res.data) {
      setBusy(false);
      toast.error(res.error?.message ?? "Failed to create organization");
      return;
    }
    const active = await authClient.organization.setActive({
      organizationId: res.data.id,
    });
    setBusy(false);
    if (active.error) {
      toast.error(active.error.message ?? "Created, but couldn't switch to it");
      return;
    }
    setCreating(false);
    setName("");
    router.refresh();
  }

  const orgs = organizations ?? [];

  return (
    <Dialog open={creating} onOpenChange={setCreating}>
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-full justify-between gap-2 px-2 font-normal hover:bg-muted"
          >
            <span className="flex min-w-0 items-center gap-2">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-md bg-brand/10 text-brand">
                <BuildingIcon className="size-3.5" />
              </span>
              <span className="truncate text-body-strong text-foreground">
                {activeOrg?.name ?? "Workspace"}
              </span>
            </span>
            <ChevronsUpDownIcon className="size-3.5 shrink-0 opacity-60" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56">
          <DropdownMenuLabel>Organizations</DropdownMenuLabel>
          {orgs.map((org) => (
            <DropdownMenuItem
              key={org.id}
              onClick={() => switchTo(org.id)}
              className="justify-between"
            >
              <span className="truncate">{org.name}</span>
              {activeOrg?.id === org.id && <CheckIcon className="size-4" />}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DialogTrigger asChild>
            <DropdownMenuItem onSelect={(e) => e.preventDefault()}>
              <PlusIcon className="mr-2 size-4" />
              New organization
            </DropdownMenuItem>
          </DialogTrigger>
        </DropdownMenuContent>
      </DropdownMenu>

      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create organization</DialogTitle>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="org-name">Name</Label>
          <Input
            id="org-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Acme Inc"
          />
        </div>
        <DialogFooter>
          <Button onClick={create} disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
