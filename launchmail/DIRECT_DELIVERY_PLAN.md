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

## Phase 1 — Transport abstraction + direct-MX MVP — ✅ DONE (2026-07-11)

- [x] Schema: `smtpConfigs.type: "smarthost" | "direct"` (default `smarthost`)
      + `heloHostname`; `host/username/password_encrypted` made nullable.
      Migration `0009_direct_delivery.sql`.
- [x] `packages/mail-queue/src/direct-transport.ts`: `resolveMx` per recipient
      domain (priority-sorted, implicit-MX fallback); delivers via nodemailer
      `SMTPTransport` (`host=<mx>`, `port=25`, `secure=false`,
      `opportunisticTLS`), EHLO=`heloHostname`, return-path aligned to From
      domain; **requires DKIM** (permanent 5xx if missing); classifies replies
      (network/4xx retryable, 5xx permanent), tries next MX on retryable;
      groups recipients by domain.
- [x] `smtp.ts` `sendMail()` branches on `config.type`; `testSmtpConnection`
      does an MX + port-25 probe for direct. Worker unchanged.
- [x] Web UI: `new-smtp-form.tsx` delivery-mode toggle; direct shows
      `heloHostname` + PTR/DKIM/SPF/port-25 requirements.
- [x] Unit tests (`direct-transport.test.ts`, 13). Full workspace typecheck +
      lint + tests green.
- [ ] **Remaining acceptance (needs the egress node):** from a clean-IP host,
      send to Gmail+Seznam → inbox, `spf=pass dkim=pass`. Code is ready; only
      blocked on a host with PTR + open port 25 (Phase 5).

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

The ONLY rented ingredient: an IP with a controllable PTR and open port 25.
The launchmail brain (queue, DKIM, tracking, UI, data) stays on the homelab.
Two ways to get that IP, cheapest first:

- **Option A — a friend's / existing server with a public IP.** Viable **only
  if** that box (a) has a static public IPv4 whose PTR the owner can set to
  `mail.<domain>`, and (b) has outbound port 25 open. A self-hosted
  Headscale/Tailscale tunnel gives network reach but does NOT change the exit
  IP's PTR/reputation — the exit host itself must meet (a)+(b). If it does:
  run the `worker` there (`WORKER_ROLE=direct`) over the tailnet — €0.
- **Option B — tiny VPS.** Hetzner CAX11/CX22 (~€4/mo) or any provider that
  sets PTR + unlocks port 25 (Hetzner: support ticket, short delay). Some
  budget hosts leave 25 open by default.

Then, whichever host:
- [ ] Set **PTR** = `mail.<domain>`; confirm outbound **port 25** works.
- [ ] Join Tailscale → run `worker` with `WORKER_ROLE=direct`, `REDIS_URL`
      over the tailnet to the homelab.
- [ ] DNS per sending domain: SPF `ip4:<node-ip>`, DKIM key (from launchmail),
      DMARC (`p=none` → `quarantine` after warm-up). Optional MTA-STS/TLS-RPT.
- [ ] Acceptance: mail-tester.com ≥ 9/10 through our own pipe; ramp via
      Phase 4 warm-up.

## Non-goals (for now)

Dedicated IP pools / multi-node scheduling, ARC sealing, full inbound MX
hosting (IMAP path covers receiving), DMARC aggregate-report analytics UI.

## Rollout strategy (homelab) — NO third-party sender, ever

The user's requirement: zero Resend/Brevo/SES. So:

1. **Day D:** deploy launchmail (Phase 1 code is already merged). Apps can
   enqueue mail immediately; it just won't leave the building until an egress
   host exists. For the brief gap, the only "third party" tolerated is the
   user's own Gmail as a stopgap SmtpConfig (app password, 500/day) — optional.
2. **Get the egress IP (Phase 5, Option A friend's box or B cheap VPS).** Set
   PTR + port 25 + DNS. Create a `direct` SmtpConfig, make it default.
3. From then on launchmail delivers everything itself. Build Phases 2–4
   (rate limits, bounce ingestion, deliverability console) on evenings to
   harden it. No external sending service is ever part of the pipeline.

Remaining build estimate after Phase 1: **~1–1.5 weeks of evenings**.
