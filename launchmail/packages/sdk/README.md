# @danilaanikin/launchmail

Typed, **zero-dependency** client for the self-hosted [LaunchMail](../../README.md)
API — send transactional email, pull incoming replies, verify webhooks. Runs on
Node 20+, Vercel/Edge, Bun and browsers (uses `fetch` + Web Crypto only).

## Install

Every project that today copies `leadcrm/src/lib/launchmail.ts` should use this
instead. Two ways:

**A) As a package (GitHub Packages)** — pin a version, get updates:
```
# .npmrc in the consuming project
@danilaanikin:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}   # token with read:packages
```
```bash
pnpm add @danilaanikin/launchmail
```

**B) Vendor the single file** — zero setup, no registry auth:
copy `src/index.ts` into your project as e.g. `src/lib/launchmail.ts`. It has no
runtime dependencies, so it just works.

> Publishing (maintainer): `pnpm --filter @danilaanikin/launchmail build && \
> pnpm --filter @danilaanikin/launchmail publish --no-git-checks` with a
> `GITHUB_TOKEN` that has `write:packages`. `publishConfig` swaps the `dist`
> paths in at pack time; **internal workspace resolution stays at `src/` and
> needs no build** (dev is unchanged). Not published automatically.

## Quick start

```ts
import { LaunchMailClient } from "@danilaanikin/launchmail";

const lm = new LaunchMailClient({
  baseUrl: process.env.LAUNCHMAIL_URL!,     // bare origin, no /api (e.g. https://mail.freio.cz)
  apiKey:  process.env.LAUNCHMAIL_API_KEY!, // "lm_..." token from the dashboard
});
```

### Send

```ts
await lm.sendEmail({
  to: [{ email: "user@example.com", name: "User" }],
  subject: "Hello",
  html: "<p>Hi</p>",
  // clientReference? — caller UUID echoed by every terminal webhook
  // clientType? — one of the exported LAUNCHMAIL_CLIENT_TYPES registry values
  // from?    — falls back to the token's bound SMTP config
  // replyTo? — reply address
  // sendAt?  — UTC ISO ending in "Z" (e.g. "2026-07-20T10:00:00Z"); future = scheduled
});
// → { id, status: "queued" | "scheduled", smtpConfigId, scheduledAt, createdAt }
```

⚠️ `sendAt` must be **UTC ending in `Z`** — the server rejects offsets like
`+02:00` with HTTP 400.

### Pull incoming replies

```ts
const inbox = await lm.listIncomingEmails({ limit: 50 });
// options: { limit, folder: "inbox"|"archived"|"starred"|"all", smtpConfigId, q, before }
```

### Verify webhooks

```ts
import { verifyWebhookSignature } from "@danilaanikin/launchmail";

const raw = await req.text(); // the RAW body — do not re-stringify
const ok = await verifyWebhookSignature(
  raw,
  req.headers.get("x-launchmail-signature"),
  process.env.LAUNCHMAIL_WEBHOOK_SECRET!, // full "whsec_..." value
);
if (!ok) return new Response("invalid signature", { status: 401 });
const { event, data } = JSON.parse(raw);
// events: email.sent | email.failed | email.bounced | email.suppressed | form.submission | incoming.received | ping
```

## Auth

Bearer `lm_…` API token (create in the dashboard / `POST /api/api-keys`).
`baseUrl` is the origin only; the client appends `/api` to every request.

## Errors

Any non-2xx response throws `LaunchMailError` (`.status`, `.message`). The
SMTP-connection test's `422` is returned (not thrown) so callers can read
`{ success: false, error }`.

## API surface

`LaunchMailClient`: `sendEmail`, `listIncomingEmails`, `whoami`, `listLogs`,
`listSmtpConfigs` / `createSmtpConfig` / `testSmtpConnection` / `deleteSmtpConfig`,
`listApiKeys` / `createApiKey` / `revokeApiKey`,
`listForms` / `createForm` / `getForm` / `deleteForm` / `listSubmissions`.

Helpers & types: `verifyWebhookSignature`, `SendEmailInput`, `SendEmailResult`,
`IncomingEmailSummary`, `ListIncomingEmailsOptions`, `LaunchMailWebhookPayload<T>`,
`EmailSentData` / `EmailFailedData` / `IncomingReceivedData`, `LaunchMailError`.
