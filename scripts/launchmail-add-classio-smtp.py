#!/usr/bin/env python3
"""
Přidá do LaunchMailu SMTP konfiguraci pro classio@anikin.cz.

Parametry jsou opsané z fungujících značek (Seznam Email Profi): smarthost
smtp.seznam.cz:587 + IMAP imap.seznam.cz:993. `isDefault` zůstává false —
default je contact@freio.cz a měnit ho by znamenalo přesměrovat tam každý
požadavek bez určené schránky.

Heslo se NEPÍŠE do příkazové řádky (bylo by v historii a v `ps`) — čte se ze
souboru /srv/homelab/secrets/launchmail-classio-mailbox.txt.

Použití: add-classio-smtp.py [--apply]
"""
import json, subprocess, sys, urllib.request, urllib.error

NAME = "Classio contact"
FROM = "classio@anikin.cz"
FROM_NAME = "Classio"
PASS_FILE = "/srv/homelab/secrets/launchmail-classio-mailbox.txt"
APPLY = "--apply" in sys.argv


def sudo_read(p):
    return subprocess.run(["sudo", "-n", "cat", p], capture_output=True,
                          text=True, timeout=20).stdout.strip()


PW = sudo_read(PASS_FILE)
if not PW:
    print(f"CHYBA: heslo ke schránce chybí. Ulož ho takto (bez historie):")
    print(f"  printf '%s' 'HESLO' | sudo tee {PASS_FILE} >/dev/null")
    print(f"  sudo chmod 600 {PASS_FILE}")
    sys.exit(1)

token = subprocess.run(
    ["sudo", "-n", "docker", "service", "inspect",
     "app-quantify-solid-state-bandwidth-oslftf", "--format",
     "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}"],
    capture_output=True, text=True, timeout=60).stdout
token = next((l.split("=", 1)[1].strip() for l in token.splitlines()
              if l.startswith("LAUNCHMAIL_API_KEY=")), "")

payload = {
    "name": NAME,
    "host": "smtp.seznam.cz",
    "port": 587,
    "type": "smarthost",
    "username": FROM,
    "password": PW,
    "fromAddress": FROM,
    "fromName": FROM_NAME,
    "isDefault": False,
    "imapHost": "imap.seznam.cz",
    "imapPort": 993,
    "imapUsername": FROM,
    "imapPassword": PW,
    "imapSecure": True,
}

print(f"vytvořím: {NAME}  ({FROM})")
print("  smtp  smtp.seznam.cz:587 (smarthost)")
print("  imap  imap.seznam.cz:993")
print("  isDefault = false")
if not APPLY:
    print("\n[náhled] nic se nezměnilo; ostrý běh: --apply")
    sys.exit(0)


def post(path, body):
    req = urllib.request.Request(
        f"https://mail.ripieno.xyz/api/{path}", method="POST",
        data=json.dumps(body).encode(),
        # Bez vlastního User-Agent vrací Cloudflare 403 (error 1010) — výchozí
        # "Python-urllib" mu neprojde přes ochranu.
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "launchmail-setup/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


code, res = post("smtp-configs", payload)
if code in (200, 201):
    cid = res.get("id") if isinstance(res, dict) else None
    print(f"\n✅ vytvořeno, id = {cid}")
    print("Ověření odesláním:")
    print("  curl -s -X POST https://mail.ripieno.xyz/api/mail/send \\")
    print("    -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"from\":\"Classio <{FROM}>\",\"to\":[{{\"email\":\"…\"}}],"
          "\"subject\":\"test\",\"text\":\"x\"}'")
else:
    print(f"\n❌ nevytvořeno (HTTP {code}): {res}")
