# Direct Delivery Plan — launchmail as a full ESP ("our own Resend")

Goal: launchmail delivers mail **itself, directly to recipient MX servers** —
no third-party sending provider (Resend/Brevo/SES) in the pipeline. The only
rented ingredient is a clean egress IP with PTR control (even Resend rents its
IPs from cloud providers; owning the pipe = renting an IP you control, not
renting someone's API).

## Ground truth (what software cannot change)

Receiving providers enforce, regardless of our stack:

| Requirement | Gmail / Outlook / Seznam policy |
|---|---|
| **PTR (FCrDNS)** on sending IP | hard requirement — no PTR → `550 5.7.25` reject |
| SPF **and** DKIM aligned | required since 2024 sender rules; DMARC for bulk |
| IP reputation / warm-up | new IPs get throttled/junked; ramp volume gradually |
| Port 25 egress | must be open from the sending host |

Measured on the current home deployment (2026-07-11, IP `185.120.71.202`):
- outbound port 25: **OPEN** (unusual for a residential line — good)
- PTR: **NONE** → direct sends from home would be hard-rejected by Gmail
- PTR is ISP-controlled; a home line cannot satisfy FCrDNS → the **egress hop
  must run on an IP where we set PTR** (tiny VPS; see Phase 5). Everything
  else — queue, composer, DKIM, tracking, bounces, UI — runs on our homelab.

## What already exists (reuse, don't rebuild)

| Capability | Where |
|---|---|
| Queue + worker, retries (3× exp. backoff) | `packages/mail-queue/src/queue.ts`, `worker.ts`, BullMQ/Redis |
| Smarthost transport + pooled transporters | `packages/mail-queue/src/smtp.ts` |
| SMTP configs, encrypted at rest, per-org, default flag | `smtp-configs.service.ts`, `crypto.ts` |
| DKIM signing input (`dkim?: {domainName, keySelector, privateKey}`) | `smtp.ts` `SendMailInput`; nodemailer signs natively |
| Suppression lists | `suppressions.service.ts`, `suppressions-schema.ts` |
| Inbound mail via IMAP + IDLE | `imap.service.ts`, `inbox-idle.ts`, `inbox.service.ts` |
| Domains, templates, tracking, webhooks, email logs, API tokens | respective services |

## Target architecture

```
apps / SDK / forms
      │
      ▼
API ──► BullMQ (Redis) ──► WORKER
                             │  per SmtpConfig.type:
              ┌──────────────┴──────────────┐
              ▼                             ▼
      type: "smarthost"             type: "direct"          ← NEW
      (today's path,                MX resolve → SMTP :25
       kept as fallback)            STARTTLS, DKIM-signed,
                                    per-domain rate limits
                                            │
                                    runs on DELIVERY NODE
                                    (VPS w/ PTR; Phase 5) —
                                    same worker image, role
                                    flag, queue over Tailscale
```

Key design decision: **the delivery node is just another worker instance**
consuming the same Redis queue (over Tailscale) with `WORKER_ROLE=direct`.
No new protocol, no relay daemon to write — the existing worker app moves to
where the clean IP is. Core (API, web, Postgres, Redis, tracking) stays home.

---

## Phase 1 — Transport abstraction + direct-MX MVP (~2–4 days)

- [ ] Schema: `smtpConfigs.type: "smarthost" | "direct"` (default `smarthost`,
      migration) + `heloHostname` (must match the egress PTR), make
      `host/port/username/password` nullable for `direct`.
- [ ] New `packages/mail-queue/src/direct-transport.ts`:
  - `dns/promises.resolveMx()` per recipient domain, sort by priority,
    try hosts in order; happy-eyeballs not needed (v4 first, fallback v6)
  - deliver via nodemailer's `SMTPConnection` (or `SMTPTransport` with
    `host=<mx>`, `port=25`, `secure=false`, `opportunisticTLS`), EHLO =
    `heloHostname`, envelope-from (return-path) on the sending domain
  - **require DKIM** for direct sends (config validation — unsigned direct
    mail is DOA in 2026)
  - classify SMTP replies: 2xx → sent; 4xx → retryable (throw retryable);
    5xx → permanent (no retry, log response, feed Phase 2 suppression)
  - group recipients by domain; one connection per domain per job
