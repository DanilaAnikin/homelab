#!/usr/bin/env python3
"""Cloudflare onboarding pro homelab domény.
Fáze 1 (add-zones): přidá zóny do CF a vypíše přidělené nameservery (přehodit na WEDOSu).
Fáze 2 (wire <domain>): po aktivaci zóny vytvoří DNS + tunel public-hostname na homelab.

Token: homelab/secrets/cloudflare-api-token.txt  (Account.Zone:Edit, Zone.DNS:Edit, Account.Cloudflare Tunnel:Edit)
"""
import sys, json, urllib.request, urllib.error
from pathlib import Path

TOKEN = Path("/home/anakin/programming/homelab/secrets/cloudflare-api-token.txt").read_text().strip()
TUNNEL_ID = "215c5edb-467c-470a-9e34-1d46e65fcfef"
TUNNEL_CNAME = f"{TUNNEL_ID}.cfargotunnel.com"
API = "https://api.cloudflare.com/client/v4"

# Živé/e-mailové domény: DNS se replikuje, apex NEsměrovat na homelab bez cutoveru.
LIVE = {"dentallocal.cz"}
SCAFFOLD = ["vetlocal.cz","salonlocal.cz","bistrolocal.cz","fitlocal.cz","autolocal.cz","lokwave.cz"]

def call(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())

def account_id():
    # Token nemá Account Settings:Read → /accounts vrací prázdno; vezmi z existující zóny.
    r = call("GET", "/accounts")
    if r.get("result"):
        return r["result"][0]["id"]
    z = call("GET", "/zones?per_page=1")
    return z["result"][0]["account"]["id"]

def add_zone(name, acct):
    r = call("POST", "/zones", {"name": name, "account": {"id": acct}, "type": "full"})
    if not r.get("success"):
        # už existuje?
        z = call("GET", f"/zones?name={name}")
        if z.get("result"):
            res = z["result"][0]
            return res["id"], res.get("name_servers", [])
        return None, r.get("errors")
    res = r["result"]
    return res["id"], res.get("name_servers", [])

def zone_id(name):
    z = call("GET", f"/zones?name={name}")
    return z["result"][0]["id"] if z.get("result") else None

def upsert_dns(zid, rtype, name, content, proxied):
    ex = call("GET", f"/zones/{zid}/dns_records?type={rtype}&name={name}")
    body = {"type": rtype, "name": name, "content": content, "proxied": proxied, "ttl": 1}
    if ex.get("result"):
        rid = ex["result"][0]["id"]
        return call("PUT", f"/zones/{zid}/dns_records/{rid}", body)
    return call("POST", f"/zones/{zid}/dns_records", body)

def delete_conflicts(zid, name):
    """Smaž existující A/AAAA/CNAME na daném jménu (kolidují s novým CNAME->tunel)."""
    for rtype in ("A", "AAAA", "CNAME"):
        ex = call("GET", f"/zones/{zid}/dns_records?type={rtype}&name={name}")
        for r in ex.get("result") or []:
            call("DELETE", f"/zones/{zid}/dns_records/{r['id']}")

def tunnel_config(acct):
    return call("GET", f"/accounts/{acct}/cfd_tunnel/{TUNNEL_ID}/configurations")

def set_tunnel_config(acct, cfg):
    return call("PUT", f"/accounts/{acct}/cfd_tunnel/{TUNNEL_ID}/configurations", {"config": cfg})

def add_ingress(acct, hostnames):
    r = tunnel_config(acct)
    cfg = (r.get("result") or {}).get("config") or {"ingress": []}
    ingress = [i for i in cfg.get("ingress", []) if i.get("hostname") not in hostnames]
    catch = [i for i in ingress if not i.get("hostname")]
    keep = [i for i in ingress if i.get("hostname")]
    for h in hostnames:
        keep.append({"hostname": h, "service": "http://localhost:80"})
    if not catch:
        catch = [{"service": "http_status:404"}]
    cfg["ingress"] = keep + catch
    return set_tunnel_config(acct, cfg)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    acct = account_id()
    print("account:", acct)
    if cmd == "add-zones":
        for d in SCAFFOLD + list(LIVE):
            zid, ns = add_zone(d, acct)
            print(f"\n{d}\n  zone_id: {zid}\n  NASTAV NA WEDOSU nameservery: {ns}")
    elif cmd == "wire":
        d = sys.argv[2]
        zid = zone_id(d)
        print(f"{d} zone_id={zid}")
        if d in LIVE:
            print("  ŽIVÁ doména — apex/www ponecháno na Vercelu, jen DNS host na CF. Cutover později.")
        else:
            delete_conflicts(zid, d)
            delete_conflicts(zid, f"www.{d}")
            print(upsert_dns(zid, "CNAME", d, TUNNEL_CNAME, True).get("success"), "apex->tunnel")
            print(upsert_dns(zid, "CNAME", f"www.{d}", TUNNEL_CNAME, True).get("success"), "www->tunnel")
            print(add_ingress(acct, [d, f"www.{d}"]).get("success"), "tunnel ingress")
    else:
        print("usage: cf-onboard.py add-zones | wire <domain>")
