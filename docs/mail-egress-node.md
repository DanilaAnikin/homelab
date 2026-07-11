# Mail egress node — launchmail's own sender (Phase 5)

This is the final piece that makes launchmail deliver mail **fully on its own**,
with no Resend/Brevo/SES in the pipeline. It's the one thing code can't provide:
an IP address with a good reputation.

## Why a separate node at all

Sending mail that lands in the inbox requires, at the sending IP:
- a **PTR record** (reverse DNS) matching the HELO hostname (FCrDNS), and
- **outbound port 25** open.

A home line can't give you a controllable PTR (the ISP owns it), so the "send"
hop must run somewhere you control the IP. Everything else — the queue, DKIM
signing, templates, tracking, bounce handling, the UI, the database — stays on
the homelab. The egress node is a dumb, reputable pipe.

```
homelab:  launchmail API + web + Postgres + Redis + DKIM keys   (the brain)
              │  BullMQ queue over Tailscale
              ▼
egress node:  launchmail worker  →  recipient MX servers :25    (the pipe)
              (public IP with PTR, port 25 open)
```

## Two ways to get the IP (cheapest first)

1. **A friend's / existing server.** Works only if it has a **static public
   IPv4 whose PTR the owner can set** to `mail.<domain>`, and **outbound port 25
   is open**. A self-hosted Headscale/Tailscale tunnel gives network reach but
   does NOT change the exit IP's PTR — the exit host itself must meet both. If
   it does: €0.
2. **A tiny VPS.** Hetzner CAX11/CX22 (~€4/mo) or similar. Set PTR in the
   console; request outbound port 25 (Hetzner: a short support ticket).

## Setup

On the egress host:

```bash
# 1) provision + preflight (checks PTR + port 25 for you)
sudo MAIL_HELO_HOSTNAME=mail.ripieno.xyz bash scripts/egress-node-setup.sh
tailscale up                                   # join your tailnet

# 2) point it at the homelab and run just the worker
cd compose/mail-egress
cp .env.example .env      # fill DATABASE_URL/REDIS_URL (homelab over tailnet),
                          # MAIL_ENCRYPTION_KEY + BETTER_AUTH_SECRET (same as homelab)
chmod 600 .env
docker compose up -d --build
```

In the launchmail UI:
1. **SMTP → New → Direct**, HELO hostname = `mail.ripieno.xyz`, make it default.
2. Verify the sending domain (creates the DKIM key).
3. Open the domain's **Deliverability** panel → publish the shown records
   (SPF `ip4:<node-ip>`, DKIM, DMARC) in Cloudflare; set the **PTR** at your
   host provider. Re-run the check until all green.
4. Send a test to [mail-tester.com](https://www.mail-tester.com) → aim ≥ 9/10.

## Important: only one worker sends

When the egress node runs the worker, the **homelab must not also run one** —
otherwise the home worker (no PTR / maybe blocked :25) could grab direct jobs.
On the homelab, run launchmail's api container with `start` (api only) instead
of `start:all`. Everything else on the homelab is unchanged.

## Bounces (completes Phase 3 in production)

Create a receive-enabled SMTP config for `bounces@<domain>` (a Seznam mailbox
with plus-addressing, or a catch-all) so the VERP DSNs (`bounces+<id>@domain`)
are IMAP-synced and auto-processed into suppressions.

## Until the node exists

Send via the **Seznam SMTP smarthost** config (day-1 path). Switching to direct
later is just adding the Direct config and flipping the default — no code change.
