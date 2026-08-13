#!/usr/bin/env python3
"""Lokwave outreach dashboard — outreach.lokwave.cz

Reads the two production databases directly through `docker exec psql`. There is
no cache and no intermediate store: every page load is a fresh query, so what it
shows is what the system actually holds.

Read-only by construction — it issues SELECTs and serves HTML, nothing else.
Access is gated by Cloudflare Access at the edge; the process itself listens
only on the docker bridge, which is host-local.
"""

import html
import json
import os
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BIND = os.environ.get("OUTREACH_DASHBOARD_BIND", "172.17.0.1")
PORT = int(os.environ.get("OUTREACH_DASHBOARD_PORT", "8096"))

APP_DB = ("shared-postgres", "lokwave", "lokwave")
MAIL_DB = ("launchmail-postgres", "postgres", "launchmail")

BRANDS = ["auto", "bistro", "dental", "fit", "salon", "vet"]
BRAND_LABEL = {"auto": "AutoLocal", "bistro": "BistroLocal", "dental": "DentalLocal",
               "fit": "FitLocal", "salon": "SalonLocal", "vet": "VetLocal"}
DAILY_CAP = 5  # must match DAILY_CAP in bot-orchestrator.py

# Prospects that never got as far as an email are attrition, not breakage:
# a place with no website or no published address was simply not reachable.
# Lumping them in with real send failures made 350 look like an outage.
# A hard bounce is the mail system telling us the address is dead. Suppressing
# it is the correct outcome, not a fault to chase, so it belongs with the other
# attrition rather than in the "something is broken" light.
NOT_REACHABLE = ("no_email_found", "no_website", "placeholder_email", "hard_bounce")

# LaunchMail is shared with the other projects on this host (Freio, Ripieno), so
# its tables hold their mail too. Every query against it must be scoped to our
# own mailboxes or the numbers on this page quietly describe someone else's
# outreach — 13 of the 135 stored inbound messages are not ours.
# The prospect table is shared with the other projects on this host too. Their
# rows carry a vertical we do not own (and no place_id at all), so without this
# every count on the page — sends, opens, the daily cap — quietly includes
# someone else's outreach.
VERT_SCOPE = "vertical IN (" + ", ".join("'" + b + "'" for b in BRANDS) + ")"

OUR_MAILBOXES = tuple([f"contact@{b}local.cz" for b in BRANDS] + ["contact@lokwave.cz"])
MAIL_SCOPE = ("smtp_config_id IN (SELECT id FROM smtp_configs WHERE from_address IN "
              f"{OUR_MAILBOXES})")

# psql writes one record per line by default, which silently corrupts any column
# holding a multi-line value — and email bodies are exactly that. An explicit
# record separator keeps embedded newlines inside their field where they belong.
FS = "\x1f"
RS = "\x1e"


class QueryError(RuntimeError):
    pass


def q(target, sql, ncols=None):
    """Run one read-only query. Rows come back as lists of strings."""
    container, user, dbname = target
    out = subprocess.run(
        ["docker", "exec", container, "psql", "-U", user, "-d", dbname,
         "-tA", "-F", FS, "-R", RS, "-c", sql],
        capture_output=True, text=True, timeout=45,
    )
    if out.returncode != 0:
        raise QueryError(out.stderr.strip()[:300])
    rows = [r.split(FS) for r in out.stdout.split(RS) if r.strip("\n\r ")]
    if ncols:
        # Never let one malformed row take down the whole page.
        rows = [(r + [""] * ncols)[:ncols] for r in rows]
    return rows


def one(target, sql, default="0"):
    rows = q(target, sql)
    return rows[0][0] if rows and rows[0] else default


