#!/usr/bin/env python3
"""
farm-pr-review.py — kontrola kvality toho, co agent-farm reálně vyrábí.

PROČ: farma běží na šesti projektech a otevírá PR, ale jestli je ta práce k něčemu,
to nikdo neověřoval. Stejná situace jako u postů — stroj jel, kvalitu nikdo neměřil,
a vady se našly až v publikovaném obsahu.

Kontrola má dvě vrstvy, protože samotný model si nevšimne všeho a samotná metrika nic
neřekne o smyslu:

  1) OBJEKTIVNÍ signály (bez LLM, nelze je „ukecat"):
     - mergeable: PR v konfliktu = práce, kterou nikdo nezmerguje
     - poměr testů: mění PR jen testy? nebo naopak žádné nepřidává?
     - velikost: obří diff se nedá zrevidovat, mikrodiff bývá kosmetika
     - stáří: PR otevřený týdny = farma tvoří rychleji, než se sklízí
     - smazané soubory: nejrizikovější druh změny

  2) POSOUZENÍ MODELEM nad skutečným diffem: dělá to, co slibuje název? je to
     integrované do existujícího kódu, nebo staví paralelní strukturu? nejsou tam
     zjevné vady (prázdné catch bloky, hardcoded návratové hodnoty, oslabené testy)?

Verdikty: useful | weak | harmful. Harmful se hlásí na Telegram hned.

Data jdou do DB `insights` (mimo Postiz i farmu, aby je žádná migrace nesmazala).
Použití: farm-pr-review.py [--limit N] [--repo NAME]
"""
import json, os, re, subprocess, sys, urllib.request, datetime

REPOS = ["contentgen", "ivanweb", "ripieno", "loot", "life-admin-agent", "hummy", "explain-and-act"]
OWNER = "DanilaAnikin"
LIMIT = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--limit=")), "40"))
ONLY = next((a.split("=")[1] for a in sys.argv if a.startswith("--repo=")), None)
DIFF_CHARS = 14000

KEY = os.environ.get("DEEPSEEK_API_KEY")
# Telegram: homelab drží token v souboru, na který ukazuje TELEGRAM_TOKEN_FILE
# (viz /srv/homelab/secrets/homelab.conf) — ne v proměnné, aby ho neviděl `ps`.
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TG_TOKEN and os.environ.get("TELEGRAM_TOKEN_FILE"):
    try:
        TG_TOKEN = open(os.environ["TELEGRAM_TOKEN_FILE"]).read().strip()
    except Exception:
        pass


GH_TOKEN = os.environ.get("GITHUB_TOKEN") or ""
if not GH_TOKEN:
    try:  # tentýž token, jaký používá farm-deploy
        with open("/srv/homelab/compose/agent-farm/app/.env") as f:
            for ln in f:
                if ln.startswith("GITHUB_ADMIN_PAT="):
                    GH_TOKEN = ln.split("=", 1)[1].strip().strip("\"'"); break
    except Exception:
        pass


