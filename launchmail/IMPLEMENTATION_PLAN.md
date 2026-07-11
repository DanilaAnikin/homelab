# LaunchMail — Implementation Plan (Organizations, Form Templates, Agentic MCP/CLI)

This plan extends the current `main` architecture (Next.js 16 dashboard + Hono API,
Better Auth, Drizzle/Postgres, BullMQ+Redis worker on `apps/api`, `@workspace/mail-queue`
core) **without** changing the queue/runtime model. Decisions confirmed with the owner:

- **Organizations** via Better Auth's **Organization plugin**; SMTP configs, API keys, domains,
  logs, and forms become **organization-owned** (shared by the team).
- **Roles:** `admin` (everything), `writer` (manage resources + send, no org/member/billing
  control), `reader` (read-only).
- **Templates** behave **Formspree-style**: each form gets a public endpoint; a submission is
  rendered into the chosen HTML design and emailed to the org's configured recipient(s).
- **Agentic access** via an **MCP server** (primary) + a **thin CLI**, both authenticated by an
  org/role-scoped API key and driving the same REST API.

Legend for effort: ◍ small · ◍◍ medium · ◍◍◍ large.

---

## 0. Cross-cutting foundations (do first)

### 0.1 Migrations + backfill (replace `db:push` with versioned migrations) ◍◍
The template syncs schema with `drizzle-kit push` and has no migrations folder. For the data
changes below we need **safe, reviewable migrations** + a one-time **backfill**.

- Add `packages/db/drizzle/migrations` and switch `db:*` scripts to `generate` + `migrate`.
- Backfill script `packages/db/src/backfill/0001_orgs.ts`:
  1. For every existing `user`, create a personal `organization` (`{name}'s Workspace`), add the
     user as `member` with role `admin`.
  2. Set `organization_id` on that user's `smtp_configs`, `api_tokens`, `email_logs`.
  3. Set each user's `activeOrganizationId`.
- Run order: migrate (add nullable `organization_id`) → backfill → migrate (set `NOT NULL`).

### 0.2 Central access-control module ◍◍
`packages/auth/src/permissions.ts` — single source of truth used by web, API, and MCP.

```ts
import { createAccessControl } from "better-auth/plugins/access";

export const statement = {
  organization: ["read", "update", "delete", "billing"],
  member:       ["read", "invite", "update-role", "remove"],
  smtpConfig:   ["read", "create", "update", "delete", "test"],
  domain:       ["read", "create", "delete"],
  apiKey:       ["read", "create", "revoke"],
  template:     ["read", "create", "update", "delete"],
  form:         ["read", "create", "update", "delete"],
  submission:   ["read", "delete"],
  email:        ["read", "send"],
  suppression:  ["read", "create", "delete"],
} as const;

const ac = createAccessControl(statement);
export const reader = ac.newRole({ /* every resource: ["read"] only */ });
export const writer = ac.newRole({ /* read+create+update+delete+test+send on resource set; org/member = read */ });
export const admin  = ac.newRole({ /* all statements */ });
export { ac };
```

Helpers: `requirePermission(ctx, resource, action)` for Hono + server actions, returning 403 on
deny. Same checks run for session users and API-key callers (the key resolves to org + role).

### 0.3 Active-organization context ◍◍
- Web: Better Auth session carries `activeOrganizationId`. A server util
  `getOrgContext()` returns `{ user, organizationId, role }`.
- API/MCP/CLI: API key resolves to `{ organizationId, role }` (see §1.4). One middleware
  populates the same context shape for both auth modes.
- **Every data query is scoped by `organization_id`** + a permission check. Add a lint/review
  checklist item: no resource query without an org filter.

**Gate after Phase 0:** typecheck/lint/build green; backfill dry-run verified on a DB copy.

---

## 1. Organizations & roles

### 1.1 Better Auth plugin wiring ◍◍ — `packages/auth/src/auth.ts`
```ts
import { organization } from "better-auth/plugins";
import { ac, admin, writer, reader } from "./permissions";

plugins: [
  organization({
    ac, roles: { admin, writer, reader },
    creatorRole: "admin",
    sendInvitationEmail: async (data) => { /* enqueue via @workspace/mail-queue, §1.5 */ },
    organizationLimit: 20,
    invitationExpiresIn: 60 * 60 * 48,
  }),
]
```
Client: add `organizationClient({ ac, roles })` to `apps/web/lib/auth-client.ts`.

