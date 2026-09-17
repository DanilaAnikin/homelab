import { requireOrg } from "@/lib/org";
import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import { StatusBadge } from "@/components/status-badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@workspace/ui/components/card";
import { MembersManager } from "./members-manager";
import { OrgSettings } from "./org-settings";
import { AuditLogTable, type AuditRow } from "./audit-log-table";
import {
  ApiKeysManager,
  type ApiKeyRow,
  type MailboxOption,
} from "./api-keys-manager";

export const dynamic = "force-dynamic";

interface FullOrg {
  id: string;
  name: string;
  slug: string;
  members: {
    id: string;
    role: string;
    userId: string;
    user: { name: string; email: string };
  }[];
  invitations: {
    id: string;
    email: string;
    role: string | null;
    status: string;
  }[];
}

export default async function OrganizationPage() {
  const ctx = await requireOrg();

  const [org, audit, apiKeys, smtpConfigs] = await Promise.all([
    apiGet<FullOrg>(
      `/api/auth/organization/get-full-organization?organizationId=${ctx.organizationId}`,
    ),
    apiGet<AuditRow[]>("/api/audit").then((r) => r ?? []),
    apiGet<ApiKeyRow[]>("/api/api-keys").then((r) => r ?? []),
    apiGet<{ id: string; name: string; fromAddress: string }[]>(
      "/api/smtp-configs",
    ).then((r) => r ?? []),
  ]);

  const mailboxes: MailboxOption[] = smtpConfigs
    .map((c) => ({ id: c.id, label: c.fromAddress || c.name }))
    .sort((a, b) => a.label.localeCompare(b.label));

  const members = (org?.members ?? []).map((m) => ({
    id: m.id,
    role: m.role,
    userId: m.userId,
    name: m.user?.name ?? "",
    email: m.user?.email ?? "",
  }));

  const invitations = (org?.invitations ?? [])
    .filter((i) => i.status === "pending")
    .map((i) => ({
      id: i.id,
      email: i.email,
      role: i.role ?? "reader",
      status: i.status,
    }));

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <PageHeader
        title="Organization"
        description="Manage your team, roles, and workspace settings."
      >
        <StatusBadge status={ctx.role} className="capitalize" />
      </PageHeader>

      <Card>
        <CardHeader>
          <CardTitle className="text-card-title">
            Members &amp; invitations
          </CardTitle>
          <CardDescription>
            Admins can invite people and change roles. Writers can manage
            resources and send; readers have view-only access.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <MembersManager
            organizationId={ctx.organizationId}
            currentUserId={ctx.userId}
            role={ctx.role}
            members={members}
            invitations={invitations}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-card-title">API keys</CardTitle>
          <CardDescription>
            Keys for agents, scripts and integrations. A whole-organization key
            works across every mailbox; a mailbox key is limited to one mailbox.
            The role bounds what the key can do.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ApiKeysManager
            role={ctx.role}
            keys={apiKeys}
            mailboxes={mailboxes}
          />
        </CardContent>
      </Card>

      <OrgSettings
        organizationId={ctx.organizationId}
        name={org?.name ?? ""}
        slug={org?.slug ?? ""}
        role={ctx.role}
      />

      <Card>
        <CardHeader>
          <CardTitle className="text-card-title">Activity log</CardTitle>
          <CardDescription>Recent changes in this workspace.</CardDescription>
        </CardHeader>
        <CardContent className="px-3 pb-3">
          <AuditLogTable audit={audit} />
        </CardContent>
      </Card>
    </div>
  );
}