def gh_api(path, accept="application/vnd.github+json"):
    """GitHub REST místo `gh` CLI — na serveru není nainstalované."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": accept,
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "farm-pr-review"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
        return raw if "diff" in accept else json.loads(raw)
    except Exception as e:
        print(f"    ⚠️ GitHub API {path}: {str(e)[:70]}")
        return "" if "diff" in accept else []


def psql(sql, tuples=True, db="insights"):
    r = subprocess.run(
        ["docker", "exec", "-i", "postiz-postgres", "psql", "-U", "postiz", "-d", db, "-tAF", "\t", "-c", sql],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:300]); sys.exit(1)
    out = [ln for ln in r.stdout.strip().split("\n") if ln]
    return [ln.split("\t") for ln in out] if tuples else out


def judge(pr, diff):
    """Posoudí diff modelem. Bez klíče vrací None — objektivní signály platí i tak."""
    if not KEY:
        return None
    sys_p = (
        "Jsi přísný, ale spravedlivý recenzent kódu. Dostaneš název úkolu a skutečný diff "
        "z automaticky vytvořeného pull requestu. Posuzuj VÝHRADNĚ podle diffu.\n"
        "Hledej hlavně: (a) dělá to, co slibuje název? (b) je to napojené na existující kód, "
        "nebo to staví paralelní strukturu, kterou nikdo nepoužívá? (c) nejsou tam podvody — "
        "prázdné catch bloky, natvrdo vrácené hodnoty jen aby prošel test, TODO místo "
        "implementace, oslabené nebo smazané testy?\n"
        "Buď konkrétní a stručný. Nechval to, co je jen kostra bez obsahu."
    )
    user = (
        f"PROJEKT: {pr['repo']}\nNÁZEV: {pr['title']}\n"
        f"ROZSAH: +{pr['additions']}/-{pr['deletions']} v {pr['changedFiles']} souborech\n\n"
        f"DIFF (zkrácený):\n{diff[:DIFF_CHARS]}\n\n"
        'Vrať POUZE JSON: {"verdict":"useful|weak|harmful","summary":"1 věta česky",'
        '"issues":["konkrétní nález", "..."],"integrated":true|false}'
    )
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"}, "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        return json.loads(d["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"    ⚠️ posouzení selhalo: {str(e)[:70]}")
        return None


def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": text}).encode(),
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception:
        pass


psql("""
create table if not exists farm_pr_reviews (
  id bigserial primary key,
  reviewed_at timestamptz not null default now(),
  repo text not null, pr int not null, title text,
  additions int, deletions int, changed_files int,
  mergeable text, age_days numeric,
  touches_tests bool, deletes_files bool,
  verdict text, integrated bool, summary text, issues jsonb,
  unique (repo, pr, reviewed_at)
);
create index if not exists ix_fpr on farm_pr_reviews(repo, pr, reviewed_at desc);
""", tuples=False)

repos = [ONLY] if ONLY else REPOS
rows, harmful, checked = [], [], 0
for repo in repos:
    listing = gh_api(f"/repos/{OWNER}/{repo}/pulls?state=open&per_page=60") or []
    prs = [x for x in listing if str(x.get("head", {}).get("ref", "")).startswith("farm/")]
    for p in prs[:LIMIT]:
        checked += 1
        num = p["number"]
        det = gh_api(f"/repos/{OWNER}/{repo}/pulls/{num}") or {}
        p = {**p, "additions": det.get("additions", 0), "deletions": det.get("deletions", 0),
             "changedFiles": det.get("changed_files", 0), "mergeable": det.get("mergeable")}
        fl = gh_api(f"/repos/{OWNER}/{repo}/pulls/{num}/files?per_page=100") or []
        files = [f.get("filename", "") for f in fl]
        tests = any(re.search(r"(test|spec)\.[jt]sx?$|__tests__/|\.test\.", f) for f in files)
        dels = any((f.get("deletions") or 0) > 0 and (f.get("additions") or 0) == 0 for f in fl)
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))).days
        diff = gh_api(f"/repos/{OWNER}/{repo}/pulls/{num}", "application/vnd.github.v3.diff")
        v = judge({**p, "repo": repo}, diff) or {}
        verdict = v.get("verdict") or ("weak" if not diff else "neposouzeno")
        print(f"  {repo}#{num:<4} {verdict:<12} +{p['additions']}/-{p['deletions']}  {p['title'][:52]}")
        if v.get("issues"):
            for it in v["issues"][:2]:
                print(f"      · {str(it)[:100]}")
        if verdict == "harmful":
            harmful.append(f"{repo}#{num}: {v.get('summary','')[:90]}")

        # POZOR: ořezávat se musí JEDNOTLIVÉ nálezy PŘED serializací. Ořez hotového
        # JSON stringu na 600 znaků ho utne uprostřed a Postgres ho odmítne.
        esc = lambda s: str(s)[:600].replace("'", "''")
        issues_json = json.dumps([str(i)[:300] for i in (v.get("issues") or [])][:6],
                                 ensure_ascii=False).replace("'", "''")
        psql(f"""insert into farm_pr_reviews
          (repo, pr, title, additions, deletions, changed_files, mergeable, age_days,
           touches_tests, deletes_files, verdict, integrated, summary, issues)
          values ('{esc(repo)}', {num}, '{esc(p['title'])}', {p['additions']}, {p['deletions']},
                  {p['changedFiles']}, '{esc(p.get('mergeable'))}', {age},
                  {str(tests).lower()}, {str(dels).lower()}, '{esc(verdict)}',
                  {str(bool(v.get('integrated'))).lower()}, '{esc(v.get('summary',''))}',
                  '{issues_json}'::jsonb);""", tuples=False)
        rows.append((repo, num, verdict))

good = sum(1 for r in rows if r[2] == "useful")
weak = sum(1 for r in rows if r[2] == "weak")
print(f"\nzkontrolováno {checked} PR — užitečných {good}, slabých {weak}, škodlivých {len(harmful)}")
if harmful:
    tg("🚜 Kontrola farmy našla škodlivé PR:\n" + "\n".join(harmful[:8]))
elif checked and good / max(checked, 1) < 0.4:
    tg(f"🚜 Kontrola farmy: jen {good} z {checked} PR je užitečných — stojí za pohled.")
