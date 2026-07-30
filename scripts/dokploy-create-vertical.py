#!/usr/bin/env python3
"""Vytvoří + nakonfiguruje Dokploy app pro jeden lokwave vertical (klon vet)."""
import os, sys, json, urllib.request, urllib.error

SEC = "/home/anakin/programming/homelab/secrets"


def _conf(key):
    """Interní identifikátory mimo git — secrets/homelab.conf (gitignored)."""
    for line in open(f"{SEC}/homelab.conf"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"chybí {key} v secrets/homelab.conf")


DK = _conf("DOKPLOY_API")
KEY = open(f"{SEC}/dokploy-api-token.txt").read().strip()
ENV_ID = _conf("DOKPLOY_ENV_ID")        # stejné environment jako vet
GITHUB_ID = _conf("DOKPLOY_GITHUB_ID")
# Stripe publishable key (pk_live_) — z designu veřejný, ale držíme ho mimo repo.
# Nastav před spuštěním:  export STRIPE_PK=pk_live_...
STRIPE_PK = os.environ.get("STRIPE_PK", "")

def post(ep, body):
    req = urllib.request.Request(DK + "/" + ep, data=json.dumps(body).encode(),
        headers={"x-api-key": KEY, "Content-Type": "application/json"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=40); return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def make_env(vertical, domain):
    lines = open("/tmp/vet_env.txt").read().splitlines()
    out = []
    for ln in lines:
        if ln.startswith("NEXT_PUBLIC_VERTICAL="):
            out.append(f"NEXT_PUBLIC_VERTICAL={vertical}")
        elif ln.startswith("NEXT_PUBLIC_APP_URL="):
            out.append(f"NEXT_PUBLIC_APP_URL=https://{domain}")
        else:
            out.append(ln)
    return "\n".join(out)

def main():
    app, vertical, domain = sys.argv[1], sys.argv[2], sys.argv[3]
    # 1) create
    s, b = post("application.create", {"name": app, "environmentId": ENV_ID,
                                       "description": f"lokwave {vertical} ({domain})"})
    if s != 200:
        print(f"create FAIL {s}: {b[:200]}"); sys.exit(1)
    app_id = json.loads(b).get("applicationId") or json.loads(b).get("id")
    print(f"  create: {app_id}")
    open(f"/tmp/app_{app}.txt", "w").write(app_id)
    # 2) github source
    s, b = post("application.saveGithubProvider", {
        "applicationId": app_id, "githubId": GITHUB_ID, "owner": "DanilaAnikin",
        "repository": "dentallocal", "branch": "feat/selfhost-strategy-b",
        "buildPath": "/", "triggerType": "push", "watchPaths": []})
    print(f"  github: {s} {'' if s==200 else b[:150]}")
    # 3) build type = dockerfile (per-app build args baked at build)
    s, b = post("application.saveBuildType", {
        "applicationId": app_id, "buildType": "dockerfile", "dockerfile": "Dockerfile",
        "dockerContextPath": "", "dockerBuildStage": "", "herokuVersion": None,
        "railpackVersion": None})
    print(f"  buildType: {s} {'' if s==200 else b[:150]}")
    # 4) env + build args
    buildargs = (f"APP={app}\nVERTICAL={vertical}\n"
                 f"NEXT_PUBLIC_APP_URL=https://{domain}\n"
                 f"NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY={STRIPE_PK}")
    s, b = post("application.saveEnvironment", {
        "applicationId": app_id, "env": make_env(vertical, domain),
        "buildArgs": buildargs, "buildSecrets": "", "createEnvFile": False})
    print(f"  env: {s} {'' if s==200 else b[:150]}")
    # 5) domains apex + www
    for host in (domain, f"www.{domain}"):
        s, b = post("domain.create", {"host": host, "applicationId": app_id, "port": 3000,
            "https": False, "path": "/", "serviceName": "", "domainType": "application",
            "certificateType": "none"})
        print(f"  domain {host}: {s}")

if __name__ == "__main__":
    main()
