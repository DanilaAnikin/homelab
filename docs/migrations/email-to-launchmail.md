# Migrace e-mailů: Resend / nodemailer / SendGrid → LaunchMail

Platí pro **všechny projekty**. Cíl: každý odchozí e-mail (transakční i marketingový)
jde přes **náš LaunchMail** místo cizí služby. Žádné měsíční platby, jedna platforma,
vlastní šablony, tracking a bounce handling.

> Předpoklad: LaunchMail běží na serveru (Dokploy) na `https://mail.<doména>`,
> máš v něm ověřenou odesílací doménu (DKIM) a **API token** pro každý projekt.

---

## Jak LaunchMail přijímá odchozí poštu

Jednotné API — nezávislé na frameworku:

```
POST https://mail.<doména>/api/mail/send
Authorization: Bearer <API_TOKEN_projektu>
Content-Type: application/json

{
  "from": "Projekt <noreply@mail.tvojedomena.cz>",   // volitelné; jinak default z configu
  "to":   [{ "email": "user@example.com", "name": "Jan" }],
  "cc":   [...],                                       // volitelné
  "bcc":  [...],                                       // volitelné
  "replyTo": "podpora@tvojedomena.cz",                // volitelné
  "subject": "Vítej!",
  "html": "<h1>Ahoj</h1>",
  "text": "Ahoj",
  "sendAt": "2026-08-01T09:00:00Z"                     // volitelné → naplánované odeslání
}
```

Odpověď: `202` + `{ id, status: "queued" | "scheduled" }`. LaunchMail se postará
o frontu, retry, DKIM podpis, tracking i bounce handling.

## Dvě cesty integrace

### A) Oficiální SDK (Node/TS projekty — doporučeno)

```ts
import { LaunchMailClient } from "@workspace/launchmail-sdk"; // nebo zkopírovaný klient

const mail = new LaunchMailClient({
  baseUrl: process.env.LAUNCHMAIL_URL!,   // https://mail.tvojedomena.cz
  apiKey:  process.env.LAUNCHMAIL_API_KEY!,
});

await mail.sendEmail({
  to: [{ email: user.email, name: user.name }],
  subject: "Vítej!",
  html: renderWelcome(user),
});
```

> SDK je v `launchmail/packages/sdk`. Pro externí projekt buď publikuj SDK do
> privátního npm, nebo zkopíruj jednosouborový klient (je malý) do `lib/launchmail.ts`.

### B) Prostý `fetch` (jakýkoli jazyk — Python, Deno, edge)

```ts
await fetch(`${process.env.LAUNCHMAIL_URL}/api/mail/send`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${process.env.LAUNCHMAIL_API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ to: [{ email }], subject, html }),
});
```

```python
import os, httpx
httpx.post(
  f"{os.environ['LAUNCHMAIL_URL']}/api/mail/send",
  headers={"Authorization": f"Bearer {os.environ['LAUNCHMAIL_API_KEY']}"},
  json={"to": [{"email": email}], "subject": subject, "html": html},
  timeout=15,
)
```

---

## Náhrada podle současného provideru

### Resend → LaunchMail

Najdi všechna volání `resend.emails.send(...)` (a `import { Resend }`). Mapování polí je 1:1:

| Resend | LaunchMail |
|--------|-----------|
| `from: "X <a@b>"` | `from: "X <a@b>"` |
| `to: ["a@b"]` nebo `[{...}]` | `to: [{ email: "a@b", name?: "…" }]` (vždy pole objektů) |
| `subject`, `html`, `text` | stejné |
| `reply_to` | `replyTo` |
| `cc`, `bcc` | `cc`, `bcc` (pole objektů) |
| `react: <Email/>` | vyrenderuj na HTML (`@react-email/render`) a pošli `html` |
| `scheduledAt` | `sendAt` |

**React Email šablony** necháváš — jen místo `react:` je předrenderuješ:
```ts
import { render } from "@react-email/render";
const html = await render(<WelcomeEmail name={user.name} />);
await mail.sendEmail({ to: [{ email }], subject, html });
```
(Časem lze šablony přesunout do LaunchMail templates, ale není nutné — HTML stačí.)

**Resend Audiences / Broadcasts** (marketing): LaunchMail má audiences; migrace
kontaktů je samostatný krok — pro transakční maily neřeš.

### nodemailer (SMTP) → LaunchMail

Dvě možnosti:
1. **Nech nodemailer, jen přesměruj SMTP** na LaunchMail SMTP endpoint (pokud
   projekt chce zůstat u SMTP). Vyžaduje SMTP bránu LaunchMailu — jednodušší je ale:
2. **Nahraď `transporter.sendMail(...)` za `mail.sendEmail(...)`** (HTTP API výše).
   Odpadne správa SMTP credentials v každém projektu; jen jeden API token.

### SendGrid / Postmark / Mailgun → LaunchMail

Odstraň jejich SDK, nahraď volání za `mail.sendEmail(...)`. Šablony přenes jako HTML.

---

## Env proměnné (přidat do každého projektu)

```env
LAUNCHMAIL_URL=https://mail.tvojedomena.cz
LAUNCHMAIL_API_KEY=lm_...            # per-projekt token z LaunchMail UI (Settings → API keys)
EMAIL_FROM=Projekt <noreply@mail.tvojedomena.cz>
```
Odstraň: `RESEND_API_KEY`, `SENDGRID_API_KEY`, `SMTP_*` (pokud šlo o cizí SMTP).

## Postup per projekt (checklist)

1. V LaunchMail UI vytvoř **API key** pro projekt (role „writer" stačí na odesílání).
2. Přidej `LAUNCHMAIL_URL` + `LAUNCHMAIL_API_KEY` do env (Dokploy / Vercel).
3. Vytvoř `lib/launchmail.ts` (SDK klient nebo fetch wrapper).
4. Najdi všechna místa odesílání (`grep -rn "resend\|nodemailer\|sendgrid" src`).
5. Nahraď je za `mail.sendEmail(...)`; React Email šablony předrenderuj na HTML.
6. Odstraň starý SDK z dependencies + staré env proměnné.
7. **Test:** pošli si testovací mail; ověř v LaunchMail → Email logs, že dorazil
   (`sent`), a v cílové schránce že je v inboxu (ne spam).
8. Smaž starý účet u provideru až po pár dnech ověřeného provozu.

## Bezpečnostní zásady

- API token drž jen v env (server-side). **Nikdy** ho nedávej do klientského/
  frontend bundlu ani do mobilní appky — mail se posílá vždy ze serveru/API.
- Jeden token per projekt (snadné odvolání, oddělené logy, per-projekt reputace).
- Doménu ověř (DKIM) v LaunchMailu dřív, než pustíš ostrý provoz — jinak spam.
