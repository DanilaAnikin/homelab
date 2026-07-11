# LaunchMail Security & Reliability Audit — Final Report

## Executive Summary

This audit consolidated 50 raw findings into **36 distinct issues** after merging duplicates. Severity distribution (post-verification, using corrected severities):

| Severity | Count |
|----------|-------|
| Critical | 2 |
| High | 7 |
| Medium | 16 |
| Low | 11 |
| **Total** | **36** |

### Fix these first (highest urgency)

1. **Fresh-database deploy is broken (Critical).** The only production bootstrap (`tsx src/migrate.ts`) never creates the base auth/mail tables, so `docker compose up` against an empty volume fails on the first migration. The product cannot be installed.
2. **SMTP-config / API-token routes have zero role enforcement (Critical).** Any authenticated org member — including a `reader` (session or API key) — can mutate SMTP configs and, worse, mint a `writer`-scoped API token via `POST /api/smtp-configs/:id/tokens`, escalating privileges org-wide.
3. **Writer can mint an admin API key (High).** `POST /api/api-keys` accepts an arbitrary `role` and never bounds it to the caller's role, so a writer self-escalates to full admin (member/org/invitation control).
4. **Unsubscribe is not honored org-wide + double-send on retry (High).** Unsubscribe only flips one per-audience row and never suppresses the address (CAN-SPAM/GDPR defect), and the worker's non-idempotent send re-delivers email up to 3x on a post-send DB failure.
5. **SMTP password rotation silently breaks all sends (High).** The transporter cache key omits the password and is never invalidated, so the long-lived worker keeps authenticating with the old password until restarted.

### Systemic issues

- **No automated test coverage of dangerous paths.** The one "no XSS" template test exercises only the safe escaped branch; the actually-vulnerable custom-HTML interpolation, the role-enforcement gaps, and the migration bootstrap are all untested. Multiple bugs "ship green."
- **Inconsistent authorization.** `management.ts` gates every mutation with `requirePerm`, but `smtp-configs.ts` gates nothing — the lone router that omits role checks, and the source of both the Critical auth bug and a privilege-escalation chain. Token roles are also never bounded by the creator's role.
- **Errors swallowed across the stack.** `apiGet` collapses every non-200/network error to `null` (rendered as empty/zero state); the broadcast loop's `.catch(() => undefined); sent++` inflates counts; Better Auth client calls resolve `{error}` that several web handlers never inspect; `recordAudit` is fire-and-forget. Failures are routinely invisible.
- **Read-then-write races with no DB constraints.** Personal-org creation, default-SMTP-config selection, and the default-switch update are all non-atomic with no unique/partial indexes, allowing duplicate or zero-default states.
- **Public unauthenticated surface is under-hardened.** Form submit, tracking, and unsubscribe endpoints lack signing, body-size limits, SSRF protection, trustworthy rate-limiting, and HTML escaping.
- **Migration tooling is fragmented.** Three divergent mechanisms (`db:push`, dead `drizzle-kit` scripts pointing at a non-existent dir, hand-written SQL with no journal) invite schema drift and mislead operators.

---

## Critical

### **Fresh-database production migration cannot bootstrap base tables**
`packages/db/migrations/0001_organizations_and_forms.sql:18,43,46` · `packages/db/src/migrate.ts:36` · `Dockerfile.api:36`
The only prod bootstrap runs the hand-written `migrations/*.sql`, none of which `CREATE TABLE` the base `user`/`session`/`account`/`verification`/`smtp_configs`/`api_tokens`/`email_logs`. Migration 0001 instead does `CREATE TABLE member ... REFERENCES "user"(id)` and `ALTER TABLE session/...`, which fail with `42P01 undefined_table` (not in `IGNORED_CODES`) on an empty DB → `process.exit(1)`. A clean `docker compose up` is broken; the schema only ever existed because someone ran `drizzle-kit push` in dev.
**Fix:** Add a `0000` migration that creates the Better Auth + mail base tables (e.g. `@better-auth/cli generate` output plus `CREATE TABLE` for the mail tables) before `0001`, or run a populated `drizzle-kit migrate` in the bootstrap.

### **SMTP-config & API-token routes enforce no role permission (privilege escalation)**
`packages/server/src/smtp-configs.ts:133-141,177-190,216-223,266-275,437-462`
Every handler in `smtpConfigsRouter` checks only that `organizationId` is present and never calls `requirePerm`/`hasPermission`, unlike every mutating route in `management.ts`. The api-token middleware sets `role` from any valid `lm_` key regardless of role. So a `reader` (session or reader API key) can POST/PATCH/DELETE SMTP configs and run test/test-send. Most severe: `POST /api/smtp-configs/:id/tokens` calls `createApiToken` with no role, which defaults to `writer` (`api-tokens.service.ts:57`) and returns the plaintext token — a reader mints a writer-scoped key and escalates org-wide write access. Reachable via dashboard, SDK, CLI, and MCP. (Merged from three findings covering the same router.)
**Fix:** Gate every handler with `requirePerm(c, "smtpConfig", "read"|"create"|"update"|"delete"|"test")` mirroring `management.ts`; gate `POST /:id/tokens` behind `apiKey:create`, pass an explicit role, and never let a caller create a token whose role exceeds their own.

