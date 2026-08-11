#!/usr/bin/env python3
"""
Outreach dashboard — outreach.lokwave.cz

One place to see whether the Lokwave outreach machine is actually working. It
reads two databases directly (the app's Postgres and LaunchMail's) because
everything it shows already exists there; nothing is aggregated ahead of time,
so a number on this page is never stale or a copy that drifted.

What it exists to answer, in order:

  1. Is the funnel converting?      audits → sent → opened → clicked → trial → paid
  2. Is the machine actually alive? sends today per brand against the cap
  3. What did we say, and to whom?  every outbound email, in full
  4. What came back?                every inbound reply, in full

The third and fourth matter more than they look. The bot writes cold email with
an LLM and answers replies unattended; without reading what it actually sent,
"it's running" is not the same as "it's working", and the first sign of a bad
prompt is a mail nobody would have sent by hand.

Auth is Cloudflare Access in front (owner email only) — this process trusts the
edge and holds no credentials of its own. It binds the docker bridge, so the only
route in is Traefik behind the tunnel.
"""

import html
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("OUTREACH_DASHBOARD_PORT", "8096"))
# docker0 by default: Traefik must reach it from a container, and docker0 is not
# routable from the LAN. The public path is the Cloudflare tunnel only.
BIND = os.environ.get("OUTREACH_DASHBOARD_BIND", "172.17.0.1")

APP_DB = ("shared-postgres", "lokwave", "lokwave")
MAIL_DB = ("launchmail-postgres", "postgres", "launchmail")

BRANDS = ["auto", "bistro", "dental", "fit", "salon", "vet"]
BRAND_LABEL = {"auto": "AutoLocal", "bistro": "BistroLocal", "dental": "DentalLocal",
               "fit": "FitLocal", "salon": "SalonLocal", "vet": "VetLocal"}
DAILY_CAP = 3  # must match DAILY_CAP in bot-orchestrator.py


class QueryError(RuntimeError):
    pass


def q(target, sql):
    """Run one read-only query. Rows come back as lists of strings."""
    container, user, dbname = target
    out = subprocess.run(
        ["docker", "exec", container, "psql", "-U", user, "-d", dbname,
         "-tAF\x1f", "-c", sql],
        capture_output=True, text=True, timeout=45,
    )
    if out.returncode != 0:
        raise QueryError(out.stderr.strip()[:300])
    return [r.split("\x1f") for r in out.stdout.split("\n") if r.strip()]


def one(target, sql, default="0"):
    rows = q(target, sql)
    return rows[0][0] if rows and rows[0] else default


# ── data ─────────────────────────────────────────────────────────────────────

def funnel():
    """The whole point of the page: does outreach turn into money?"""
    audits = one(APP_DB, "SELECT count(*) FROM audits")
    sent = one(APP_DB, "SELECT count(*) FROM outreach_prospects WHERE status='sent'")
    opened = one(APP_DB, "SELECT count(*) FROM outreach_prospects WHERE opened_at IS NOT NULL")
    clicked = one(APP_DB, "SELECT count(*) FROM outreach_prospects WHERE clicked_at IS NOT NULL")
    orgs = one(APP_DB, "SELECT count(*) FROM organizations WHERE deleted_at IS NULL")
    paying = one(APP_DB, "SELECT count(*) FROM subscriptions WHERE status IN ('active','trialing','past_due')")
    locations = one(APP_DB, "SELECT count(*) FROM locations")
    return {"audits": audits, "sent": sent, "opened": opened, "clicked": clicked,
            "orgs": orgs, "paying": paying, "locations": locations}


def pipeline():
    rows = q(APP_DB, """
        SELECT vertical, status, count(*)
        FROM outreach_prospects
        WHERE vertical IN ('auto','bistro','dental','fit','salon','vet')
        GROUP BY 1, 2
    """)
    table = {b: {} for b in BRANDS}
    for vert, status, n in rows:
        table.setdefault(vert, {})[status] = int(n)
    return table


def sent_today():
    rows = q(APP_DB, """
        SELECT vertical, count(*)
        FROM outreach_prospects
        WHERE status='sent' AND sent_at::date = current_date
        GROUP BY 1
    """)
    return {v: int(n) for v, n in rows}


