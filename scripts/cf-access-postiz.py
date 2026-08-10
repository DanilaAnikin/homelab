#!/usr/bin/env python3
# Založí Cloudflare Access ochranu pro postiz.freio.cz:
#  - app "Postiz-uploads" (postiz.freio.cz/uploads) → BYPASS (veřejné, pro sociální sítě)
#  - app "Postiz" (postiz.freio.cz)                 → ALLOW jen owner e-maily (login přes e-mail OTP)
import json, urllib.request, sys, pathlib
TOK = pathlib.Path("/srv/homelab/secrets/freio-cloudflare-full-token.txt").read_text().strip()
ACC = "fbf7f01b479d7b0ccde31d25115da320"
API = "https://api.cloudflare.com/client/v4"
EMAILS = ["danila.s.anikin@gmail.com", "danakin1050@gmail.com"]

def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def find_app(domain):
    r = call("GET", f"/accounts/{ACC}/access/apps")
    for a in (r.get("result") or []):
        if a.get("domain") == domain:
            return a.get("id")
    return None

def ensure_app(name, domain, policies):
    existing = find_app(domain)
    body = {"name": name, "domain": domain, "type": "self_hosted",
            "session_duration": "24h", "app_launcher_visible": False,
            "policies": policies}
    if existing:
        r = call("PUT", f"/accounts/{ACC}/access/apps/{existing}", body)
        action = "aktualizována"
    else:
        r = call("POST", f"/accounts/{ACC}/access/apps", body)
        action = "vytvořena"
    ok = r.get("success")
    print(f"[{name}] {domain} → {action}: success={ok}" + ("" if ok else f" ERR={r.get('errors')}"))
    return r.get("result", {}).get("id") if ok else None

# 1) /uploads veřejně (bypass) — vyšší specificita cesty má přednost
ensure_app("Postiz uploads (public)", "postiz.freio.cz/uploads",
           [{"name": "public-uploads", "decision": "bypass",
             "include": [{"everyone": {}}]}])

# 2) zbytek → jen owner e-maily (login e-mail OTP)
ensure_app("Postiz", "postiz.freio.cz",
           [{"name": "owner-only", "decision": "allow",
             "include": [{"email": {"email": e}} for e in EMAILS]}])
