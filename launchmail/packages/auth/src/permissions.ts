import { createAccessControl } from "better-auth/plugins/access";

export const statement = {
  organization: ["update", "delete"],
  member: ["create", "update", "delete"],
  invitation: ["create", "cancel"],
  smtpConfig: ["read", "create", "update", "delete", "test"],
  domain: ["read", "create", "delete"],
  apiKey: ["read", "create", "revoke"],
  template: ["read", "create", "update", "delete"],
  form: ["read", "create", "update", "delete"],
  submission: ["read", "delete"],
  email: ["read", "send"],
  suppression: ["read", "create", "delete"],
  webhook: ["read", "create", "update", "delete"],
  audience: ["read", "create", "update", "delete"],
  broadcast: ["read", "create"],
  // Incoming mail (IMAP inbox): read messages, reply, and trigger a manual sync.
  inbox: ["read", "reply", "sync"],
} as const;

export const ac = createAccessControl(statement);

export const reader = ac.newRole({
  smtpConfig: ["read"],
  domain: ["read"],
  apiKey: ["read"],
  template: ["read"],
  form: ["read"],
  submission: ["read"],
  email: ["read"],
  suppression: ["read"],
  webhook: ["read"],
  audience: ["read"],
  broadcast: ["read"],
  inbox: ["read", "sync"],
});

export const writer = ac.newRole({
  smtpConfig: ["read", "create", "update", "delete", "test"],
  domain: ["read", "create", "delete"],
  apiKey: ["read", "create", "revoke"],
  template: ["read", "create", "update", "delete"],
  form: ["read", "create", "update", "delete"],
  submission: ["read", "delete"],
  email: ["read", "send"],
  suppression: ["read", "create", "delete"],
  webhook: ["read", "create", "update", "delete"],
  audience: ["read", "create", "update", "delete"],
  broadcast: ["read", "create"],
  inbox: ["read", "reply", "sync"],
});

export const admin = ac.newRole({
  organization: ["update", "delete"],
  member: ["create", "update", "delete"],
  invitation: ["create", "cancel"],
  smtpConfig: ["read", "create", "update", "delete", "test"],
  domain: ["read", "create", "delete"],
  apiKey: ["read", "create", "revoke"],
  template: ["read", "create", "update", "delete"],
  form: ["read", "create", "update", "delete"],
  submission: ["read", "delete"],
  email: ["read", "send"],
  suppression: ["read", "create", "delete"],
  webhook: ["read", "create", "update", "delete"],
  audience: ["read", "create", "update", "delete"],
  broadcast: ["read", "create"],
  inbox: ["read", "reply", "sync"],
});

export const roles = { admin, writer, reader };

export type AppRole = keyof typeof roles;

export const APP_ROLES: AppRole[] = ["admin", "writer", "reader"];

type Resource = keyof typeof statement;

export function hasPermission(
  role: AppRole,
  resource: Resource,
  action: string,
): boolean {
  const request = { [resource]: [action] } as Record<string, string[]>;
  return roles[role].authorize(request).success;
}

export function isAppRole(value: unknown): value is AppRole {
  return value === "admin" || value === "writer" || value === "reader";
}

// Numeric ordering of roles so privilege can be compared: reader < writer <
// admin. Used to clamp the role granted by API-key / SMTP-token endpoints so a
// caller can never mint credentials more privileged than themselves.
const ROLE_RANK: Record<AppRole, number> = {
  reader: 1,
  writer: 2,
  admin: 3,
};

export function roleRank(role: AppRole): number {
  return ROLE_RANK[role];
}

export function canGrantRole(actor: AppRole, target: AppRole): boolean {
  return roleRank(target) <= roleRank(actor);
}