def daily_series(days=14):
    rows = q(APP_DB, f"""
        SELECT sent_at::date, count(*)
        FROM outreach_prospects
        WHERE status='sent' AND sent_at > now() - interval '{days} days'
        GROUP BY 1 ORDER BY 1
    """)
    return [(d, int(n)) for d, n in rows]


def outbound(limit=200, brand=None, search=None):
    where = ["status='sent'", "sent_body IS NOT NULL"]
    if brand in BRANDS:
        where.append(f"vertical = {lit(brand)}")
    if search:
        s = lit(f"%{search}%")
        where.append(f"(name ILIKE {s} OR email ILIKE {s} OR sent_subject ILIKE {s} OR sent_body ILIKE {s})")
    return q(APP_DB, f"""
        SELECT sent_at, vertical, name, email, coalesce(sent_subject,''),
               coalesce(sent_body,''), opened_at IS NOT NULL, clicked_at IS NOT NULL,
               coalesce(city,'')
        FROM outreach_prospects
        WHERE {' AND '.join(where)}
        ORDER BY sent_at DESC LIMIT {int(limit)}
    """)


def inbound(limit=200, search=None):
    where = ["1=1"]
    if search:
        s = lit(f"%{search}%")
        where.append(f"(from_address ILIKE {s} OR subject ILIKE {s} OR coalesce(text,'') ILIKE {s})")
    return q(MAIL_DB, f"""
        SELECT received_at, from_address, coalesce(from_name,''), coalesce(subject,''),
               coalesce(text, snippet, ''), seen, replied_at IS NOT NULL,
               coalesce(auto_submitted,'')
        FROM incoming_emails
        WHERE {' AND '.join(where)}
        ORDER BY received_at DESC LIMIT {int(limit)}
    """)


def delivery_log(limit=120):
    return q(MAIL_DB, """
        SELECT created_at, "to"::text, coalesce(subject,''), status, coalesce(error,'')
        FROM email_logs
        ORDER BY created_at DESC LIMIT %d
    """ % int(limit))


def health():
    """Things that have silently broken before, checked every page load."""
    out = []
    try:
        suppressed = one(APP_DB, "SELECT count(*) FROM email_suppression")
        out.append(("Odhlášené adresy", suppressed, False))
    except QueryError:
        out.append(("Odhlášené adresy", "chyba", True))
    try:
        failed = one(APP_DB, """
            SELECT count(*) FROM outreach_prospects
            WHERE status='failed' AND updated_at > now() - interval '2 days'
        """)
        out.append(("Selhalo za 48 h", failed, int(failed) > 0))
    except QueryError:
        out.append(("Selhalo za 48 h", "chyba", True))
    try:
        stale = one(APP_DB, """
            SELECT coalesce(extract(epoch FROM now() - max(sent_at))/3600, 999)::int
            FROM outreach_prospects WHERE status='sent'
        """)
        out.append(("Hodin od posledního odeslání", stale, int(stale) > 24))
    except QueryError:
        out.append(("Hodin od posledního odeslání", "chyba", True))
    try:
        noaudit = one(APP_DB, """
            SELECT count(*) FROM outreach_prospects p
            WHERE p.status='pending' AND NOT EXISTS (
              SELECT 1 FROM audits a WHERE a.prospect_place_id = p.place_id AND a.payload ? 'benchmark')
        """)
        out.append(("Fronta bez srovnání s konkurencí", noaudit, int(noaudit) > 0))
    except QueryError:
        out.append(("Fronta bez srovnání s konkurencí", "chyba", True))
    return out


def lit(value):
    """Single-quoted SQL literal. Everything user-supplied goes through here."""
    return "'" + str(value).replace("'", "''") + "'"


# ── rendering ────────────────────────────────────────────────────────────────

E = html.escape


def fmt_dt(raw):
    if not raw:
        return "—"
    return str(raw)[:16].replace("T", " ")