def num(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── data ─────────────────────────────────────────────────────────────────────

def funnel():
    """The whole point of the page: does outreach turn into money?"""
    # `status='sent'` alone over-counts: 57 rows carry the status with a NULL
    # sent_at and NULL body, left behind by an earlier send path. They never
    # reached anyone, so counting them deflates every rate below.
    sent = one(APP_DB, f"SELECT count(*) FROM outreach_prospects WHERE status='sent' AND sent_at IS NOT NULL AND {VERT_SCOPE}")
    ghosts = one(APP_DB, f"SELECT count(*) FROM outreach_prospects WHERE status='sent' AND sent_at IS NULL AND {VERT_SCOPE}")
    return {
        "audits": one(APP_DB, "SELECT count(*) FROM audits"),
        "sent": sent,
        "ghosts": ghosts,
        "opened": one(APP_DB, f"SELECT count(*) FROM outreach_prospects WHERE opened_at IS NOT NULL AND {VERT_SCOPE}"),
        "clicked": one(APP_DB, f"SELECT count(*) FROM outreach_prospects WHERE clicked_at IS NOT NULL AND {VERT_SCOPE}"),
        "orgs": one(APP_DB, "SELECT count(*) FROM organizations WHERE deleted_at IS NULL"),
        "locations": one(APP_DB, "SELECT count(*) FROM locations"),
        # Trials and past_due are NOT revenue. Keep them visible, keep them apart.
        "paying": one(APP_DB, "SELECT count(*) FROM subscriptions WHERE status='active'"),
        "trialing": one(APP_DB, "SELECT count(*) FROM subscriptions WHERE status='trialing'"),
    }


def pipeline():
    rows = q(APP_DB, f"""
        SELECT vertical,
               CASE WHEN status='failed' AND coalesce(error,'') IN {NOT_REACHABLE}
                    THEN 'unreachable' ELSE status END,
               count(*)
        FROM outreach_prospects
        WHERE vertical IN ('auto','bistro','dental','fit','salon','vet')
        GROUP BY 1, 2
    """, ncols=3)
    table = {b: {} for b in BRANDS}
    for vert, status, n in rows:
        table.setdefault(vert, {})[status] = num(n)
    return table


def sent_today():
    rows = q(APP_DB, f"""
        SELECT vertical, count(*)
        FROM outreach_prospects
        WHERE status='sent' AND sent_at::date = current_date AND {VERT_SCOPE}
        GROUP BY 1
    """, ncols=2)
    return {v: num(n) for v, n in rows}


def daily_series(days=14):
    rows = q(APP_DB, f"""
        SELECT to_char(sent_at::date, 'DD.MM'), count(*)
        FROM outreach_prospects
        WHERE status='sent' AND sent_at > now() - interval '{days} days' AND {VERT_SCOPE}
        GROUP BY sent_at::date ORDER BY sent_at::date
    """, ncols=2)
    return [(d, num(n)) for d, n in rows]


def prospect_senders():
    """Which inbound addresses belong to a business we actually emailed?

    This is the only metric that matters on the inbound side — everything else
    in that mailbox is noise. The two databases are separate, so it is done as
    two cheap queries rather than a join: distinct senders (a handful) checked
    against the prospect table.
    """
    rows = q(MAIL_DB, f"""
        SELECT DISTINCT lower(from_address) FROM incoming_emails
        WHERE coalesce(from_address,'') <> '' AND {MAIL_SCOPE}
    """, ncols=1)
    addrs = [r[0] for r in rows if r[0]][:400]
    if not addrs:
        return {}
    inlist = ", ".join(lit(a) for a in addrs)
    # The prospect table is shared with the other projects too — a Ripieno lead
    # replying is not a Lokwave lead.
    verticals = ", ".join(lit(b) for b in BRANDS)
    hits = q(APP_DB, f"""
        SELECT lower(email), name, vertical, coalesce(city,'')
        FROM outreach_prospects
        WHERE lower(email) IN ({inlist}) AND vertical IN ({verticals})
    """, ncols=4)
    return {h[0]: {"name": h[1], "vertical": h[2], "city": h[3]} for h in hits}


def outbound(limit=300, brand=None, search=None):
    where = ["status='sent'", "sent_at IS NOT NULL", VERT_SCOPE]
    if brand in BRANDS:
        where.append(f"vertical = {lit(brand)}")
    if search:
        s = lit(f"%{search}%")
        where.append(f"(name ILIKE {s} OR email ILIKE {s} OR coalesce(sent_subject,'') ILIKE {s}"
                     f" OR coalesce(sent_body,'') ILIKE {s} OR coalesce(city,'') ILIKE {s})")
    clause = " AND ".join(where)
    total = one(APP_DB, f"SELECT count(*) FROM outreach_prospects WHERE {clause}")
    rows = q(APP_DB, f"""
        SELECT sent_at, vertical, name, email, coalesce(sent_subject,''),
               coalesce(sent_body,''), opened_at IS NOT NULL, clicked_at IS NOT NULL,
               coalesce(city,''), coalesce(audit_score::text,'')
        FROM outreach_prospects
        WHERE {clause}
        ORDER BY sent_at DESC LIMIT {int(limit)}
    """, ncols=10)
    return rows, num(total)


def inbound(limit=300, search=None):
    where = [MAIL_SCOPE]
    if search:
        s = lit(f"%{search}%")
        where.append(f"(i.from_address ILIKE {s} OR coalesce(i.subject,'') ILIKE {s}"
                     f" OR coalesce(i.text,'') ILIKE {s})")
    clause = " AND ".join(where)
    total = one(MAIL_DB, f"""
        SELECT count(*) FROM incoming_emails i
        LEFT JOIN smtp_configs s ON s.id = i.smtp_config_id WHERE {clause}""")
    rows = q(MAIL_DB, f"""
        SELECT i.received_at, i.from_address, coalesce(i.from_name,''), coalesce(i.subject,''),
               coalesce(i.text, i.snippet, ''), i.replied_at IS NOT NULL, i.starred, i.archived,
               coalesce(i.auto_submitted,''), coalesce(s.from_address,'')
        FROM incoming_emails i
        LEFT JOIN smtp_configs s ON s.id = i.smtp_config_id
        WHERE {clause}
        ORDER BY i.received_at DESC LIMIT {int(limit)}
    """, ncols=10)
    return rows, num(total)


SELFCHECK_STATE = "/srv/homelab/state/selfcheck.json"


def selfcheck():
    """Last result of the daily money-path self-check.

    Kept on the page because the four failures found on 2026-08-12 — undelivered
    Stripe webhooks, a checkout that threw for every paying customer, a refused
    login and a rate limiter keyed on the proxy — were all silent. The outreach
    numbers were healthy throughout.
    """
    try:
        with open(SELFCHECK_STATE) as fh:
            return json.load(fh)
    except Exception:
        return None


def subject_variants():
    """Which subject-line strategy earns the open, and the click.

    'gap' leads with a defect found in the profile, 'rival' with a named
    neighbour doing better. Rows sent before the split have a null variant and
    are excluded — attributing them to either arm would invent a result.
    """
    return q(APP_DB, f"""
        SELECT subject_variant,
               count(*),
               count(*) FILTER (WHERE opened_at IS NOT NULL),
               count(*) FILTER (WHERE clicked_at IS NOT NULL)
        FROM outreach_prospects
        WHERE subject_variant IS NOT NULL AND sent_at IS NOT NULL AND {VERT_SCOPE}
        GROUP BY 1 ORDER BY 1
    """, ncols=4)


def waiting():
    """Zprávy, u kterých se bot vědomě zastavil a předal je člověku.

    Tohle je jediná fronta, do které se má chodit ručně. Do 11. 8. v ní leželo
    40 položek, z toho 39 strojů (DMARC reporty, hlášky z Instagramu, úřední
    e-podatelny) — a jeden skutečný zájemce, kterého v tom nikdo nenašel.
    """
    return q(MAIL_DB, f"""
        SELECT i.received_at, i.from_address, coalesce(i.from_name,''),
               coalesce(i.subject,''), coalesce(i.text, i.snippet, ''),
               coalesce(s.from_address,'')
        FROM incoming_emails i
        LEFT JOIN smtp_configs s ON s.id = i.smtp_config_id
        WHERE {MAIL_SCOPE} AND i.starred AND i.replied_at IS NULL
        ORDER BY i.received_at DESC LIMIT 100
    """, ncols=6)


def delivery_log(limit=150):
    return q(MAIL_DB, f"""
        SELECT created_at, "to"::text, coalesce(subject,''), status, coalesce(error,'')
        FROM email_logs
        WHERE {MAIL_SCOPE}
        ORDER BY created_at DESC LIMIT {int(limit)}
    """, ncols=5)


def health():
    """Things that have silently broken before, checked every page load."""
    checks = [
        ("Odhlášené adresy", APP_DB, "SELECT count(*) FROM email_suppression",
         lambda v: False),
        ("Chyby odeslání za 48 h", APP_DB, f"""
            SELECT count(*) FROM outreach_prospects
            WHERE status='failed' AND {VERT_SCOPE} AND coalesce(error,'') NOT IN {NOT_REACHABLE}
              AND updated_at > now() - interval '2 days'
         """, lambda v: num(v) > 0),
        ("Hodin od posledního odeslání", APP_DB, f"""
            SELECT coalesce(extract(epoch FROM now() - max(sent_at))/3600, 999)::int
            FROM outreach_prospects WHERE status='sent' AND {VERT_SCOPE}
         """, lambda v: num(v) > 24),
        # A `benchmark` key holding JSON null is what a lookup that found no
        # neighbours writes. Counting the key alone reported the queue as ready
        # while those emails still had no competitor to name.
        ("Fronta bez srovnání s konkurencí", APP_DB, f"""
            SELECT count(*) FROM outreach_prospects p
            WHERE p.status='pending' AND p.{VERT_SCOPE} AND NOT EXISTS (
              SELECT 1 FROM audits a WHERE a.prospect_place_id = p.place_id
                AND jsonb_typeof(a.payload -> 'benchmark') = 'object')
         """, lambda v: num(v) > 0),
        # `seen` is not a reliable signal: LaunchMail leaves it false on messages
        # the bot archived after replying. What actually matters is whether a
        # message was neither answered, nor filed, nor flagged for a human.
        ("Nevyřízená pošta", MAIL_DB, f"""
            SELECT count(*) FROM incoming_emails
            WHERE {MAIL_SCOPE} AND NOT seen AND replied_at IS NULL AND NOT archived
         """, lambda v: num(v) > 0),
        ("Čeká na tebe", MAIL_DB,
         f"SELECT count(*) FROM incoming_emails WHERE {MAIL_SCOPE} AND starred AND replied_at IS NULL",
         lambda v: num(v) > 0),
    ]
    out = []
    for label, target, sql, is_bad in checks:
        try:
            value = one(target, sql)
            out.append((label, value, is_bad(value)))
        except QueryError:
            out.append((label, "chyba", True))
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


def pct(part, whole):
    whole = num(whole)
    return round(100 * num(part) / whole, 1) if whole else 0.0


def initial(text):
    text = (text or "?").strip()
    return E(text[0].upper()) if text else "?"


CSS = """
:root{
  --ground:#f4f6f5; --surface:#fff; --raise:#fbfcfb; --sunk:#eceff0;
  --ink:#101614; --soft:#39443f; --muted:#6b7671; --line:#dde3e0; --line-2:#eef1ef;
  --accent:#0d6152; --accent-soft:#e3f0eb; --crit:#a52a1d; --crit-soft:#fbe9e6;
  --warn:#8a5b12; --warn-soft:#fbf0da; --ok:#256b4b; --ok-soft:#e2f1e8;
  --shadow:0 1px 2px rgba(16,22,20,.05),0 8px 24px -16px rgba(16,22,20,.28);
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0d1211; --surface:#151b19; --raise:#1a2220; --sunk:#111716;
  --ink:#e8ecea; --soft:#c2cbc7; --muted:#8d9894; --line:#252d2b; --line-2:#1e2523;
  --accent:#4fbfa1; --accent-soft:#16332c; --crit:#ea8073; --crit-soft:#331c19;
  --warn:#d6a44e; --warn-soft:#33290f; --ok:#6fbf95; --ok-soft:#16301f;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -18px rgba(0,0,0,.8);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;padding:0 1.25rem 5rem;background:var(--ground);color:var(--ink);
 font:15px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:78rem;margin:0 auto}
a{color:inherit}

header{display:flex;flex-wrap:wrap;gap:1rem;align-items:baseline;justify-content:space-between;
 padding:2rem 0 1.1rem}
.brand{display:flex;align-items:center;gap:.6rem}
.dot{width:.55rem;height:.55rem;border-radius:50%;background:var(--ok);flex:none;
 box-shadow:0 0 0 3px var(--ok-soft)}
h1{font-size:1.35rem;margin:0;letter-spacing:-.02em;font-weight:680}
.meta{font-family:var(--mono);font-size:.75rem;color:var(--muted);letter-spacing:.02em}

nav.tabs{display:flex;flex-wrap:wrap;gap:.3rem;padding:.3rem;background:var(--sunk);
 border-radius:.65rem;margin-bottom:1.8rem;width:fit-content;max-width:100%}
nav.tabs a{font-size:.86rem;text-decoration:none;color:var(--muted);padding:.42rem .85rem;
 border-radius:.45rem;white-space:nowrap;font-weight:520}
nav.tabs a:hover{color:var(--ink)}
nav.tabs a.on{background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}

h2{font-size:.78rem;margin:2.2rem 0 .8rem;font-weight:660;text-transform:uppercase;
 letter-spacing:.08em;color:var(--muted);display:flex;align-items:center;gap:.5rem}
h2:first-child{margin-top:0}
h2 .count{font-family:var(--mono);font-weight:500;text-transform:none;letter-spacing:0;
 color:var(--muted);background:var(--sunk);padding:.1rem .45rem;border-radius:.3rem;font-size:.75rem}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:.6rem}
.cell{background:var(--surface);border:1px solid var(--line);border-radius:.6rem;
 padding:.85rem .95rem;box-shadow:var(--shadow)}
.cell dt{font-size:.72rem;letter-spacing:.02em;color:var(--muted);margin:0 0 .45rem;font-weight:520}
.cell dd{margin:0;font-size:1.6rem;font-weight:700;line-height:1.05;letter-spacing:-.03em;
 font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:.4rem}
.cell dd small{font-size:.75rem;font-weight:500;color:var(--muted);letter-spacing:0}
.cell.bad dd{color:var(--crit)} .cell.good dd{color:var(--ok)} .cell.warn dd{color:var(--warn)}
.cell.bad{border-color:color-mix(in srgb,var(--crit) 32%,var(--line))}
.cell.good{border-color:color-mix(in srgb,var(--ok) 28%,var(--line))}

.note{font-size:.8rem;color:var(--muted);margin:.6rem 0 0;line-height:1.5}
.note strong{color:var(--soft);font-weight:600}

.caps{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:.6rem}
.cap{background:var(--surface);border:1px solid var(--line);border-radius:.6rem;padding:.75rem .9rem;
 box-shadow:var(--shadow)}
.cap b{display:block;font-size:.82rem;font-weight:600;margin-bottom:.1rem}
.cap span{font-family:var(--mono);font-size:.72rem;color:var(--muted)}
.pips{display:flex;gap:.25rem;margin-top:.5rem}
.pips i{flex:1;height:.4rem;border-radius:.2rem;background:var(--sunk)}
.pips i.f{background:var(--accent)}

.chart{display:flex;align-items:flex-end;gap:.45rem;background:var(--surface);border:1px solid var(--line);
 border-radius:.6rem;padding:1rem .9rem .6rem;box-shadow:var(--shadow);overflow-x:auto}
.chart .col{flex:1;min-width:2.1rem;display:flex;flex-direction:column;align-items:center;gap:.3rem}
.chart .v{font-family:var(--mono);font-size:.7rem;color:var(--muted);font-variant-numeric:tabular-nums}
.chart .bar{width:100%;background:var(--accent);border-radius:.25rem .25rem 0 0;min-height:.2rem}
.chart .d{font-family:var(--mono);font-size:.66rem;color:var(--muted);white-space:nowrap}

.tw{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:.6rem;
 box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{text-align:left;padding:.62rem .85rem;border-bottom:1px solid var(--line-2);vertical-align:middle}
th{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
 white-space:nowrap;font-weight:620;background:var(--raise)}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
td.z{color:var(--muted)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--raise)}
.runway{display:inline-block;height:.35rem;border-radius:.2rem;background:var(--accent);
 vertical-align:middle;min-width:.2rem}

.list{display:flex;flex-direction:column;gap:.4rem}
details{background:var(--surface);border:1px solid var(--line);border-radius:.6rem;
 box-shadow:var(--shadow);overflow:hidden}
details[open]{border-color:color-mix(in srgb,var(--accent) 35%,var(--line))}
summary{padding:.7rem .9rem;cursor:pointer;list-style:none;display:flex;gap:.75rem;align-items:flex-start}
summary::-webkit-details-marker{display:none}
summary:hover{background:var(--raise)}
.av{flex:none;width:2rem;height:2rem;border-radius:.45rem;background:var(--accent-soft);color:var(--accent);
 display:grid;place-items:center;font-weight:700;font-size:.85rem}
.sm{min-width:0;flex:1}
.l1{display:flex;flex-wrap:wrap;gap:.4rem;align-items:baseline}
.l1 strong{font-weight:620;font-size:.92rem}
.l1 .addr{font-family:var(--mono);font-size:.75rem;color:var(--muted);word-break:break-all}
.l2{font-size:.87rem;color:var(--soft);margin-top:.15rem;overflow:hidden;text-overflow:ellipsis}
.when{flex:none;font-family:var(--mono);font-size:.72rem;color:var(--muted);white-space:nowrap;
 padding-top:.15rem}
pre{margin:0;padding:.9rem 1.1rem 1.2rem 3.6rem;white-space:pre-wrap;word-break:break-word;
 font:13px/1.7 var(--mono);color:var(--soft);border-top:1px solid var(--line-2);background:var(--raise)}

.tag{font-size:.66rem;text-transform:uppercase;letter-spacing:.05em;font-weight:640;
 padding:.12rem .4rem;border-radius:.28rem;background:var(--sunk);color:var(--muted);white-space:nowrap}
.tag.ok{background:var(--ok-soft);color:var(--ok)}
.tag.hot{background:var(--crit-soft);color:var(--crit)}
.tag.w{background:var(--warn-soft);color:var(--warn)}
.tag.lead{background:var(--accent);color:var(--surface)}

form.search{display:flex;gap:.4rem;margin:0 0 .8rem}
input[type=search]{flex:1;padding:.55rem .8rem;border:1px solid var(--line);background:var(--surface);
 color:var(--ink);border-radius:.5rem;font-size:.9rem;font-family:var(--sans)}
input[type=search]:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
button{padding:.55rem 1.1rem;border:1px solid var(--accent);background:var(--accent);color:var(--surface);
 border-radius:.5rem;font-size:.88rem;font-weight:600;cursor:pointer;font-family:var(--sans)}
nav.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin:0 0 .9rem}
nav.chips a{font-size:.8rem;text-decoration:none;color:var(--muted);border:1px solid var(--line);
 padding:.3rem .7rem;border-radius:1rem;background:var(--surface);white-space:nowrap}
nav.chips a.on{background:var(--accent);color:var(--surface);border-color:var(--accent);font-weight:600}

.err{border:1px solid var(--crit);background:var(--crit-soft);color:var(--crit);padding:.9rem 1.1rem;
 border-radius:.6rem;margin:1rem 0;font-size:.9rem}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:.6rem;padding:2rem;
 text-align:center;color:var(--muted);font-size:.9rem}
footer{margin-top:3.5rem;padding-top:1.2rem;border-top:1px solid var(--line);
 font-size:.76rem;color:var(--muted);line-height:1.7}
@media (max-width:34rem){
  body{padding:0 .8rem 4rem}
  .cell dd{font-size:1.35rem}
  .when{display:none}
}
"""


def page(body, active="", refreshed=""):
    tabs = [("/", "home", "Přehled"), ("/odeslane", "out", "Odeslané"),
            ("/prijate", "in", "Přijaté"), ("/ceka", "wait", "Čeká na tebe"),
            ("/doruceni", "log", "Doručování")]
    nav = "".join(f'<a href="{href}" class="{"on" if active == key else ""}">{label}</a>'
                  for href, key, label in tabs)
    return f"""<!doctype html>
<html lang="cs"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Lokwave outreach</title>
<style>{CSS}</style></head><body><div class="wrap">
<header>
  <div class="brand"><span class="dot"></span><h1>Lokwave outreach</h1></div>
  <div class="meta">{E(refreshed)} &nbsp;·&nbsp; 6 značek &nbsp;·&nbsp; strop {DAILY_CAP}/den/značku</div>
</header>
<nav class="tabs">{nav}</nav>
{body}
<footer>Čte přímo z produkčních databází — žádná mezivrstva, žádná zastaralá kopie.<br>
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
    leads = prospect_senders()

    open_pct = pct(f["opened"], f["sent"])
    click_pct = pct(f["clicked"], f["sent"])

    b = ['<h2>Trychtýř</h2><dl class="grid">']
    b.append(cell("Auditů", f["audits"]))
    b.append(cell("Odesláno", f["sent"]))
    b.append(cell("Otevřeno", f["opened"], f"{open_pct} %", "good" if open_pct >= 20 else ""))
    b.append(cell("Prokliků", f["clicked"], f"{click_pct} %", "good" if num(f["clicked"]) else "bad"))
    b.append(cell("Odpovědí od oslovených", len(leads), "", "good" if leads else "bad"))
    b.append(cell("Registrací", f["orgs"]))
    b.append(cell("Poboček", f["locations"]))
    b.append(cell("Platících", f["paying"], f"+{f['trialing']} ve zkušební",
                  "good" if num(f["paying"]) else "bad"))
    b.append("</dl>")

    notes = []
    if num(f["ghosts"]):
        notes.append(f"<strong>{f['ghosts']}</strong> záznamů má stav „odesláno“, ale prázdné "
                     "<code>sent_at</code> i tělo — pozůstatek staršího odesílání. "
                     "Do čísel výše se nepočítají.")
    notes.append("„Platících“ = jen předplatné ve stavu <code>active</code>. Zkušební verze "
                 "ani nezaplacené faktury se za tržbu nepovažují.")
    b.append('<p class="note">' + "<br>".join(notes) + "</p>")

    total_today = sum(today.values())
    cap_total = DAILY_CAP * len(BRANDS)
    b.append(f'<h2>Dnes odesláno <span class="count">{total_today} z {cap_total}</span></h2>')
    b.append('<div class="caps">')
    for br in BRANDS:
        n = today.get(br, 0)
        pips = "".join(f'<i class="{"f" if i < n else ""}"></i>' for i in range(DAILY_CAP))
        b.append(f'<div class="cap"><b>{E(BRAND_LABEL[br])}</b>'
                 f'<span>{n} z {DAILY_CAP}</span><div class="pips">{pips}</div></div>')
    b.append("</div>")

    if series:
        peak = max(n for _, n in series) or 1
        cols = "".join(
            f'<div class="col"><span class="v">{n}</span>'
            f'<span class="bar" style="height:{max(3, round(72 * n / peak))}px"></span>'
            f'<span class="d">{E(d)}</span></div>'
            for d, n in series)
        b.append(f'<h2>Odesílání za posledních 14 dní</h2><div class="chart">{cols}</div>')

    b.append('<h2>Fronta podle značky</h2><div class="tw"><table><thead><tr><th>Značka</th>'
             "<th>Čeká</th><th>Kvalifikováno</th><th>Odesláno</th><th>Nedosažitelných</th>"
             "<th>Chyb</th><th>Zásoba</th></tr></thead><tbody>")
    max_runway = max((pipe.get(br, {}).get("pending", 0) for br in BRANDS), default=0) or 1
    for br in BRANDS:
        row = pipe.get(br, {})
        pending = row.get("pending", 0)
        days = round(pending / DAILY_CAP) if pending else 0
        width = round(90 * pending / max_runway) if pending else 0

        def td(v):
            return f'<td class="n{"" if v else " z"}">{v}</td>'
        b.append(
            f"<tr><td><strong>{E(BRAND_LABEL[br])}</strong></td>"
            + td(pending) + td(row.get("qualified", 0)) + td(row.get("sent", 0))
            + td(row.get("unreachable", 0)) + td(row.get("failed", 0))
            + f'<td class="n"><span class="runway" style="width:{width}px"></span> {days} d</td></tr>')
    b.append("</tbody></table></div>")
    b.append('<p class="note">„Nedosažitelných“ = firma nemá web nebo se nepodařilo najít e-mail; '
             "to není chyba systému, jen přirozený odpad při prospektování. Sloupec „Chyb“ jsou "
             "skutečná selhání odeslání.</p>")

    variants = subject_variants()
    VLABEL = {"gap": "Nedostatek z auditu", "rival": "Srovnání s konkurencí"}
    b.append("<h2>Varianty předmětu</h2>")
    if not variants:
        b.append('<div class="empty">Zatím nic — měření začíná dalším odeslaným e-mailem.</div>')
    else:
        b.append('<div class="tw"><table><thead><tr><th>Strategie</th><th>Odesláno</th>'
                 "<th>Otevřeno</th><th>Prokliků</th></tr></thead><tbody>")
        for key, sent, opened, clicked in variants:
            n = num(sent)
            b.append(f"<tr><td><strong>{E(VLABEL.get(key, key))}</strong></td>"
                     f'<td class="n">{n}</td>'
                     f'<td class="n">{opened} <span class="tag">{pct(opened, n)} %</span></td>'
                     f'<td class="n">{clicked} <span class="tag">{pct(clicked, n)} %</span></td></tr>')
        b.append("</tbody></table></div>")
        total = sum(num(v[1]) for v in variants)
        if total < 100:
            b.append(f'<p class="note">Zatím {total} e-mailů — na závěr je to málo. '
                     "Rozdíl pod několika stovkami odeslaných je šum, ne výsledek.</p>")

    sc = selfcheck()
    b.append("<h2>Denní kontrola peněžní cesty</h2>")
    if not sc:
        b.append('<div class="empty">Kontrola zatím neproběhla.</div>')
    else:
        stale = fmt_dt(sc.get("checkedAt"))
        b.append('<dl class="grid">')
        for name, r in (sc.get("results") or {}).items():
            ok = r.get("ok")
            cls = "good" if ok is True else "bad" if ok is False else ""
            value = "OK" if ok is True else "chyba" if ok is False else "—"
            b.append(cell(name, value, "", cls))
        b.append("</dl>")
        b.append(f'<p class="note">Naposledy {E(stale)} UTC. Běží každé ráno; '
                 "při selhání odejde e-mail. " + E("; ".join(
                     f"{k}: {v.get('msg','')}" for k, v in (sc.get("results") or {}).items())) + "</p>")

    b.append("<h2>Kontrolky</h2><dl class='grid'>")
    for label, value, bad in health():
        b.append(cell(label, value, "", "bad" if bad else "good"))
    b.append("</dl>")
    return "".join(b)


def search_form(placeholder, value, hidden=""):
    return (f'<form class="search" method="get">{hidden}'
            f'<input type="search" name="q" placeholder="{E(placeholder)}" value="{E(value or "")}">'
            f"<button>Hledat</button></form>")


def render_outbound(brand=None, search=None):
    rows, total = outbound(brand=brand, search=search)
    shown = f'{len(rows)} z {total}' if total > len(rows) else str(total)
    b = [f'<h2>Odeslané e-maily <span class="count">{shown}</span></h2>']
    hidden = f'<input type="hidden" name="brand" value="{E(brand)}">' if brand else ""
    b.append(search_form("Hledat ve jménu, adrese, městě, předmětu i textu…", search, hidden))
    qs = f"&q={E(search)}" if search else ""
    b.append('<nav class="chips"><a href="/odeslane?x=1' + qs + '" class="'
             + ("on" if not brand else "") + '">Vše</a>'
             + "".join(f'<a href="/odeslane?brand={br}{qs}" class="{"on" if brand == br else ""}">'
                       f"{BRAND_LABEL[br]}</a>" for br in BRANDS) + "</nav>")
    if not rows:
        b.append('<div class="empty">Nic neodpovídá.</div>')
        return "".join(b)

    b.append('<div class="list">')
    for sent_at, vert, name, email, subject, body, opened, clicked, city, score in rows:
        tags = [f'<span class="tag">{E(BRAND_LABEL.get(vert, vert))}</span>']
        if score:
            tags.append(f'<span class="tag">skóre {E(score)}</span>')
        if opened == "t":
            tags.append('<span class="tag ok">otevřel</span>')
        if clicked == "t":
            tags.append('<span class="tag lead">proklik</span>')
        b.append(
            "<details><summary>"
            f'<span class="av">{initial(name)}</span><span class="sm">'
            f'<span class="l1"><strong>{E(name)}</strong>'
            + (f'<span class="tag">{E(city)}</span>' if city else "")
            + f'<span class="addr">{E(email)}</span>{"".join(tags)}</span>'
            f'<span class="l2">{E(subject)}</span></span>'
            f'<span class="when">{E(fmt_dt(sent_at))}</span></summary>'
            f"<pre>{E(body) if body else '(tělo se neuložilo)'}</pre></details>")
    b.append("</div>")
    return "".join(b)


def render_inbound(search=None):
    rows, total = inbound(search=search)
    leads = prospect_senders()
    shown = f'{len(rows)} z {total}' if total > len(rows) else str(total)
    b = [f'<h2>Přijaté e-maily <span class="count">{shown}</span></h2>']
    b.append(search_form("Hledat v odesílateli, předmětu i textu…", search))
    if leads:
        b.append('<p class="note">Odpovědi od firem, které jsme oslovili, jsou označené štítkem '
                 "<strong>PROSPEKT</strong> — ty jsou jediné, které stojí za pozornost.</p>")
    if not rows:
        b.append('<div class="empty">Nic neodpovídá.</div>')
        return "".join(b)

    b.append('<div class="list">')
    for received, frm, frm_name, subject, text, replied, starred, archived, auto, mailbox in rows:
        tags = []
        lead = leads.get((frm or "").lower())
        if lead:
            tags.append('<span class="tag lead">prospekt</span>')
            tags.append(f'<span class="tag">{E(BRAND_LABEL.get(lead["vertical"], lead["vertical"]))}</span>')
        elif mailbox:
            tags.append(f'<span class="tag">{E(mailbox.split("@")[-1])}</span>')
        if replied == "t":
            tags.append('<span class="tag ok">bot odpověděl</span>')
        elif starred == "t":
            tags.append('<span class="tag w">čeká na tebe</span>')
        elif archived == "t":
            tags.append('<span class="tag">odloženo</span>')
        else:
            tags.append('<span class="tag hot">nevyřízeno</span>')
        if auto:
            tags.append('<span class="tag">automat</span>')
        who = frm_name or (lead["name"] if lead else "") or frm
        b.append(
            "<details><summary>"
            f'<span class="av">{initial(who)}</span><span class="sm">'
            f'<span class="l1"><strong>{E(who)}</strong>'
            f'<span class="addr">{E(frm)}</span>{"".join(tags)}</span>'
            f'<span class="l2">{E(subject) or "(bez předmětu)"}</span></span>'
            f'<span class="when">{E(fmt_dt(received))}</span></summary>'
            f"<pre>{E(text) if text else '(prázdné)'}</pre></details>")
    b.append("</div>")
    return "".join(b)


def render_waiting():
    rows = waiting()
    leads = prospect_senders()
    b = [f'<h2>Čeká na tebe <span class="count">{len(rows)}</span></h2>']
    if not rows:
        b.append('<div class="empty">Nic nečeká — bot vyřídil všechno sám.</div>')
        return "".join(b)
    b.append('<p class="note">Bot se u těchhle zpráv <strong>záměrně zastavil</strong> a nechal je '
             "na tebe: odesílatele nedokázal spárovat s osloveným podnikem, nebo šlo o citlivou "
             "žádost (smazání údajů, právní dotaz), kterou automat řešit nemá.</p>")
    b.append('<div class="list">')
    for received, frm, frm_name, subject, text, mailbox in rows:
        lead = leads.get((frm or "").lower())
        tags = []
        if lead:
            tags.append('<span class="tag lead">prospekt</span>')
        if mailbox:
            tags.append(f'<span class="tag">{E(mailbox)}</span>')
        who = frm_name or (lead["name"] if lead else "") or frm
        b.append(
            "<details open><summary>"
            f'<span class="av">{initial(who)}</span><span class="sm">'
            f'<span class="l1"><strong>{E(who)}</strong>'
            f'<span class="addr">{E(frm)}</span>{"".join(tags)}</span>'
            f'<span class="l2">{E(subject) or "(bez předmětu)"}</span></span>'
            f'<span class="when">{E(fmt_dt(received))}</span></summary>'
            f"<pre>{E(text) if text else '(prázdné)'}</pre></details>")
    b.append("</div>")
    return "".join(b)


def render_log():
    rows = delivery_log()
    ok = sum(1 for r in rows if r[3] in ("sent", "delivered", "queued"))
    b = [f'<h2>Doručování (LaunchMail) <span class="count">{ok} z {len(rows)} v pořádku</span></h2>'
         '<div class="tw"><table><thead><tr>'
         "<th>Čas</th><th>Příjemce</th><th>Předmět</th><th>Stav</th><th>Chyba</th>"
         "</tr></thead><tbody>"]
    for created, to, subject, status, error in rows:
        try:
            addr = ", ".join(x.get("email", "") for x in json.loads(to))
        except Exception:
            addr = to
        good = status in ("sent", "delivered", "queued")
        pill = f'<span class="tag {"ok" if good else "hot"}">{E(status)}</span>'
        b.append(f'<tr><td class="n">{E(fmt_dt(created))}</td><td class="n">{E(addr)}</td>'
                 f"<td>{E(subject[:70])}</td><td>{pill}</td>"
                 f'<td class="z">{E(error[:70])}</td></tr>')
    b.append("</tbody></table></div>")
    return "".join(b)


ROUTES = {"/": ("home", render_home), "/odeslane": ("out", render_outbound),
          "/prijate": ("in", render_inbound), "/ceka": ("wait", render_waiting),
          "/doruceni": ("log", render_log)}


class Handler(BaseHTTPRequestHandler):
    server_version = "lokwave-outreach"

    def log_message(self, *_):  # keep the journal for real events only
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send(200, "ok", "text/plain")
        route = ROUTES.get(parsed.path)
        if not route:
            return self._send(404, page('<div class="empty">Stránka neexistuje.</div>',
                                        refreshed=stamp()), "text/html")

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
            import traceback
            traceback.print_exc()
            body = (f'<div class="err">Neočekávaná chyba: {E(type(err).__name__)}: '
                    f"{E(str(err)[:200])}</div>")
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