### 1.2 Schema (Better Auth-managed + ours) ◍◍ — `packages/db/src/schemas`
- Plugin tables: `organization`, `member`, `invitation` (generate via Better Auth CLI / mirror in
  Drizzle schema). `session.activeOrganizationId` added.
- Add `organizationId` (FK → organization, `onDelete cascade`) to: `smtp_configs`, `api_tokens`,
  `email_logs`, and new `forms` / `form_submissions` (§2).
- Index every `organizationId`.

### 1.3 Auto-create first org — and skip it for invited users ◍◍
Controlled at the application flow, not a blind DB hook:
- **Normal sign-up** (`/sign-up`): after `signUp.email` success, call
  `organization.create({ name: "${name}'s Workspace", slug })` then `setActive`.
- **Invited sign-up** (`/invite/[token]`): the page carries the invitation token. After
  sign-up/sign-in, call `organization.acceptInvitation({ invitationId })` and **do not** create a
  personal org. Active org = the joined org.
- Guard: a `createDefaultOrgForUser()` server action that is a no-op if the user already belongs to
  any org (covers race/edge cases).

### 1.4 API keys → org + role ◍◍ — `api_tokens` evolves into org-scoped keys
- Columns: add `organization_id`, `role` (`admin|writer|reader`), keep `token_hash`,
  `token_prefix`, `last_used_at`, `expires_at`. `smtp_config_id` becomes optional (a key is no
  longer bound to one SMTP config; sending picks the org's default/specified config).
- Plus a **Personal Access Token** variant (user-scoped, `organization_id` null) for the MCP/CLI
  "manage *all* my orgs" case — it can list the user's orgs and act in each with the user's role
  there (§3).
- Auth middleware: `Bearer` key → look up by hash → `{ organizationId|null, userId, role }`.

### 1.5 Invitations via our own mail (dogfood) ◍
`sendInvitationEmail` renders an invite template (one of the §2 designs) and enqueues it through
`@workspace/mail-queue` using a configurable **system SMTP connection** (org default, or a
platform-level `SYSTEM_SMTP_*`). Fallback: surface the invite URL in the UI to copy. Email
contains `/invite/{token}`.

### 1.6 Permission matrix (authoritative)

| Capability | Admin | Writer | Reader |
|---|:--:|:--:|:--:|
| View dashboard / logs / configs / templates | ✓ | ✓ | ✓ |
| Send email / use form endpoints' settings | ✓ | ✓ | ✗ |
| Create/edit/delete SMTP configs, domains, API keys | ✓ | ✓ | ✗ |
| Create/edit/delete forms & templates | ✓ | ✓ | ✗ |
| Invite / remove members, change roles | ✓ | ✗ | ✗ |
| Org settings (name/slug/branding), billing | ✓ | ✗ | ✗ |
| Delete organization | ✓ (owner) | ✗ | ✗ |

### 1.7 UI ◍◍◍ — `apps/web`
- **Org switcher** in `dashboard-sidebar.tsx`: dropdown listing the user's orgs, active org, and
  "Create organization" (dialog using the new `dialog.tsx`).
- **Members page** `/dashboard/settings/members`: table (avatar, email, role `Select`), invite
  form (email + role), pending invitations (resend/revoke). Admin-only mutations.
- **Org settings** `/dashboard/settings/organization`: name, slug, logo, default reply-to, system
  SMTP; danger zone (delete) — owner/admin.
- **Invite acceptance** `/invite/[token]`: shows org + inviter; "Accept" → auth (no personal org)
  → join → redirect to dashboard.
- **Role-gating**: a `<Can permission="...">` wrapper + server-side guards; Reader sees disabled/
  hidden mutations and role badges.
- Scope existing pages (overview, smtp, logs) to the active org.

**Gate:** integration tests for create-org → invite → accept → role enforcement (Reader blocked
from mutations; Writer blocked from member mgmt; cross-org isolation).

---

## 2. Form templates (Formspree-style)

### 2.1 Concept
A **form** = a named instance bound to one of 20+ HTML designs. It exposes a **public endpoint**
`POST /f/{token}`. A submission is validated, stored, rendered into the design, and **emailed to
the form's recipient(s)** (reply-to = submitter's email if present). Response is either a redirect
to a thank-you URL (HTML form post) or `{ ok: true }` (AJAX/JSON).