- [ ] `smtp.ts` `sendMail()` branches on `config.type` — call sites unchanged.
- [ ] Web UI: SMTP config form gets type toggle; `direct` hides credentials,
      shows `heloHostname` + "egress IP must have matching PTR" hint.
- [ ] Acceptance: from a clean-IP host, send to Gmail+Seznam test accounts →
      inbox, headers show `spf=pass dkim=pass`, `Received:` shows our HELO/IP.

## Phase 2 — Queue hardening for direct mode (~1–2 days)

- [ ] Per-recipient-domain rate limiter (Redis token bucket in worker;
      config: default N/min + per-domain overrides for gmail.com etc.).
- [ ] Direct-mode retry schedule replacing the flat 3×: e.g.
      1m → 5m → 15m → 1h → 4h → 8h → … cap 24–48 h (greylisting-friendly),
      then permanent-fail. (BullMQ custom backoff strategy.)
- [ ] Permanent 5xx → auto-insert into suppressions + webhook event + email
      log status (`delivered | deferred | bounced` + last SMTP response).
- [ ] Acceptance: forced 4xx (greylisting sim) retries on schedule; forced
      5xx suppresses recipient and fires webhook.

## Phase 3 — Bounce ingestion (DSN) via existing IMAP path (~2–3 days)

Async bounces (accepted then bounced later) arrive at the **return-path
mailbox**. We already sync mailboxes via IMAP — reuse it:

- [ ] Point `Return-Path`/`MAIL FROM` at `bounces@<sending-domain>`, mailbox
      IMAP-synced by the existing `imap.service.ts` machinery.
- [ ] VERP: `bounces+<messageId>@…` so each DSN maps to the exact message
      without parsing the (often mangled) original.
- [ ] DSN/ARF parser job: detect `multipart/report`, extract status +
      recipient (`mailparser` is already in the stack via inbox) →
      suppression + email-log update + webhook.
- [ ] Acceptance: send to a nonexistent Gmail address → async DSN parsed,
      recipient suppressed automatically, event visible in UI.
- [ ] *Later (optional):* lightweight inbound SMTP listener on the delivery
      node for bounce-only MX, removing the IMAP dependency.

## Phase 4 — Deliverability console (~1–2 days)

- [ ] Domain health check in UI (extends `domains.service.ts`): SPF record
      contains egress IP? DKIM DNS TXT matches stored key? DMARC present?
      **PTR/FCrDNS of egress IP resolves and matches heloHostname?**
      Live port-25 probe from the delivery node.
- [ ] Warm-up scheduler: per-domain daily send cap with ramp
      (20 → 50 → 100 → 250 → …), enforced in the rate limiter; UI progress.
- [ ] One-click copy of the full DNS set per sending domain.

## Phase 5 — Egress delivery node (ops, ~0.5–1 day)

- [ ] Tiny VPS: Hetzner CAX11/CX22 (~€4/mo). Set **PTR** in console →
      `mail.<domain>`. Request **port 25 unlock** (Hetzner support ticket;
      blocked by default on new accounts — plan for a short delay).
- [ ] Join node to Tailscale → runs `worker` container with
      `WORKER_ROLE=direct`, `REDIS_URL` over tailnet to homelab.
- [ ] DNS per sending domain: SPF `ip4:<node-ip>`, DKIM key, DMARC
      (`p=none` → `quarantine` after warm-up), PTR done above.
      Optional: MTA-STS + TLS-RPT.
- [ ] Acceptance: mail-tester.com ≥ 9/10 through our own pipe; then ramp
      volume via Phase 4 warm-up and **retire the Resend smarthost config**.

## Non-goals (for now)

Dedicated IP pools / multi-node scheduling, ARC sealing, full inbound MX
hosting (IMAP path covers receiving), DMARC aggregate-report analytics UI.

## Rollout strategy (homelab)

1. **Day D (server arrival): zero code.** Run launchmail with one `smarthost`
   SmtpConfig (Resend free tier) — everything ships mail from day one.
2. Build Phases 1–3 on evenings; test direct mode from any clean-IP box.
3. Phase 5: rent the delivery node, flip the default SmtpConfig to `direct`,
   keep Resend config as emergency fallback for a month, then delete it.

Total build estimate: **~1.5–2 weeks of evenings** to a fully self-hosted ESP.