def page(body, active="", refreshed=""):
    return f"""<!doctype html>
<html lang="cs"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Lokwave outreach</title>
<style>
:root {{
  --ground:#f5f6f4; --surface:#fff; --surface-2:#edf0ee; --ink:#141a18; --soft:#3d4744;
  --muted:#5c6663; --line:#dbe1de; --accent:#0e5c4a; --crit:#a62b1f; --warn:#96631a; --ok:#2f6b4c;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
}}
@media (prefers-color-scheme:dark) {{ :root {{
  --ground:#0f1412; --surface:#171d1b; --surface-2:#1e2523; --ink:#e7ebe8; --soft:#c3cbc7;
  --muted:#939d99; --line:#262e2b; --accent:#5cbfa0; --crit:#e8776a; --warn:#d5a24a; --ok:#6dbb92;
}} }}
*{{box-sizing:border-box}}
body{{margin:0;padding:0 1rem 5rem;background:var(--ground);color:var(--ink);
 font:15px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:74rem;margin:0 auto}}
header{{padding:2rem 0 1rem;border-bottom:2px solid var(--ink);margin-bottom:1.5rem}}
h1{{font-family:var(--mono);font-size:1.5rem;margin:0 0 .3rem;letter-spacing:-.02em}}
.meta{{font-family:var(--mono);font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
nav{{display:flex;flex-wrap:wrap;gap:.4rem;margin:1.2rem 0}}
nav a{{font-family:var(--mono);font-size:.8rem;text-decoration:none;color:var(--soft);
 border:1px solid var(--line);padding:.35rem .7rem;border-radius:2px;background:var(--surface)}}
nav a.on{{background:var(--accent);color:var(--ground);border-color:var(--accent)}}
h2{{font-size:1.1rem;margin:2rem 0 .7rem;font-weight:640}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:1px;background:var(--line);
 border:1px solid var(--line)}}
.cell{{background:var(--surface);padding:.85rem 1rem}}
.cell dt{{font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 .3rem}}
.cell dd{{margin:0;font-family:var(--mono);font-size:1.5rem;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}}
.cell dd small{{font-size:.8rem;font-weight:400;color:var(--muted)}}
.bad dd{{color:var(--crit)}} .good dd{{color:var(--ok)}} .warn dd{{color:var(--warn)}}
.tw{{overflow-x:auto;border:1px solid var(--line);background:var(--surface)}}
table{{border-collapse:collapse;width:100%;font-size:.88rem}}
th,td{{text-align:left;padding:.5rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-family:var(--mono);font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);white-space:nowrap}}
td.n{{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}}
tbody tr:last-child td{{border-bottom:none}}
details{{border:1px solid var(--line);background:var(--surface);margin-bottom:1px}}
details summary{{padding:.6rem .8rem;cursor:pointer;font-size:.9rem;display:flex;flex-wrap:wrap;gap:.5rem;align-items:baseline}}
details summary::-webkit-details-marker{{display:none}}
details[open] summary{{border-bottom:1px solid var(--line);background:var(--surface-2)}}
pre{{margin:0;padding:.9rem 1.1rem;white-space:pre-wrap;word-break:break-word;font:13px/1.65 var(--mono);color:var(--soft)}}
.tag{{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;
 padding:.1rem .35rem;border-radius:2px;background:var(--surface-2);color:var(--muted)}}
.tag.ok{{color:var(--ok)}} .tag.hot{{color:var(--crit)}} .tag.w{{color:var(--warn)}}
form{{display:flex;gap:.4rem;margin:.8rem 0}}
input[type=search]{{flex:1;padding:.45rem .6rem;border:1px solid var(--line);background:var(--surface);
 color:var(--ink);border-radius:2px;font-size:.9rem}}
button{{padding:.45rem .9rem;border:1px solid var(--accent);background:var(--accent);color:var(--ground);
 border-radius:2px;font-size:.85rem;cursor:pointer}}
.spark{{display:flex;align-items:flex-end;gap:2px;height:44px;margin:.5rem 0}}
.spark i{{flex:1;background:var(--accent);min-height:2px;border-radius:1px 1px 0 0}}
.err{{border:1px solid var(--crit);background:var(--surface);color:var(--crit);padding:.8rem 1rem;margin:1rem 0;font-size:.9rem}}
footer{{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);font-family:var(--mono);
 font-size:.75rem;color:var(--muted)}}
</style></head><body><div class="wrap">
<header>
  <h1>Lokwave outreach</h1>
  <div class="meta">{refreshed} · 6 značek · strop {DAILY_CAP}/den/značku</div>
</header>
<nav>
  <a href="/" class="{'on' if active == 'home' else ''}">Přehled</a>
  <a href="/odeslane" class="{'on' if active == 'out' else ''}">Odeslané e-maily</a>
  <a href="/prijate" class="{'on' if active == 'in' else ''}">Přijaté e-maily</a>
  <a href="/doruceni" class="{'on' if active == 'log' else ''}">Doručování</a>
</nav>
{body}
<footer>Čte přímo z produkční databáze — žádná mezivrstva, žádná zastaralá kopie.<br>
Přístup hlídá Cloudflare Access; služba naslouchá jen na docker bridge.</footer>
</div></body></html>"""