### 2.2 Schema ◍◍ — `packages/db/src/schemas/forms-schema.ts`
- `forms`: `id`, `organization_id`, `name`, `slug`, `endpoint_token` (unique, used in `/f/`),
  `template_key` (which design), `subject` (templated), `recipients` (jsonb string[]),
  `reply_to_field` (which submitted field is the reply-to, e.g. `email`),
  `branding` (jsonb: logo, brand color, accent, footer), `field_config` (jsonb: labels/order/show),
  `custom_html` (nullable full override), `settings` (jsonb: honeypot field name, captcha provider,
  redirect_url, allowed_origins[], store_submissions, max_per_hour), `enabled`, `created_by`,
  timestamps.
- `form_submissions`: `id`, `form_id`, `data` (jsonb), `meta` (jsonb: ip, ua, referer),
  `email_status` (`queued|sent|failed`), `email_log_id` (nullable), `created_at`.

### 2.3 Template registry (the 20+ designs) ◍◍◍ — `packages/templates`
A new workspace package holding the designs as **responsive, inline-CSS, table-based HTML**
(authored via MJML compiled to HTML at build, then editable). Each entry:
`{ key, name, category, description, html, sampleData, variables[] }`. Rendering uses a safe
`{{variable}}` interpolator (Handlebars with HTML-escaping) + a `{{_all_fields}}` block that renders
every submitted field as a labeled table.

Proposed catalogue (≥24, covering use-cases × visual styles):
1. Contact form · 2. Feedback / NPS · 3. Support ticket · 4. Bug report · 5. Waitlist signup ·
6. Newsletter subscribe (double opt-in) · 7. Demo request · 8. Quote / estimate request ·
9. Booking / appointment · 10. Event RSVP · 11. Job application · 12. Lead capture ·
13. Partnership inquiry · 14. Donation receipt · 15. Survey response · 16. Order / quote request ·
17. Review / testimonial · 18. Beta access request · 19. Abandoned-form follow-up ·
20. Generic "new submission" (minimal) · 21. Branded card · 22. Dark theme ·
23. Receipt/invoice style · 24. Plain-text-first (high deliverability).
Each is mobile-responsive, dark-mode-aware, and tested against the common-client HTML subset.

### 2.4 Public submission endpoint ◍◍ — `packages/server/src/forms-public.ts`
`POST /f/:token` (mounted on web at `apps/web/app/f/[token]/route.ts`, Node runtime):
1. Resolve form by `endpoint_token`; 404 if missing/disabled.
2. Accept `urlencoded`, `multipart/form-data`, or JSON. Enforce max body size.
3. **Spam protection**: honeypot field (`_gotcha`), optional Turnstile/hCaptcha verify,
   Redis-backed rate limit (`max_per_hour` per form+IP), `allowed_origins` CORS check.
4. Persist `form_submissions` row (if `store_submissions`).
5. Render `template_key` (or `custom_html`) with the data → enqueue email via existing BullMQ to
   `recipients`, `replyTo` from `reply_to_field`.
6. Respond: 303 redirect to `redirect_url` (form post) or `{ ok: true, id }` (JSON/AJAX).
Public, **no auth** (Formspree-style) — security is origin + spam + rate-limit.

> **CORS note (important):** main now restricts the dashboard API CORS to
> `["http://localhost:3000", "http://localhost:5000"]`. The public `/f/:token` route must **not**
> inherit that — it serves its own CORS derived from the form's `allowed_origins` (default `*` so
> plain HTML `<form>` posts and customer-site AJAX both work; tighten per form). Mounting it on the
> Next app (`apps/web/app/f/[token]/route.ts`), outside the Hono dashboard CORS middleware, keeps
> the two policies separate.

### 2.5 Dashboard UI ◍◍◍ — `/dashboard/forms`
- **Gallery / template picker**: grid of designs with **live preview** (iframe rendering the design
  with `sampleData`).
- **Create form**: name → pick design → recipients → done; show the generated endpoint.
- **Form editor**: two-pane — left controls (subject, recipients, reply-to field, branding, spam,
  redirect, allowed origins, optional custom HTML), right **live preview** that re-renders on change.
