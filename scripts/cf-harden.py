#!/usr/bin/env python3
"""Cloudflare WAF hardening veřejných zón (free tier).
- Nasadí Cloudflare Managed Free Ruleset (WAF) do každé zóny.
- Rate-limit na SIGNUP endpointy (ne token — chrání SSR hairpin) u zón se Supabase auth.
Bezpečné: managed ruleset má nízké false-positive; signup se přes SSR neopakuje.
"""
import json, urllib.request, urllib.error
from pathlib import Path

TOKEN = Path("/home/anakin/programming/homelab/secrets/cloudflare-api-token.txt").read_text().strip()
API = "https://api.cloudflare.com/client/v4"
MANAGED_FREE = "77454fe2d30c4220b5701f6fdfb893ba"
WAF_ZONES = ["anikin.cz","freio.cz","ripieno.xyz","lokwave.cz","dentallocal.cz",
             "autolocal.cz","vetlocal.cz","bistrolocal.cz","salonlocal.cz","fitlocal.cz"]
RL_ZONES = ["anikin.cz","freio.cz"]   # zóny se Supabase auth (/auth/v1/signup)

def call(m,p,b=None):
    d=json.dumps(b).encode() if b is not None else None
    r=urllib.request.Request(API+p,data=d,method=m,
        headers={"Authorization":f"Bearer {TOKEN}","Content-Type":"application/json"})
    try: return json.loads(urllib.request.urlopen(r,timeout=30).read())
    except urllib.error.HTTPError as e: return json.loads(e.read())

def zid(name):
    r=call("GET",f"/zones?name={name}")
    return r["result"][0]["id"] if r.get("result") else None

def deploy_waf(z):
    body={"rules":[{"action":"execute","action_parameters":{"id":MANAGED_FREE},
          "expression":"true","description":"Cloudflare Managed Free Ruleset (WAF)","enabled":True}]}
    r=call("PUT",f"/zones/{z}/rulesets/phases/http_request_firewall_managed/entrypoint",body)
    return r.get("success"), r.get("errors")

def deploy_ratelimit(z):
    body={"rules":[{"action":"block",
          "ratelimit":{"characteristics":["ip.src","cf.colo.id"],"period":10,
                       "requests_per_period":15,"mitigation_timeout":10},
          "expression":'(http.request.uri.path contains "/auth/v1/signup")',
          "description":"signup abuse limit (safe: SSR neopakuje signup)","enabled":True}]}
    r=call("PUT",f"/zones/{z}/rulesets/phases/http_ratelimit/entrypoint",body)
    return r.get("success"), r.get("errors")

print("=== WAF (Managed Free Ruleset) ===")
for name in WAF_ZONES:
    z=zid(name)
    if not z: print(f"  {name}: zóna nenalezena"); continue
    ok,err=deploy_waf(z)
    print(f"  {name}: {'✓ WAF nasazen' if ok else '✗ '+str(err)}")

print("=== Rate-limit (signup) ===")
for name in RL_ZONES:
    z=zid(name)
    ok,err=deploy_ratelimit(z)
    print(f"  {name}: {'✓ signup rate-limit' if ok else '✗ '+str(err)}")
