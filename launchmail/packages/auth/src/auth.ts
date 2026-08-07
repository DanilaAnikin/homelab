import { randomUUID } from "node:crypto";
import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { organization, bearer } from "better-auth/plugins";
import { db } from "@workspace/db";
import {
  member as memberTable,
  organization as organizationTable,
} from "@workspace/db/schemas";
import { enqueueEmail, getDefaultSmtpConfig } from "@workspace/mail-queue";
import { ac, admin, writer, reader } from "./permissions";
import { renderInvitationEmail } from "./invitation-email";

const appUrl = (
  process.env.BETTER_AUTH_URL ?? "http://localhost:3000"
).replace(/\/$/, "");

// Explicit allow-list of public origins for origin / CSRF / callbackURL
// validation. Prefer WEB_ORIGIN (comma-separated list), falling back to
// BETTER_AUTH_URL. Never use a wildcard, which would disable validation.
const trustedOrigins = (process.env.WEB_ORIGIN ?? appUrl)
  .split(",")
  .map((o) => o.trim().replace(/\/$/, ""))
  .filter(Boolean);

// The API is reachable only through Cloudflare Tunnel → Traefik → Next.js.
// Trust an explicit proxy-owned header, never the client-controlled left-most
// X-Forwarded-For value. Deployments outside Cloudflare can override this with
// a comma-separated list of headers their private ingress overwrites.
const ipAddressHeaders = (
  process.env.BETTER_AUTH_IP_ADDRESS_HEADERS ?? "cf-connecting-ip"
)
  .split(",")
  .map((header) => header.trim().toLowerCase())
  .filter((header) => /^[a-z0-9-]+$/.test(header));

if (ipAddressHeaders.length === 0) {
  throw new Error("BETTER_AUTH_IP_ADDRESS_HEADERS has no valid header names");
}

export const auth = betterAuth({
  secret: process.env.BETTER_AUTH_SECRET!,
  url: process.env.BETTER_AUTH_URL!,
  trustedOrigins,
  advanced: {
    ipAddress: {
      ipAddressHeaders,
    },
  },
  database: drizzleAdapter(db, { provider: "pg" }),
  emailAndPassword: {
    enabled: true,
    // No email verification anywhere: never require it for sign-in, and don't
    // auto-send verification mail.
    requireEmailVerification: false,
    autoSignIn: true,
  },
  // Every account is created already-verified, so no flow (sign-in, invite
  // accept, org join) is ever gated on email verification.
  databaseHooks: {
    user: {
      create: {
        before: async (user) => ({
          data: { ...user, emailVerified: true },
        }),
        // Give every new user a personal workspace at sign-up. The org-scoped
        // dashboard needs a context on the very first render; without an org the
        // client's org reads come back empty and the dashboard bounces to
        // /login before the on-demand ensurePersonalOrg (/api/me) can run.
        after: async (user) => {
          try {
            const base =
              (user.name || "My")
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, "-")
                .replace(/^-+|-+$/g, "") || "workspace";
            const orgId = randomUUID();
            await db.insert(organizationTable).values({
              id: orgId,
              name: `${user.name || "My"}'s Workspace`,
              slug: `${base}-${orgId.slice(0, 6)}`,
            });
            await db.insert(memberTable).values({
              id: randomUUID(),
              organizationId: orgId,
              userId: user.id,
              role: "admin",
            });
          } catch (e) {
            console.error(
              "[auth] failed to create personal org for new user:",
              (e as Error).message,
            );
          }
        },
      },
    },
  },
  plugins: [
    bearer(),
    organization({
      ac,
      roles: { admin, writer, reader },
      creatorRole: "admin",
      organizationLimit: 50,
      async sendInvitationEmail(data) {
        const inviteUrl = `${appUrl}/invite/${data.id}`;
        const config = await getDefaultSmtpConfig(data.organization.id).catch(
          () => null,
        );
        if (!config) {
          console.log(
            `[invite] No SMTP config for inviter; invite link: ${inviteUrl}`,
          );
          return;
        }
        const { subject, html, text } = renderInvitationEmail({
          organizationName: data.organization.name,
          inviterName: data.inviter.user.name,
          inviteUrl,
        });
        await enqueueEmail({
          smtpConfigId: config.id,
          organizationId: data.organization.id,
          userId: data.inviter.user.id,
          from: config.fromName
            ? `${config.fromName} <${config.fromAddress}>`
            : config.fromAddress,
          to: [{ email: data.email }],
          subject,
          html,
          text,
        }).catch((error) => {
          console.error(
            `[invite] Failed to enqueue invitation email: ${(error as Error).message}. Link: ${inviteUrl}`,
          );
        });
      },
    }),
  ],
});
