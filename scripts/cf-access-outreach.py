#!/usr/bin/env python3
"""Put outreach.lokwave.cz behind Cloudflare Access — owner emails only.

This page lists every email we have sent to a named business and every reply
received, so it must never be reachable without authentication. Unlike the
Postiz app there is NO bypass path: nothing here is meant to be fetched
anonymously, so the whole hostname is gated.

Access is the only gate the dashboard has — the service itself trusts the edge
and checks nothing — so this script failing is not cosmetic.
"""
import json, pathlib, urllib.request, urllib.error

# The general zone token cannot touch Access (auth.forbidden); Access apps need
# the dedicated Zero Trust token.
TOKEN = pathlib.Path("/srv/homelab/secrets/cloudflare-access-token.txt").read_text().strip()
ACCOUNT = "fbf7f01b479d7b0ccde31d25115da320"
API = "https://api.cloudflare.com/client/v4"
DOMAIN = "outreach.lokwave.cz"
EMAILS = ["danila.s.anikin@gmail.com", "danakin1050@gmail.com"]


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return json.load(e)


apps = call("GET", f"/accounts/{ACCOUNT}/access/apps")
existing = next((a for a in (apps.get("result") or []) if a.get("domain") == DOMAIN), None)

body = {
    "name": "Lokwave outreach dashboard",
    "domain": DOMAIN,
    "type": "self_hosted",
    "session_duration": "24h",
    "app_launcher_visible": False,
    "policies": [{
        "name": "owner-only",
        "decision": "allow",
        "include": [{"email": {"email": e}} for e in EMAILS],
    }],
}

if existing:
    res = call("PUT", f"/accounts/{ACCOUNT}/access/apps/{existing['id']}", body)
    action = "updated"
else:
    res = call("POST", f"/accounts/{ACCOUNT}/access/apps", body)
    action = "created"

ok = res.get("success")
print(f"Access app {action}: success={ok}" + ("" if ok else f" errors={str(res.get('errors'))[:200]}"))
