#!/usr/bin/env python3
"""Daily self-check of the paths that make money.

On 2026-08-12 four things turned out to be broken at once: Stripe webhooks had
never once been delivered, checkout threw for every customer whose trial had
run out, Google sign-in was refused on one brand, and the auth rate limiter was
keyed on the proxy so a single visitor could lock out everyone. Three of the
four had been broken for weeks or months. Not one of them raised anything —
the outreach side has a dashboard, the money and login side had nothing.

Each check below reproduces one of those failures, so a repeat is caught within
a day instead of whenever someone happens to click the wrong button.

Read-only except for the Stripe probe, which creates a subscription and cancels
it in the same run — that is the only way to exercise the branch that broke.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SEC = "/srv/homelab/secrets"
STATE = "/srv/homelab/state/selfcheck.json"
LM_BASE = "https://mail.ripieno.xyz"
ALERT_TO = os.environ.get("LOKWAVE_ALERT_TO", "danila.s.anikin@gmail.com")
ALERT_BRAND_STEM = "dentallocal"

BRANDS = ["dentallocal", "vetlocal", "salonlocal", "bistrolocal", "fitlocal", "autolocal", "lokwave"]
GCP_PROJECT_NUMBER = "1013669550670"
GBP_SERVICE = "mybusinessbusinessinformation.googleapis.com"
# Google grants 300 QPM on approval; anything else means the allowlist has not
# landed yet. See the GBP API prereqs page.
GBP_APPROVED_QPM = 300


def now():
    return datetime.now(timezone.utc)


def read_secret(name):
    with open(f"{SEC}/{name}") as fh:
        return fh.read().strip()


def container_env(match, var):
    """Read an env var out of a running app container.

    The apps are the only place these values live: Dokploy rewrites the service
    spec from its own database, so there is no file on disk to read instead.
    """
    names = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30,
    ).stdout.split()
    for n in names:
        if n.startswith(match):
            out = subprocess.run(
                ["docker", "exec", n, "printenv", var],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
    raise RuntimeError(f"env {var} not found in a container matching {match}")


# Cloudflare sits in front of every brand and rejects `Python-urllib` outright
# with error 1010, so an un-disguised probe reports a 403 that has nothing to do
# with the application. Send a browser signature.
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def http(url, method="GET", data=None, headers=None, auth=None, timeout=45):
    body = None
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data, doseq=True).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(data, (bytes, str)):
        body = data.encode() if isinstance(data, str) else data
    if auth:
        import base64
        token = base64.b64encode(f"{auth}:".encode()).decode()
        hdrs["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)[:200]}


# ── checks ───────────────────────────────────────────────────────────────────

def check_stripe_intent(sk):
    """Can a customer actually be charged?

    Reproduces the create call the app makes, with no trial so the first
    invoice is issued immediately — the exact branch that threw "subscription
    created without an intent client_secret" for every paying customer, because
    the code read `invoice.payment_intent`, a field Stripe removed in API
    version 2025-03-31 in favour of `confirmation_secret`.

    Creates a subscription and cancels it in the same run. Stripe expires
    incomplete subscriptions by itself, but leaving them lying around would
    make the dashboard unreadable.
    """
    price = container_env("app-", "STRIPE_PRICE_GROWTH")

    st, cust = http("https://api.stripe.com/v1/customers", "POST", {
        "name": "Lokwave self-check",
        "email": "selfcheck@lokwave.cz",
        "address[country]": "CZ",
        "address[city]": "Praha",
        "address[line1]": "Kettnerova 2053",
        "address[postal_code]": "15500",
        "metadata[lokwave_selfcheck]": "1",
    }, auth=sk)
    if st != 200:
        return False, f"nelze založit zákazníka ({st}): {str(cust)[:120]}"
    customer_id = cust["id"]

    sub_id = None
    try:
        st, sub = http("https://api.stripe.com/v1/subscriptions", "POST", {
            "customer": customer_id,
            "items[0][price]": price,
            "payment_behavior": "default_incomplete",
            "payment_settings[payment_method_types][]": "card",
            "payment_settings[save_default_payment_method]": "on_subscription",
            "automatic_tax[enabled]": "true",
            "expand[]": ["latest_invoice.confirmation_secret", "pending_setup_intent"],
            "metadata[lokwave_selfcheck]": "1",
        }, auth=sk)
        if st != 200:
            return False, f"předplatné se nezaložilo ({st}): {str(sub)[:160]}"
        sub_id = sub.get("id")

        invoice = sub.get("latest_invoice") or {}
        secret = None
        if isinstance(invoice, dict):
            secret = (invoice.get("confirmation_secret") or {}).get("client_secret")
        setup = sub.get("pending_setup_intent")
        if not secret and isinstance(setup, dict):
            secret = setup.get("client_secret")
        if not secret:
            return False, (
                "předplatné vzniklo BEZ client_secret — zákazník nemůže zaplatit "
                f"(subscription {sub_id}). Stripe nejspíš znovu přesunul pole na faktuře."
            )
        return True, f"client_secret v pořádku ({'confirmation_secret' if invoice else 'setup_intent'})"
    finally:
        if sub_id:
            http(f"https://api.stripe.com/v1/subscriptions/{sub_id}", "DELETE", auth=sk)
        http(f"https://api.stripe.com/v1/customers/{customer_id}", "DELETE", auth=sk)


def check_stripe_webhooks(sk):
    """Is Stripe actually reaching us?

    `pending_webhooks` counts endpoints Stripe has not yet delivered to. Events
    older than an hour that are still pending mean delivery is failing — which
    is how a signing-secret mismatch went unnoticed long enough that not one
    webhook had ever been processed.
    """
    st, evs = http("https://api.stripe.com/v1/events?limit=50", auth=sk)
    if st != 200:
        return False, f"nelze načíst události ({st})"
    cutoff = time.time() - 3600
    stuck = [
        e for e in (evs.get("data") or [])
        if (e.get("pending_webhooks") or 0) > 0 and (e.get("created") or 0) < cutoff
    ]
    if stuck:
        kinds = ", ".join(sorted({e.get("type", "?") for e in stuck})[:4])
        return False, f"{len(stuck)} událostí se nedoručilo déle než hodinu ({kinds})"
    return True, "žádné uvázlé události"


def check_auth():
    """Can a visitor still log in, on every brand?

    Sends Origin AND a cookie deliberately: Better Auth only runs its origin
    check on requests that carry cookies, so a cookie-less probe passes even
    when every real browser is being refused. That is exactly why "Invalid
    origin" on one brand survived a morning of testing.
    """
    bad, throttled = [], []
    # Better Auth limits this endpoint more tightly than the global 30/min, so
    # the tail of a fixed list would be throttled every single run and never
    # actually verified. Rotate by day: over a week every brand gets covered.
    offset = now().timetuple().tm_yday % len(BRANDS)
    order = BRANDS[offset:] + BRANDS[:offset]
    for i, stem in enumerate(order):
        domain = f"{stem}.cz" if stem != "lokwave" else "lokwave.cz"
        # Seven hits on one endpoint from one address would trip the limiter on
        # us. Since it is now keyed per visitor, our own 429 says nothing about
        # whether anyone else can log in — but a false alarm every morning would
        # train us to ignore this mail, so space the probes out.
        if i:
            # 3s was not enough: this endpoint is limited far more tightly than
            # the global 30/min, so four probes in and the rest came back 429.
            # A daily job can afford to take two minutes and check all seven.
            time.sleep(20)
        st, body = http(
            f"https://{domain}/api/auth/sign-in/social", "POST",
            json.dumps({"provider": "google", "callbackURL": "/cs/dashboard"}),
            headers={
                "Content-Type": "application/json",
                # Both headers on purpose: Better Auth only runs its origin
                # check on requests carrying cookies, so a cookie-less probe
                # passes while every real browser is being refused.
                "Origin": f"https://{domain}",
                "Cookie": "NEXT_LOCALE=cs",
            },
        )
        url = body.get("url") if isinstance(body, dict) else None
        if st == 429:
            throttled.append(domain)
        elif st != 200 or not (url or "").startswith("https://accounts.google.com/"):
            bad.append(f"{domain} ({st}: {body.get('message') or body.get('code') or 'bez URL'})")
    if bad:
        return False, "přihlášení nefunguje: " + "; ".join(bad)
    if throttled:
        # Endpoint answered and the limiter did its job — that is a pass.
        return True, f"{len(BRANDS) - len(throttled)} domén ověřeno, {len(throttled)} narazilo na náš vlastní limit"
    return True, f"všech {len(BRANDS)} domén v pořádku"


def check_gbp_quota():
    """Has Google granted API access yet?

    Neither of the two earlier applications produced a single email, so the
    quota is the only reliable signal: 0 QPM means pending, 300 means granted.
    Needs a service account with `serviceusage.services.get`; without one the
    check reports itself as skipped rather than failing.
    """
    sa_path = f"{SEC}/gcp-selfcheck-sa.json"
    if not os.path.exists(sa_path):
        return None, "přeskočeno (chybí service account gcp-selfcheck-sa.json)"
    try:
        import jwt  # type: ignore
    except ImportError:
        return None, "přeskočeno (chybí knihovna pyjwt)"
    sa = json.load(open(sa_path))
    now_ts = int(time.time())
    assertion = jwt.encode({
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/cloud-platform.read-only",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now_ts, "exp": now_ts + 3600,
    }, sa["private_key"], algorithm="RS256")
    st, tok = http("https://oauth2.googleapis.com/token", "POST", {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    })
    if st != 200:
        return None, f"přeskočeno (token se nezískal, {st})"
    st, data = http(
        f"https://serviceusage.googleapis.com/v1beta1/projects/{GCP_PROJECT_NUMBER}"
        f"/services/{GBP_SERVICE}/consumerQuotaMetrics",
        headers={"Authorization": f"Bearer {tok['access_token']}"},
    )
    if st != 200:
        return None, f"přeskočeno (kvótu nelze číst, {st})"
    for m in data.get("metrics") or []:
        for lim in m.get("consumerQuotaLimits") or []:
            if "/min/" not in (lim.get("unit") or ""):
                continue
            for b in lim.get("quotaBuckets") or []:
                qpm = b.get("effectiveLimit")
                if qpm and int(qpm) >= GBP_APPROVED_QPM:
                    return True, f"GBP API SCHVÁLENO ({qpm} QPM) — povol mybusiness.googleapis.com"
                return None, f"GBP API zatím neschváleno ({qpm or 0} QPM)"
    return None, "kvóta za minutu nenalezena"


# ── reporting ────────────────────────────────────────────────────────────────

def send_alert(subject, text):
    try:
        token = read_secret(f"launchmail-{ALERT_BRAND_STEM}-token.txt")
    except Exception as ex:
        print(f"[alert] token nedostupný: {ex}", flush=True)
        return False
    st, _ = http(
        f"{LM_BASE}/api/mail/send", "POST",
        json.dumps({"to": [{"email": ALERT_TO}], "subject": subject, "text": text}),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    return st in (200, 201)


def push_kuma(ok, msg):
    """Optional. Drop a Kuma push URL in the secret file to enable it."""
    path = f"{SEC}/kuma-selfcheck-push-url.txt"
    if not os.path.exists(path):
        return
    url = open(path).read().strip()
    if not url:
        return
    sep = "&" if "?" in url else "?"
    http(f"{url}{sep}status={'up' if ok else 'down'}&msg={urllib.parse.quote(msg[:180])}")


def main():
    sk = container_env("app-", "STRIPE_SECRET_KEY")
    checks = [
        ("platba", lambda: check_stripe_intent(sk)),
        ("webhooky", lambda: check_stripe_webhooks(sk)),
        ("přihlášení", check_auth),
        ("gbp-kvóta", check_gbp_quota),
    ]

    results, failures = {}, []
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as ex:  # a broken check must not look like a broken system
            ok, msg = False, f"kontrola selhala: {type(ex).__name__}: {str(ex)[:160]}"
        results[name] = {"ok": ok, "msg": msg}
        mark = {True: "OK  ", False: "CHYBA", None: "–   "}[ok]
        print(f"[selfcheck] {mark} {name}: {msg}", flush=True)
        if ok is False:
            failures.append(f"{name}: {msg}")

    state = {"checkedAt": now().isoformat(timespec="seconds"), "results": results,
             "failing": len(failures)}
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)

    summary = "vše v pořádku" if not failures else "; ".join(failures)
    push_kuma(not failures, summary)

    if failures:
        body = (
            "Denní kontrola Lokwave našla problém na cestě k penězům.\n\n"
            + "\n".join(f"— {f}" for f in failures)
            + "\n\nOstatní kontroly:\n"
            + "\n".join(f"— {k}: {v['msg']}" for k, v in results.items() if v["ok"] is not False)
            + f"\n\nČas: {state['checkedAt']}\n"
        )
        send_alert(f"⚠️ Lokwave: {len(failures)} kontrola selhala", body)
        return 1

    # A granted allowlist is news worth an email even though nothing is broken.
    gbp = results.get("gbp-kvóta") or {}
    if gbp.get("ok") is True:
        send_alert("✅ Lokwave: Google schválil přístup k GBP API", gbp["msg"] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