def cell(label, value, sub="", cls=""):
    s = f" <small>{E(sub)}</small>" if sub else ""
    return f'<div class="cell {cls}"><dt>{E(label)}</dt><dd>{E(str(value))}{s}</dd></div>'


def render_home():
    f = funnel()
    pipe = pipeline()
    today = sent_today()
    series = daily_series()

    sent_n = int(f["sent"]) or 1
    open_pct = round(100 * int(f["opened"]) / sent_n)
    click_pct = round(100 * int(f["clicked"]) / sent_n)

    b = ['<h2>Trychtýř</h2><dl class="grid">']
    b.append(cell("Auditů", f["audits"]))
    b.append(cell("Odesláno", f["sent"]))
    b.append(cell("Otevřeno", f["opened"], f"{open_pct} %", "good" if open_pct >= 20 else ""))
    b.append(cell("Prokliků", f["clicked"], f"{click_pct} %", "good" if click_pct > 0 else "bad"))
    b.append(cell("Registrací", f["orgs"]))
    b.append(cell("Poboček", f["locations"]))
    b.append(cell("Platících", f["paying"], "", "good" if int(f["paying"]) else "bad"))
    b.append("</dl>")

    total_today = sum(today.values())
    cap_total = DAILY_CAP * len(BRANDS)
    b.append("<h2>Dnes odesláno</h2>")
    b.append('<dl class="grid">')
    b.append(cell("Celkem dnes", f"{total_today}", f"z {cap_total}",
                  "good" if total_today >= cap_total else "warn" if total_today else "bad"))
    for br in BRANDS:
        n = today.get(br, 0)
        b.append(cell(BRAND_LABEL[br], f"{n}", f"z {DAILY_CAP}", "good" if n >= DAILY_CAP else ""))
    b.append("</dl>")

    if series:
        peak = max(n for _, n in series) or 1
        bars = "".join(
            f'<i style="height:{max(2, round(100 * n / peak))}%" title="{E(d)}: {n}"></i>'
            for d, n in series)
        b.append(f'<h2>Posledních 14 dní</h2><div class="spark">{bars}</div>')

    b.append("<h2>Fronta podle značky</h2><div class='tw'><table><thead><tr><th>Značka</th>"
             "<th>Čeká</th><th>Kvalifikováno</th><th>Odesláno</th><th>Selhalo</th><th>Přeskočeno</th>"
             "<th>Dnů zásoby</th></tr></thead><tbody>")
    for br in BRANDS:
        row = pipe.get(br, {})
        pending = row.get("pending", 0)
        runway = round(pending / DAILY_CAP) if pending else 0
        b.append(
            f"<tr><td>{E(BRAND_LABEL[br])}</td>"
            f'<td class="n">{pending}</td><td class="n">{row.get("qualified", 0)}</td>'
            f'<td class="n">{row.get("sent", 0)}</td><td class="n">{row.get("failed", 0)}</td>'
            f'<td class="n">{row.get("skipped", 0)}</td><td class="n">{runway}</td></tr>')
    b.append("</tbody></table></div>")

    b.append("<h2>Kontrolky</h2><dl class='grid'>")
    for label, value, bad in health():
        b.append(cell(label, value, "", "bad" if bad else ""))
    b.append("</dl>")
    return "".join(b)