- **Endpoint panel**: copyable `curl`, an HTML `<form action="…/f/{token}">` snippet, and an
  AJAX/`fetch` snippet.
- **Submissions inbox**: per-form table of submissions (fields, date, email status) + detail view.
- Role-gated (Reader read-only; Writer/Admin manage).

**Gate:** unit tests for render + honeypot/rate-limit/origin; integration test: `POST /f/:token` →
submission stored → email enqueued → worker delivers (Mailpit/real SMTP) → status `sent`.

---

## 3. Agentic MCP server + CLI

### 3.1 Shared SDK ◍◍ — `packages/sdk`
A typed client over the REST API (fetch-based, API-key auth, base-URL configurable). Methods mirror
every operation (orgs, members, smtp, domains, keys, forms, submissions, email, logs,
suppressions). Consumed by both MCP and CLI so behavior + permissions are identical.

### 3.2 REST surface completion ◍◍ — `packages/server`
Ensure first-class REST endpoints exist for everything agents need (some already exist; add the
rest): `/api/orgs*`, `/api/orgs/:id/members*`, `/api/smtp-configs*`, `/api/domains*`,
`/api/api-keys*`, `/api/forms*`, `/api/forms/:id/submissions*`, `/api/mail/send`, `/api/logs*`,
`/api/suppressions*`. Every route runs `requirePermission` against the caller's org+role.

### 3.3 MCP server ◍◍◍ — `apps/mcp`
- Node server using `@modelcontextprotocol/sdk`. Transports: **stdio** (Claude Desktop/Code) and
  **streamable HTTP** (remote agents).
- Config via env: `LAUNCHMAIL_API_URL`, `LAUNCHMAIL_API_KEY` (a PAT for multi-org, or org key for
  single-org).
- **Tools** (each maps to an SDK call; server enforces permissions, so a `reader` key simply can't
  call mutating tools):
  - `list_organizations`, `get_organization`, `create_organization`, `update_organization`,
    `delete_organization`
  - `list_members`, `invite_member`, `update_member_role`, `remove_member`
  - `list_smtp_configs`, `create_smtp_config`, `update_smtp_config`, `delete_smtp_config`,
    `test_smtp_connection`
  - `list_domains`, `add_domain`, `delete_domain`
  - `list_api_keys`, `create_api_key`, `revoke_api_key`
  - `list_forms`, `create_form`, `update_form`, `get_form_endpoint`, `list_submissions`
  - `send_email`, `get_email_logs`, `get_email_status`
  - `list_suppressions`, `add_suppression`, `remove_suppression`
- Multi-org: a **PAT** lets the agent `list_organizations` and pass `organizationId` to scoped
  tools; the server checks the user's role in each target org. Resources/prompts: expose a
  `whoami`/capabilities resource so the agent knows its role and orgs.
- Ship a ready Claude MCP config snippet in the README.

### 3.4 CLI ◍◍ — `apps/cli` (`launchmail` bin)
- `commander`/`clipanion`-based, wraps `packages/sdk`. Config from env or `~/.launchmail/config.json`
  (`api_url`, `api_key`, default org).
- Commands mirror the tools: `launchmail orgs list`, `launchmail members invite …`,
  `launchmail smtp create …`, `launchmail forms create …`, `launchmail email send …`,
  `launchmail logs …`. `--json` for scripting.

**Gate:** MCP tool tests (role-gated allow/deny), CLI command tests against a test API, and an
end-to-end "agent sends an email" path.

---

## 4. Phasing & gates

| Phase | Scope | Exit gate |
|---|---|---|
| 0 | Migrations infra, access-control module, org-context middleware | gates green; backfill dry-run |
| 1a | Org plugin + schema + auto-create/invite flow + API-key org/role | org lifecycle integration tests |
| 1b | Org UI (switcher, members, invites, settings, role-gating) | role-enforcement e2e |
| 1c | Migrate SMTP/keys/logs to org-owned + backfill | data integrity tests |
| 2a | `packages/templates` (≥24 designs) + render + forms schema | render/spam unit tests |
| 2b | Public `/f/:token` + submissions + worker render-and-send | submit→email e2e |
| 2c | Forms dashboard (gallery, editor, preview, inbox) | UI + perms tests |
| 3a | `packages/sdk` + REST completion + PAT/org keys | sdk/contract tests |
| 3b | MCP server | tool perms tests |
| 3c | CLI | command tests |