---

## High

### **Privilege escalation: any apiKey:create caller can mint an admin API key**
`packages/server/src/management.ts:251-269` · `packages/mail-queue/src/api-tokens.service.ts:45-66` · `packages/server/src/api-token-middleware.ts:35-37` · `packages/auth/src/permissions.ts:36-48` · `apps/cli/src/index.ts:110-113` · `apps/mcp/src/index.ts:124-128`
`POST /api/api-keys` accepts `role: z.enum(["admin","writer","reader"])`, gated only by `apiKey:create` (which `writer` holds), and persists the role verbatim with no check that it is `<=` the caller's role. The middleware then trusts the stored role, so a writer mints an admin token and gains member/org/invitation control it never had. Exposed via API, CLI (`--role admin`), and MCP. (Merged: two findings on the same `/api/api-keys` escalation. One verifier down-rated the token-clamp dimension to low, but the consensus and the reachable vertical escalation keep this High.)
**Fix:** Reject (403) any requested role ranking higher than `c.get("role")` before calling `createApiToken`.

### **Unsubscribe only flips a per-audience flag; address is never suppressed org-wide**
`packages/mail-queue/src/audiences.service.ts:116-123` · `packages/server/src/forms-public.ts:80-91` · `packages/server/src/management.ts:561-605`
`GET /u/:id` calls `unsubscribeContact(id)`, which sets `subscribed=false` on a single `contacts` row and renders "won't receive further emails" — but never adds the email to the org suppression list. The unique constraint is `(audienceId, email)`, so the same person in another audience keeps receiving broadcasts (the worker's suppression filter never blocks them because nothing was suppressed). CAN-SPAM/GDPR defect and a broken user-facing promise.
**Fix:** On unsubscribe, also `addSuppression(orgId, contact.email, "unsubscribe")` (and/or flip `subscribed=false` on all rows with that org+email) so `filterSuppressed` blocks them everywhere.

### **Double-send on retry: successful send followed by a failed log insert re-sends the email**
`packages/mail-queue/src/worker.ts:99-171` · `packages/mail-queue/src/queue.ts:19-39`
The worker calls `sendMail()` then `await db.insert(emailLogs)` in the same try block. If the send succeeds but the insert throws (DB blip/outage), the job throws and BullMQ (`attempts:3`) re-runs the entire handler, re-sending the email up to 3x. There is no idempotency key, jobId dedup, or "already sent" guard, and the log row uses a fresh UUID each attempt so no unique constraint catches it.
**Fix:** Make the side effect idempotent — pass a deterministic `jobId`, record a "sent" marker checked at handler top, and at minimum wrap post-send logging so a logging failure does not re-throw and re-trigger the send.

### **Stale SMTP transporter cache: password rotation breaks all sends**
`packages/mail-queue/src/smtp.ts:4-31` · `packages/mail-queue/src/smtp-configs.service.ts:147-190` · `packages/mail-queue/src/index.ts:13`
`getTransport()` caches transporters keyed only on `host:port:username` (password excluded), and `invalidateTransport()` is exported but never called anywhere. After a password-only rotation the long-lived worker keeps the cached transporter built with the OLD password; all sends for that config fail SMTP auth until the worker restarts. `/test` masks it by building a fresh uncached transporter, so admins see the test pass while production fails. (Merged: two findings on the same cache.) Note: the in-process `invalidateTransport` call cannot work cross-process (API vs worker) — the robust fix is the cache key.
**Fix:** Include a password fingerprint (hash, or `config.updatedAt`/`id`) in `getTransportKey` so a credential change yields a new cache entry; the worker already reloads config per job, so this self-heals.

### **Open redirect on public click-tracking endpoint /t/c/:id**
`packages/server/src/forms-public.ts:71-79`
The unauthenticated handler reads the `u` query param and 302-redirects to any `http(s)://` value, with no signature on the id, no binding of id→destination, and no allowlist. `GET /t/c/anything?u=https://evil.example` issues a redirect from the platform's trusted (API) origin — phishing/reputation abuse. (Merged: two identical findings. Verifiers split high/medium; kept High per the original ratings, noting it lives on the API origin so direct token theft is not demonstrated.)
**Fix:** HMAC-sign the destination in `injectTracking()` and verify on redirect (plus confirm the id maps to a real `emailLogs` row), or allowlist hosts.

### **Unauthenticated, unsigned unsubscribe (GET /u/:id) — auto-unsubscribe by link prefetch**
`packages/server/src/forms-public.ts:80-91` · `packages/mail-queue/src/audiences.service.ts:116-123` · `packages/server/src/management.ts:591`
A state-mutating write on an idempotent GET with no token, no confirmation step, and no org scoping (the raw `contacts.id` UUID is embedded in the email). Any mail scanner, antivirus URL-rewriter, or corporate gateway that fetches the link silently unsubscribes the recipient (RFC 8058 exists to prevent exactly this). `unsubscribeContact` also lacks an org filter, so a leaked/known UUID can be unsubscribed cross-tenant. (Both verifiers down-rated to medium because the UUID is not enumerable, but the GET-side-effect blast radius across all delivered broadcasts justifies High here.)
**Fix:** Require a signed token (HMAC over contact id + org), render a confirmation page on GET and unsubscribe only on POST (RFC 8058 one-click), and scope `unsubscribeContact` by the token's org.

### **Custom-HTML template rendering does not escape interpolated submitter data (HTML injection into delivered email)**
`packages/templates/src/blocks.ts:18-21,105-110` · `packages/templates/src/render-helpers.ts:28-36` · `packages/templates/src/index.ts:48-54` · `packages/server/src/forms-public.ts:210-218,236-243`
Both `interpolate` implementations substitute `{{field}}` verbatim with no HTML escaping. For a form bound to an HTML-mode template or a `customHtml` override, untrusted public submitter values are injected raw into the notification email (`<img onerror=...>`, markup, phishing links, hidden pixels). The built-in `buildFieldsHtml` path escapes correctly; this custom path bypasses it. (Verifiers note the `renderBlocks` button/text path *is* escaped — only the custom-HTML/`renderForm({customHtml})` paths are vulnerable — and email clients neutralize `<script>`, so impact is HTML/content injection, not true XSS; one verifier down-rated to medium on that basis.)
**Fix:** HTML-escape interpolated `data` values in the custom-HTML/`renderForm` interpolation; inject the already-escaped `_fields`/`_brand`/`_heading` via a separate raw mechanism.

---

## Medium

### **Broadcast over-reports sentCount: enqueue failures swallowed, counter still increments**
`packages/server/src/management.ts:575-609` · `packages/mail-queue/src/queue.ts:29-39` · `packages/mail-queue/src/audiences.service.ts:154-164` · `apps/web/.../audiences/[id]/audience-detail.tsx:91-101` · `apps/web/.../audiences/actions.ts:45-60`
`await enqueueEmail(...).catch(() => undefined); sent++;` — `sent++` runs unconditionally even when the enqueue rejects (Redis down). `completeBroadcast` persists the inflated count and marks status `sent`; the UI toasts "Broadcast queued to N contacts". A Redis outage reports 1000/1000 sent while zero were enqueued — silent data loss with a falsely successful contract. (Merged from four findings describing this exact loop.)
**Fix:** Increment only on success (`try { await enqueueEmail(...); sent++; } catch { failed++; }`), surface a `failedCount`, and don't mark `sent` when enqueues failed.

### **Webhook delivery has no SSRF protection**
`packages/mail-queue/src/webhooks.service.ts:102-111,142-150` · `packages/server/src/management.ts:369-381`
`createWebhook`/`pingWebhook` accept any `z.string().url()` and `fetch()` it server-side with no check against private/loopback/link-local/metadata ranges. `POST /webhooks/:id/ping` lets a user trigger the request and read the resulting `lastStatus`, enabling internal port/host probing (e.g. `http://169.254.169.254/...`, `http://localhost:5000/...`) on self-hosted deployments where the API sits on an internal network.
**Fix:** Validate URLs at create/update and delivery: require https, resolve the host and reject private/loopback/link-local/multicast/metadata ranges, pin the resolved IP against DNS rebinding, optionally allowlist.

### **Worker has no graceful shutdown: SIGTERM calls process.exit(0) mid-job**
`apps/api/src/worker.ts:9-12` · `packages/mail-queue/src/worker.ts:39-195`
The SIGTERM handler calls `process.exit(0)` without awaiting `worker.close()`, and the returned `Worker` handle is discarded; there is no SIGINT handler. With concurrency 10, up to 10 in-flight jobs are abandoned on every deploy/scale-down and left `active` for BullMQ stalled-recovery to re-process — which, combined with the non-idempotent handler, causes re-sends.
**Fix:** Keep the `Worker` reference; on SIGTERM/SIGINT `await worker.close()` then `closeRedis()`/exit.

### **No request body size / field count / field length limits on public form submit**
`packages/server/src/forms-public.ts:140-154` · `apps/api/src/server.ts:1-14` · `packages/mail-queue/src/forms.service.ts:146-157`
`POST /f/:token` parses the body with no `bodyLimit` middleware and copies every key/value into `data`, persisted verbatim to `formSubmissions.data` (jsonb). The per-IP rate limit runs *after* parsing and fails open. An unauthenticated client can POST multi-megabyte/thousands-of-field bodies → memory exhaustion and unbounded storage growth.
**Fix:** Add Hono `bodyLimit` to the public form route, cap field count and per-value length, reject oversized submissions with 413.

### **Stored HTML/JS injection into notification email via unescaped subject interpolation**
`packages/server/src/forms-public.ts:200-230` · `packages/templates/src/blocks.ts:105-110` · `packages/templates/src/render-helpers.ts:28-36`
A second instance of the unescaped-interpolation defect: the form `subject` (and the built-in `renderForm` customHtml path) interpolate submitter data with no escaping, enabling content spoofing in the delivered notification. (Closely related to the High custom-HTML finding; listed separately as it covers the subject line and the built-in customHtml branch.)
**Fix:** HTML-escape interpolated values when targeting HTML output; sanitize field values in the subject to prevent content spoofing.

### **Per-IP form rate limit trusts spoofable X-Forwarded-For and fails open**
`packages/server/src/forms-public.ts:160-177`
The only abuse control on `POST /f/:token` derives the client IP from `x-forwarded-for.split(",")[0]` with no trusted-proxy validation, so rotating the header gives a fresh key per request and never hits `maxPerHour`. Any Redis error is caught and ignored, fully disabling the limit during incidents. This is the sole throttle protecting storage, webhook dispatch, notification sends, and the auto-responder.
**Fix:** Derive the client IP from trusted-proxy config / the socket address; fail closed (or to a conservative global limit) when the rate-limit backend is unavailable.

### **Open-relay / spam amplification via form auto-responder to attacker-chosen address**
`packages/server/src/forms-public.ts:261-290,160-177`
When `settings.autoRespond` is enabled, `POST /f/:token` sends a confirmation email to `data[form.replyToField || "email"]` — an unauthenticated, attacker-controlled address — from the org's verified SMTP, throttled only by the spoofable per-IP limit. An attacker rotates victim addresses to burn the tenant's sending reputation/quota. (Verifiers down-rated to medium: the body is a fixed org template with escaped fields, so it's recipient-injection/reputation burn rather than arbitrary-content relay, and `autoRespond` is opt-in.)
**Fix:** Auto-respond only to addresses verified via double opt-in, cap auto-responder volume per form per day independent of IP, add per-recipient dedupe, require CAPTCHA/honeypot proof, and never key the only abuse control on a client-supplied header.

### **Audit log coverage is incomplete: destructive/security-sensitive actions not recorded**
`packages/server/src/management.ts:56-65,270-275,354-359,412-417` · `packages/mail-queue/src/audit.service.ts:15-24`
`audit()` is called only for domain/webhook/audience create and broadcast send. No entry is written for API key create/revoke (the privilege-escalation vector), any deletion, webhook/form/template updates, or SMTP config changes. `recordAudit` also swallows all DB errors and runs as `void`, so even recorded events can silently fail to persist.
**Fix:** Call `audit()` for all create/update/delete/revoke handlers (especially API-key create/revoke and all deletes); await or log failures for security-critical events.

### **Broadcast reports contacts as 'sent' even when recipient is suppressed**
`packages/server/src/management.ts:561-609` · `packages/mail-queue/src/worker.ts:62-88`
`totalCount`/`sentCount` are computed from the raw subscribed list with no suppression filtering; the worker later drops suppressed/bounced addresses (logged `suppressed`) but they were already counted. Stats overstate reach. (Related to the sentCount-inflation finding; this is the suppression dimension of the same reporting gap.)
**Fix:** Run `filterSuppressed()` over the recipient list before computing `totalCount` and before enqueueing, counting/enqueuing only the allowed set.

### **Open redirect via unsanitized `redirect` query param on login/sign-up**
`apps/web/app/(auth)/login/page.tsx:23-37` · `apps/web/app/(auth)/sign-up/page.tsx:24-37` · `packages/auth/src/auth.ts:16`
Both auth pages read `redirect` from the URL with no validation and use it as both Better Auth `callbackURL` and `router.push(redirectTo)`. `router.push` navigates to absolute external URLs, so a freshly-authenticated victim is sent off-site (`/login?redirect=https://evil.example`). `trustedOrigins:["*"]` removes the server-side constraint on `callbackURL`. The invite flow generates such links, normalizing trust in the param. (Merged: two findings on the same sink.)
**Fix:** Accept only relative, same-origin paths (`raw.startsWith("/") && !raw.startsWith("//")`, else `/dashboard`) for both `callbackURL` and `router.push`; tighten `trustedOrigins`.

### **apiGet swallows all non-200 responses to null → backend errors render as empty/'no data'**
`apps/web/lib/api.ts:26-34` · dashboard/logs/audiences/forms/domains/webhooks/suppressions/analytics pages
`apiGet` returns `null` on any non-2xx, thrown fetch, or unparseable JSON, undistinguished. Pages coalesce that to `?? []` / zeroed totals and render the normal empty state, so a real partial backend failure (data endpoint 500s while the session endpoint succeeds) shows "No X yet / 0 emails / 0% delivery" to a user who has data, with no error or retry. (Whole-API/session failure is mitigated by the layout's `getSession` redirect; the partial-failure case across 8+ pages is the live one.)
**Fix:** Have `apiGet` distinguish error classes (discriminated result or throw on 5xx/network), redirect to `/login` only on 401, and stop coalescing genuine failures into empty arrays.

### **Member role change / removal / invite cancellation silently swallow API errors**
`apps/web/app/(dashboard)/dashboard/organization/members-manager.tsx:74-94`
`changeRole`, `remove`, and `cancelInvite` call the Better Auth client methods and immediately `router.refresh()` without inspecting the returned `{ data, error }` (these methods resolve with an error field, they don't throw). On a rejection (last-admin protection, permission denied, 5xx) nothing is shown and the table refreshes as if it succeeded. The sibling `invite()` checks `res.error`, proving the omission. (One verifier down-rated to low; kept medium for the silent-failure of destructive member ops.)
**Fix:** Capture each result and `if (res.error) setMessage(...); return;` before `router.refresh()`, mirroring `invite()`.

### **Organization create/update/delete/switch swallow errors and falsely report success**
`apps/web/components/org-switcher.tsx:36-39,41-55` · `apps/web/app/(dashboard)/dashboard/organization/org-settings.tsx:35-43,45-57`
`create()`, `switchTo()`, `save()`, and `remove()` never check the returned `error` object; the dialog closes / input clears / page refreshes (and `remove()` navigates to `/dashboard`) regardless of whether the operation succeeded. A failed org rename/delete looks successful — `remove()` claims a delete that didn't happen.
**Fix:** Branch on `res.error` for each `authClient.organization.*` call: surface it and skip the success path (setActive/close/clear/navigate/refresh).

### **Form editor can save a form with zero recipients, breaking delivery**
`apps/web/app/(dashboard)/dashboard/forms/[id]/form-editor.tsx:109-146` · `packages/server/src/management.ts:138`
The create path enforces `recipients.min(1)` and defaults to the session email; the edit path filters blanks to `[]` and the PATCH schema is `z.array(...).optional()` with no `.min(1)`, so clearing the field persists `recipients: []`. The form then has nowhere to deliver submissions and silently stops notifying anyone (`to: []` send fails, logged `failed`, no UI warning).
**Fix:** Guard in `save()` (toast and return if empty) and tighten the PATCH schema to `z.array(z.string().email()).min(1).optional()`.

### **Broadcast sentCount inflated — failed enqueues reported to the user as sent**
`packages/server/src/management.ts:575-609` · `apps/web/.../audiences/[id]/audience-detail.tsx:91-101` · `apps/web/.../audiences/actions.ts:45-60`
The user-facing dimension of the enqueue-swallow bug: the action returns `res.data?.sentCount` and the UI toasts "Broadcast queued to N contacts" using the inflated count, so the operator believes a broadcast went out when zero were enqueued. (Same root cause as the sentCount findings above; retained for the UI/contract surface.)
**Fix:** Same as the broadcast sentCount fix — increment only on resolve and surface partial-failure status/count to the response and UI.

### **`MAIL_ENCRYPTION_KEY` silently falls back to `BETTER_AUTH_SECRET`; later setting it makes all secrets undecryptable**
`packages/mail-queue/src/crypto.ts:7-16` · `.env.example:16-18` · `docker-compose.yml:39-48`
`getKey()` derives the AES key from `MAIL_ENCRYPTION_KEY || BETTER_AUTH_SECRET`; `.env.example` ships the key empty and documents the fallback. If an operator runs with it empty, stores SMTP passwords/DKIM keys, then later sets `MAIL_ENCRYPTION_KEY` (or rotates `BETTER_AUTH_SECRET`), every stored secret becomes permanently undecryptable (auth-tag errors) with no migration path — no per-record key/salt is stored.
**Fix:** Require `MAIL_ENCRYPTION_KEY` explicitly (fail fast if unset) or persist the key id/salt per record; at minimum ship a real key with a loud "never change after first use" warning.

### **docker-compose CMD runs the app even when migration fails (`;` instead of `&&`)**
`Dockerfile.api:36`
The entrypoint joins migrate and start with `;`, so a non-zero `migrate.ts` exit (`process.exit(1)` on any non-ignored error) is discarded and `start:all` runs anyway against a partial/inconsistent schema. Because the long-lived server keeps the container "up", `restart: unless-stopped` never retries, masking deploy-time migration failures. (Verifier down-rated high→medium.)
**Fix:** Use `&&` between migrate and start so a failed migration aborts startup and triggers Docker's restart policy.

### **`concurrently` start:all has no kill-on-fail: a crashed worker/API is never restarted**
`apps/api/package.json:9` · `Dockerfile.api:36` · `apps/api/src/worker.ts:6`
API and worker run as two children of one `concurrently` parent (PID 1) with no `--kill-others-on-fail`. If one child dies (e.g. an unhandled BullMQ `error` event — `startWorker` registers no `error` listener), the other keeps running and PID 1 stays alive, so `restart: unless-stopped` never fires. Result: emails silently stop with the container reporting healthy. (Verifier down-rated high→medium: needs a process crash to trigger.)
**Fix:** Run API and worker as separate compose services each with a restart policy, or add `--kill-others-on-fail`; add a `/health` healthcheck to the api service.

### **docker-compose publishes web/api on random host ports**
`docker-compose.yml:51-52,80-81`
Both services use short-form single-port syntax (`ports: - 3000` / `- 5000`), which maps the container port to a *random* ephemeral host port. There is no reverse proxy and no documentation, while `BETTER_AUTH_URL`/`WEB_ORIGIN` are pinned to `http://localhost:3000` — which won't match the random port, also breaking auth callback/CORS. The shipped self-host stack is unusable on first run. (Verifier down-rated high→medium.)
**Fix:** Use explicit `"3000:3000"` for web; drop the public `api` mapping (keep only `expose: 5000` since web proxies it internally).

### **forms.templateId / broadcasts.templateId have no FK to email_templates — dangling refs on delete**
`packages/db/src/schemas/forms-schema.ts:48` · `packages/db/src/schemas/audiences-schema.ts:63` · `packages/db/migrations/0008_form_custom_template.sql:2` · `packages/db/migrations/0006_audiences.sql:29` · `packages/mail-queue/src/templates.service.ts:109`
Both columns are bare `uuid` with no `.references()`/`onDelete`, and `deleteTemplate` hard-deletes with no referencing check. Deleting a template leaves forms/broadcasts pointing at a nonexistent id. (Verifier down-rated medium→low: forms null-guard `getTemplate` and fall back to the built-in design; broadcasts are rendered at send time and never re-read `templateId`, so impact is graceful degradation, not a crash.)
**Fix:** Add `.references(() => emailTemplates.id, { onDelete: "set null" })` to both columns and a matching `ALTER TABLE ... ADD CONSTRAINT ... ON DELETE SET NULL` migration. (needs confirmation — severity uncertain between medium/low)

---

## Low

### **Privilege delegation: token role not bounded by creator role (CLI/MCP)**
`packages/server/src/management.ts:251-269` · `packages/auth/src/permissions.ts:39` · `apps/mcp/src/index.ts:124-128` · `apps/cli/src/index.ts:110-113`
A second framing of the api-keys escalation focused on the missing role clamp itself; `createApiToken` stores any requested role with no `<=` caller check, reachable via REST/CLI/MCP. (Verifier corrected severity high→low because REST resources grant writer and admin identical permissions *today*, though admin-only org/member statements make it latent.) Folded into the High `/api/api-keys` finding above; retained here per the verifier's low rating.
**Fix:** Reject any requested role ranking higher than the caller's role. (needs confirmation)

### **Personal-org auto-creation race creates duplicate workspaces**
`packages/server/src/org-context.ts:19-53` · `packages/server/src/index.ts:79-100`
`ensurePersonalOrg` is a non-atomic check-then-insert with no transaction, lock, or unique constraint on `member.userId`. Two concurrent `/api/me` calls for a brand-new user both see no membership and each create an org+admin membership; `resolveSessionOrg` then picks an arbitrary `first`, splitting data across duplicate workspaces. (Verifier down-rated medium→low: narrow window, self-inflicted, recoverable.)
**Fix:** Take a pg advisory lock keyed on userId and re-check inside the lock, or add a partial unique index + `ON CONFLICT` fall-back to selecting the existing membership.

### **Race in createSmtpConfig default-config logic can produce two (or zero) defaults**
`packages/mail-queue/src/smtp-configs.service.ts:116-145`
Non-atomic read-then-write: `isDefault = existing.length === 0` then insert, with no transaction or partial unique index. Two concurrent creates on an empty org both insert `isDefault=true`; `getDefaultSmtpConfig` then `.limit(1)` picks one non-deterministically (affecting which credentials/from-address default sends use). `updateSmtpConfig`'s default switch has the same shape. (Verifier kept at medium; placed in Low alongside the related DB-constraint finding below for grouping.)
**Fix:** Add a partial unique index `UNIQUE(organization_id) WHERE is_default` and wrap the clear-then-set in a single transaction. (needs confirmation — verifier rated medium, listed low for dedup grouping)

### **No DB constraint enforcing a single default SMTP config per org; clear-then-set is non-transactional**
`packages/db/src/schemas/mail-schema.ts:30` · `packages/mail-queue/src/smtp-configs.service.ts:120,165`
The DB-layer view of the default-config race: `isDefault` is a plain boolean with no partial unique index, so "exactly one default per org" is enforced purely in racy app logic, and the two-statement clear-then-set update is not transactional (a crash between them leaves zero defaults; `getDefaultSmtpConfig` then falls back to most-recent). (Same root cause as the two findings above — merged.)
**Fix:** Partial unique index `UNIQUE(organization_id) WHERE is_default` plus `db.transaction()` around clear-then-set.

### **updateSmtpConfig default-switch: two un-transacted UPDATEs, partial failure leaves no default**
`packages/mail-queue/src/smtp-configs.service.ts:165-187`
When `isDefault:true`, the clear-all-defaults UPDATE and the set-target UPDATE are separate auto-committed statements; a crash between them leaves the org with zero defaults. Self-healing via `getDefaultSmtpConfig`'s most-recent fallback, but the user-selected default is silently lost. (Same clear-then-set issue; retained as the explicit transaction-boundary fix.)
**Fix:** Wrap both UPDATEs in a single `db.transaction()`.

### **Mixed credentials (session cookie + lm_ token) cause cross-org audit attribution**
`packages/server/src/api-token-middleware.ts:35-39` · `packages/server/src/auth-middleware.ts:20-31` · `packages/server/src/management.ts:56-65`
A request carrying both a session cookie and an `lm_` token gets `organizationId`/`role` from the token but `user`/`session` from the cookie, so `audit()` stamps the cookie user's identity onto an action in the token's org. Not an authorization issue (authz uses the overridden org/role), only audit-log integrity.
**Fix:** When the api-token authenticates, clear `user`/`session` (or reject when both are present) so the context is unambiguously an API-token context.

### **`trustedOrigins: ["*"]` disables Better Auth origin/CSRF validation**
`packages/auth/src/auth.ts:16`
Wildcard trusted origins turns off origin/Referer (CSRF) checks and `callbackURL` validation on `/api/auth/*`. (Both verifiers down-rated medium→low: the app uses Bearer tokens with `credentials:"omit"`, and the residual session cookie is SameSite=Lax, so there is no realistic CSRF/ATO exploit — the genuine residual risk is the unrestricted `callbackURL` defeating the open-redirect guard, plus loss of a defense-in-depth layer.) (Merged: three findings on the same line.)
**Fix:** Set `trustedOrigins` to the explicit public origin(s) derived from `WEB_ORIGIN`/`BETTER_AUTH_URL`.

### **removeContact scoped to org but not to the audience in the path (cross-audience deletion within org)**
`packages/server/src/management.ts:514-522` · `packages/mail-queue/src/audiences.service.ts:105-114`
`DELETE /api/audiences/:id/contacts/:contactId` deletes by `(id, organizationId)` and never validates the `:id` audience segment against the contact's `audienceId`, so a caller can delete any org contact via any audience URL. Not cross-tenant (org scoping holds); the path id is effectively meaningless. The sibling POST route does validate the audience, confirming the inconsistency.
**Fix:** Validate the audience (`getAudience(id, orgId)`) and add `eq(contacts.audienceId, audienceId)` to the delete `where`.

### **Broadcast totalCount counts suppressed/never-delivered recipients**
`packages/server/src/management.ts:561-569` · `packages/mail-queue/src/audiences.service.ts:96-103` · `packages/mail-queue/src/worker.ts:62-88`
`totalCount = getSubscribedContacts().length` does not exclude org-suppressed addresses, which the worker later drops, so totals overstate reach (compounding the sentCount inflation). Reporting inaccuracy only.
**Fix:** Pre-filter recipients with `filterSuppressed(orgId, emails)` before computing `totalCount` and enqueueing.

### **Broadcasts never check the suppression list at enqueue time (totals overstate reach)**
`packages/server/src/management.ts:561-605` · `packages/mail-queue/src/audiences.service.ts:96-103`
Duplicate of the above totalCount finding from a different audit dimension: suppression is enforced only in the worker, so totals count addresses the worker will drop. Functionally deliverability is still protected — a reporting/contract inaccuracy. (Merged with the totalCount finding.)
**Fix:** Same — `filterSuppressed()` in the broadcast handler before computing totals and enqueueing.

### **Scheduled-send response claims status 'scheduled' for a past sendAt (sent immediately)**
`packages/server/src/mail.ts:128-137` · `packages/mail-queue/src/queue.ts:33-38`
A past `sendAt` yields `delay 0` (sent immediately, correct) but the response reports `status:"scheduled"` with a past `scheduledAt`. The OpenAPI 201 schema also omits `scheduledAt` and the `403` response and never documents the `scheduled` status. Contract/cosmetic only.
**Fix:** Base reported status on whether a delay was actually applied; set `scheduledAt` only when genuinely delayed; update the OpenAPI responses.

### **replyTo dropped on the public /api/send path**
`packages/mail-queue/src/types.ts:20-29` · `packages/server/src/mail.ts:112-126` · `packages/mail-queue/src/queue.ts:4-16`
The transport layer, worker, and `forms-public.ts` all support `replyTo`, but `sendEmailSchema` omits it and the `/api/send` handler never forwards it, so API/SDK/CLI/MCP callers cannot set Reply-To. Capability gap.
**Fix:** Add `replyTo: z.string().email().optional()` to `sendEmailSchema` and pass it through to `enqueueEmail`.

### **OpenAPI SmtpConfig schema declares userId required non-null UUID, but it's always null**
`packages/server/src/smtp-configs.ts:31,133-140` · `packages/mail-queue/src/smtp-configs.service.ts:116-145`
The published schema declares `userId` as a required UUID, but `createSmtpConfig` always sets `userId: null` (no caller supplies `createdByUserId`), and the column is a nullable `text` user id, not a uuid. Contract mismatch; doc-only (no runtime validation). (Verifier noted the live schema is the inline component in `index.ts:136-151`, with the same mismatch; the cited `smtpConfigSchema` is dead code.)
**Fix:** Either populate `userId` (`c.get("user")?.id`) and mark the schema nullable, or change the schema to `userId: z.string().nullable()` (and correct the `uuid` format) to match reality.

### **Unauthenticated open/click counters allow stats poisoning**
`packages/server/src/forms-public.ts:62-79` · `packages/mail-queue/src/tracking.service.ts:26-48`
`GET /t/o/:id` and `/t/c/:id` increment `opens`/`clicks` for any id with no auth, signature, org scoping, or `(id,ip)` dedup, and are not idempotent. Anyone who receives (or replays) a tracking id can inflate counts, corrupting `openRate`/`clickRate`. Integrity-of-feature, not confidentiality.
**Fix:** HMAC-sign tracking ids so only system-minted ids are accepted, and dedupe increments per recipient/IP within a window.

### **migrate.ts swallows DDL errors at file granularity, skipping the rest of a multi-statement file**
`packages/db/src/migrate.ts:38,42,47`
Files are split on `--> statement-breakpoint`, but no migration contains that marker, so each whole file runs as one query and the try/catch operates per-file. The `IGNORED_CODES` design intends per-statement "skip if exists" semantics; the granularity is wrong. Currently latent (all statements are idempotent), and on a multi-statement simple-query failure Postgres rolls back the whole file rather than partially applying it. (Verifier down-rated medium→low: no current trigger, failure mechanism partly inaccurate.)
**Fix:** Emit `--> statement-breakpoint` between statements so each is handled individually, or drop `IGNORED_CODES` and rely on `IF NOT EXISTS` guards + a migrations-applied tracking table.

### **Three divergent migration mechanisms; db:migrate is a silent no-op + drizzle out dir mismatch**
`packages/db/drizzle.config.ts:5` · `packages/db/package.json:11-12` · `packages/db/src/migrate.ts:27-31` · `README.md:38,92`
`drizzle.config.ts` `out: "./drizzle/migrations"` (a non-existent dir) while the runtime migrator reads `packages/db/migrations/`. `db:generate` writes to the dir production never reads, and the README-documented `db:migrate` applies zero migrations and exits successfully. There is no migration-tracking table, so `migrate.ts` re-runs every file each boot. Latent schema-drift footgun; production's hand-written path works today. (Two findings merged; both verifiers down-rated high→low.)
**Fix:** Pick one mechanism. Either set `out: "./migrations"` and commit to drizzle-kit (with a journal/tracking table), or remove the dead `db:generate`/`db:migrate` scripts and document the hand-written path production actually uses.

### **`smtp test` throws instead of returning {success:false} on a failed connection**
`packages/sdk/src/index.ts:111-116` · `packages/server/src/smtp-configs.ts:266-275` · `apps/cli/src/index.ts:101-103` · `apps/mcp/src/index.ts:109-114`
The server returns HTTP 422 `{success:false,error}` on a failed test, but the SDK treats any non-2xx as a thrown `LaunchMailError`, so its documented `{success:boolean}` contract never resolves to `{success:false}` — the CLI prints the error and exits 1; MCP returns `isError`. The common case the command exists for is surfaced as a transport failure.
**Fix:** Return 200 with `{success:false,error}` for a reachable-but-failed test (reserve non-2xx for not-found/unauthorized), or special-case 422 in the SDK to parse and return the body.

### **Template tests don't cover the unescaped custom-HTML rendering paths**
`packages/templates/test/render.test.ts:25-32,52-61`
The escaping test only exercises the safe built-in layout, and the custom-HTML test merely asserts values appear (never that markup is neutralized), so the HTML-injection defect ships green. (`renderBlocks` is actually escaped; the gap is `renderCustomHtml` and `renderForm({customHtml})`.)
**Fix:** Add tests passing `<script>`/`<img onerror>` through `renderCustomHtml` and `renderForm({customHtml})`, asserting raw `<script` is absent and `&lt;` encoding present (after the escaping fix).

### **requireOrg redirects an authenticated user to /login on any transient /api/me failure**
`apps/web/lib/org.ts:22-41` · `apps/web/lib/api.ts:26-34`
Because `apiGet` returns `null` on any non-200, a 500 from `/api/me` (e.g. `ensurePersonalOrg`/DB hiccup) makes `getOrgContext` null and `requireOrg` redirect to `/login`, bouncing a fully-authenticated user and potentially producing a login loop while the backend is unhealthy.
**Fix:** Redirect to `/login` only on 401/403; on 5xx/network errors render an error state or rethrow to an error boundary.

### **auth-client reads `NEXT_PUBLIC_URL`, an env var defined nowhere**
`apps/web/lib/auth-client.ts:7` · `turbo.json:4`
`baseURL = process.env.NEXT_PUBLIC_URL` is always `undefined` (not in any `.env*`, compose, or `turbo.json` globalEnv — which lists the differently-named `NEXT_PUBLIC_API_URL`); Better Auth then falls back to the current origin, which happens to be the intended behavior. Dead/misleading code that works by accident.
**Fix:** Drop the `baseURL`/`API_URL` indirection and let Better Auth default to the current origin. (Note: do *not* point it at `NEXT_PUBLIC_API_URL` — that would break the intentional same-origin proxy.)

### **Copy-to-clipboard in domain DNS records reports success even when the write fails**
`apps/web/app/(dashboard)/dashboard/domains/[id]/domain-detail.tsx:37-43`
`navigator.clipboard.writeText(text)` is fire-and-forget then `setCopied(true)` unconditionally; if the write rejects (permission denied / not focused) the user sees the "copied" check while nothing was copied, and the rejection is unhandled. (Verifier note: the "plain HTTP" trigger actually throws synchronously and skips the check; the real path is a rejected promise in a secure context.)
**Fix:** `await` the write inside try/catch; `setCopied(true)` only on success, toast on failure.

### **Test-send success message can render 'Message ID: undefined'**
`apps/web/app/(dashboard)/dashboard/smtp/[id]/test-send.tsx:22-24` · `apps/web/app/(dashboard)/dashboard/smtp/[id]/actions.ts:44-52`
`messageId` is optional but the banner renders `Sent! Message ID: ${result.messageId}` unconditionally, producing the literal "undefined" when absent. Cosmetic; rarely fires with nodemailer SMTP (which populates messageId).
**Fix:** `setMessage(result.messageId ? \`Sent! Message ID: ${result.messageId}\` : "Sent!")`.