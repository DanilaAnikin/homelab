"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { authClient } from "@/lib/auth-client";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { Badge } from "@workspace/ui/components/badge";
import { Label } from "@workspace/ui/components/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@workspace/ui/components/select";
import {
  DataTable,
  type DataTableColumn,
} from "@workspace/ui/components/data-table";
import { MailPlusIcon } from "lucide-react";

type Role = "admin" | "writer" | "reader";

interface MemberRow {
  id: string;
  userId: string;
  name: string;
  email: string;
  role: string;
}
interface InviteRow {
  id: string;
  email: string;
  role: string;
}

const ROLES: Role[] = ["admin", "writer", "reader"];

// Tonal pill per role: admin reads as the privileged tier, writer mid, reader
// muted — all on the single Badge pill system.
const ROLE_VARIANT: Record<string, "brand" | "info" | "pending"> = {
  admin: "brand",
  writer: "info",
  reader: "pending",
};

type DirectoryRow =
  | { kind: "member"; member: MemberRow }
  | { kind: "invite"; invite: InviteRow };

export function MembersManager({
  organizationId,
  currentUserId,
  role,
  members,
  invitations,
}: {
  organizationId: string;
  currentUserId: string;
  role: Role;
  members: MemberRow[];
  invitations: InviteRow[];
}) {
  const router = useRouter();
  const isAdmin = role === "admin";
  const [email, setEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<Role>("writer");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function invite() {
    if (!email.trim()) return;
    setBusy(true);
    setMessage(null);
    const res = await authClient.organization.inviteMember({
      email: email.trim(),
      role: inviteRole,
      organizationId,
    });
    setBusy(false);
    if (res.error) {
      setMessage(res.error.message ?? "Failed to invite");
    } else {
      setEmail("");
      setMessage(`Invitation sent to ${email.trim()}`);
      router.refresh();
    }
  }

  async function changeRole(memberId: string, newRole: Role) {
    setMessage(null);
    const res = await authClient.organization.updateMemberRole({
      memberId,
      role: newRole,
      organizationId,
    });
    if (res.error) {
      setMessage(res.error.message ?? "Failed to update role");
      return;
    }
    router.refresh();
  }

  async function remove(memberId: string) {
    setMessage(null);
    const res = await authClient.organization.removeMember({
      memberIdOrEmail: memberId,
      organizationId,
    });
    if (res.error) {
      setMessage(res.error.message ?? "Failed to remove member");
      return;
    }
    router.refresh();
  }

  async function cancelInvite(invitationId: string) {
    setMessage(null);
    const res = await authClient.organization.cancelInvitation({ invitationId });
    if (res.error) {
      setMessage(res.error.message ?? "Failed to cancel invitation");
      return;
    }
    router.refresh();
  }

  const rows: DirectoryRow[] = [
    ...members.map((member): DirectoryRow => ({ kind: "member", member })),
    ...invitations.map((invite): DirectoryRow => ({ kind: "invite", invite })),
  ];

  const columns: DataTableColumn<DirectoryRow>[] = [
    {
      id: "member",
      header: "Member",
      cell: (row) => {
        if (row.kind === "invite") {
          return (
            <div className="min-w-0">
              <div className="truncate text-body-strong">{row.invite.email}</div>
              <div className="text-caption text-muted-foreground">
                Pending invite
              </div>
            </div>
          );
        }
        const m = row.member;
        return (
          <div className="min-w-0">
            <div className="truncate text-body-strong">{m.name}</div>
            <div className="truncate text-caption text-muted-foreground">
              {m.email}
            </div>
          </div>
        );
      },
    },
    {
      id: "role",
      header: "Role",
      cell: (row) => {
        if (row.kind === "invite") {
          return (
            <Badge variant="outline" className="capitalize">
              {row.invite.role}
            </Badge>
          );
        }
        const m = row.member;
        const isSelf = m.userId === currentUserId;
        if (isAdmin && !isSelf) {
          return (
            <Select
              value={m.role}
              onValueChange={(v) => changeRole(m.id, v as Role)}
            >
              <SelectTrigger size="sm" className="w-32 capitalize">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ROLES.map((r) => (
                  <SelectItem key={r} value={r} className="capitalize">
                    {r}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          );
        }
        return (
          <Badge
            variant={ROLE_VARIANT[m.role.toLowerCase()] ?? "pending"}
            className="capitalize"
          >
            {m.role}
          </Badge>
        );
      },
    },
    {
      id: "actions",
      align: "right",
      cellClassName: "w-px",
      cell: (row) => {
        if (row.kind === "invite") {
          return (
            isAdmin && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => cancelInvite(row.invite.id)}
              >
                Cancel
              </Button>
            )
          );
        }
        const m = row.member;
        const isSelf = m.userId === currentUserId;
        return (
          isAdmin &&
          !isSelf && (
            <Button size="sm" variant="ghost" onClick={() => remove(m.id)}>
              Remove
            </Button>
          )
        );
      },
    },
  ];

  return (
    <div className="space-y-6">
      {isAdmin && (
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex-1 space-y-1.5">
            <Label htmlFor="invite-email">Invite by email</Label>
            <Input
              id="invite-email"
              type="email"
              placeholder="teammate@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invite-role">Role</Label>
            <Select
              value={inviteRole}
              onValueChange={(v) => setInviteRole(v as Role)}
            >
              <SelectTrigger id="invite-role" className="w-32 capitalize">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ROLES.map((r) => (
                  <SelectItem key={r} value={r} className="capitalize">
                    {r}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            onClick={invite}
            disabled={busy || !email.trim()}
            loading={busy}
            iconStart={<MailPlusIcon />}
          >
            Send invite
          </Button>
        </div>
      )}
      {message && <p className="text-caption text-muted-foreground">{message}</p>}

      <DataTable
        columns={columns}
        data={rows}
        getRowKey={(row) =>
          row.kind === "member" ? `m-${row.member.id}` : `i-${row.invite.id}`
        }
        getRowStatus={(row) => (row.kind === "invite" ? "pending" : undefined)}
        emptyState="No members yet."
      />
    </div>
  );
}