Each phase: `install → typecheck → lint → build → test` must pass; commit on green; verify real
send against launchday.cz where relevant.

---

## 5. Testing strategy
- **Unit:** permission matrix (every role × resource × action), template rendering + escaping,
  honeypot/rate-limit/origin logic, API-key hashing/scoping.
- **Integration (test DB + Mailpit):** org create→invite→accept→role enforcement, cross-org
  isolation, form submission→render→enqueue→deliver, API-key role gating.
- **E2E:** real send via the worker; MCP "send_email" through to delivery; CLI happy paths.
- Keep a dedicated `launchmail_test` DB; never run destructive migrations against production.

## 6. Risks & mitigations
- **Better Auth org-plugin API/version drift** → pin version; verify exact method names against the
  installed SDK in Phase 1a; thin wrapper so call sites are stable.
- **Public form endpoint abuse** → honeypot + captcha + Redis rate-limit + origin allowlist + body
  caps from day one.
- **Email HTML rendering across clients** → author via MJML, inline CSS, test the common subset;
  ship a plain-text part for deliverability.
- **Backfill correctness** → idempotent script, dry-run mode, transactional, verified on a DB copy
  before production.
- **Permission regressions** → centralized `requirePermission`, shared by web/API/MCP, with the
  matrix covered by unit tests.

## 7. New/changed workspaces (summary)
- `packages/db` — org/member/invitation + `organization_id` columns + forms tables; migrations + backfill.
- `packages/auth` — org plugin, `permissions.ts` (ac + roles), invite email, org-context helpers.
- `packages/mail-queue` — services scoped by org; API-key org/role; template-send helper.
- `packages/templates` *(new)* — 24+ HTML designs + renderer.
- `packages/server` — org/members/forms/public-`/f/` routes, permission middleware, REST completion.
- `packages/sdk` *(new)* — typed API client for CLI + MCP.
- `apps/web` — org switcher, members/settings/invite pages, forms gallery/editor/inbox, role-gating.
- `apps/mcp` *(new)* — MCP server (stdio + HTTP).
- `apps/cli` *(new)* — `launchmail` CLI.

## 8. Deployment (Docker) — align with what's now on main
`main` now ships a container pipeline: `Dockerfile` (web, Next `output: "standalone"`),
`Dockerfile.api` (Bun api+worker, bound to `0.0.0.0`), a deploy `docker-compose.yml` (`api` + `web`
services, `env_file: .env`, `API_URL=http://api:5000`), `.dockerignore`, and
`trustedOrigins: ["*"]` on auth. The new work must slot into this:

- **Schema/migrations:** the `web` and `api` images must run `drizzle-kit migrate` (and the one-time
  backfill) on release — add a migration step to the deploy flow (compose `command`/entrypoint or a
  dedicated `migrate` one-shot service) so org/forms tables + `organization_id` columns exist before
  the apps serve traffic. Do **not** rely on `db:push` in production.
- **New runtime services:** `apps/mcp` (HTTP transport) gets its own `Dockerfile.mcp` + a `mcp`
  compose service (expose its port; `env_file: .env`, `LAUNCHMAIL_API_URL=http://api:5000`). The
  stdio transport is used locally by agents and needs no service. `apps/cli` is published/installed
  as a bin, not a long-running service.
- **New packages** (`templates`, `sdk`) are workspace deps compiled into the existing `web`/`api`
  images via the monorepo build — ensure the Dockerfiles' `pnpm install`/build cover them
  (Turborepo prune/`--filter` if image size matters).
- **CORS/origins:** with `trustedOrigins: ["*"]`, auth works across container hostnames; keep the
  dashboard API CORS allowlist in sync with real deploy origins, and keep the public `/f/:token`
  CORS independent (see §2.4).
- **Secrets:** set a dedicated `MAIL_ENCRYPTION_KEY` (currently empty → falls back to
  `BETTER_AUTH_SECRET`) and a real per-deploy `BETTER_AUTH_SECRET` in the deploy `.env`.