def render_outbound(brand=None, search=None):
    rows = outbound(brand=brand, search=search)
    b = [f'<h2>Odeslané e-maily <span class="tag">{len(rows)}</span></h2>']
    b.append('<form method="get"><input type="search" name="q" placeholder="Hledat ve jménu, adrese, předmětu i textu…" '
             f'value="{E(search or "")}"><button>Hledat</button></form>')
    b.append('<nav><a href="/odeslane" class="' + ("on" if not brand else "") + '">Vše</a>' +
             "".join(f'<a href="/odeslane?brand={br}" class="{"on" if brand == br else ""}">{BRAND_LABEL[br]}</a>'
                     for br in BRANDS) + "</nav>")
    if not rows:
        b.append("<p>Nic neodpovídá.</p>")
    for sent_at, vert, name, email, subject, body, opened, clicked, city in rows:
        tags = [f'<span class="tag">{E(BRAND_LABEL.get(vert, vert))}</span>']
        if opened == "t":
            tags.append('<span class="tag ok">otevřel</span>')
        if clicked == "t":
            tags.append('<span class="tag hot">proklik</span>')
        b.append(
            "<details><summary>"
            f'<span class="tag">{E(fmt_dt(sent_at))}</span>'
            f"<strong>{E(name)}</strong> <span class='tag'>{E(city)}</span> "
            f"<span style='color:var(--muted)'>{E(email)}</span> {' '.join(tags)}"
            f"<br>{E(subject)}</summary>"
            f"<pre>{E(body)}</pre></details>")
    return "".join(b)


def render_inbound(search=None):
    rows = inbound(search=search)
    b = [f'<h2>Přijaté e-maily <span class="tag">{len(rows)}</span></h2>']
    b.append('<form method="get"><input type="search" name="q" placeholder="Hledat v odesílateli, předmětu i textu…" '
             f'value="{E(search or "")}"><button>Hledat</button></form>')
    if not rows:
        b.append("<p>Nic neodpovídá.</p>")
    for received, frm, frm_name, subject, text, seen, replied, auto in rows:
        tags = []
        if replied == "t":
            tags.append('<span class="tag ok">bot odpověděl</span>')
        elif seen == "t":
            tags.append('<span class="tag w">jen přečteno</span>')
        else:
            tags.append('<span class="tag hot">nové</span>')
        if auto:
            tags.append('<span class="tag">automat</span>')
        who = f"{frm_name} <{frm}>" if frm_name else frm
        b.append(
            "<details><summary>"
            f'<span class="tag">{E(fmt_dt(received))}</span>'
            f"<strong>{E(who)}</strong> {' '.join(tags)}<br>{E(subject)}</summary>"
            f"<pre>{E(text)}</pre></details>")
    return "".join(b)


def render_log():
    rows = delivery_log()
    b = ['<h2>Doručování (LaunchMail)</h2><div class="tw"><table><thead><tr>'
         "<th>Čas</th><th>Příjemce</th><th>Předmět</th><th>Stav</th><th>Chyba</th>"
         "</tr></thead><tbody>"]
    for created, to, subject, status, error in rows:
        try:
            addr = ", ".join(x.get("email", "") for x in json.loads(to))
        except Exception:
            addr = to
        cls = "" if status in ("sent", "delivered", "queued") else ' style="color:var(--crit)"'
        b.append(f'<tr><td class="n">{E(fmt_dt(created))}</td><td>{E(addr)}</td>'
                 f"<td>{E(subject[:70])}</td><td{cls}>{E(status)}</td><td>{E(error[:70])}</td></tr>")
    b.append("</tbody></table></div>")
    return "".join(b)


ROUTES = {"/": ("home", render_home), "/odeslane": ("out", render_outbound),
          "/prijate": ("in", render_inbound), "/doruceni": ("log", render_log)}


class Handler(BaseHTTPRequestHandler):
    server_version = "lokwave-outreach"

    def log_message(self, *_):  # keep the journal for real events only
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        route = ROUTES.get(parsed.path)
        if parsed.path == "/health":
            return self._send(200, "ok", "text/plain")
        if not route:
            return self._send(404, page("<h2>Stránka neexistuje</h2>", refreshed=stamp()), "text/html")

        active, fn = route
        params = parse_qs(parsed.query)
        search = (params.get("q") or [None])[0]
        brand = (params.get("brand") or [None])[0]
        # Only ever accept a brand we know; the value reaches SQL.
        brand = brand if brand in BRANDS else None
        try:
            if active == "out":
                body = fn(brand=brand, search=search)
            elif active == "in":
                body = fn(search=search)
            else:
                body = fn()
        except QueryError as err:
            body = f'<div class="err">Dotaz do databáze selhal: {E(str(err))}</div>'
        except Exception as err:  # never show a stack trace at the edge
            body = f'<div class="err">Neočekávaná chyba: {E(type(err).__name__)}</div>'
        self._send(200, page(body, active=active, refreshed=stamp()), "text/html")

    def _send(self, code, payload, ctype):
        data = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)


def stamp():
    return datetime.now(timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
