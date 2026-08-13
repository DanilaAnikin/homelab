#!/usr/bin/env python3
"""
farm-pr-triage.py — autonomní vyřizování PR, které vyrobila agent-farm.

Farma umí kód napsat, ale ne uzavřít: PR se hromadily (31 otevřených) a čekaly na
člověka. Tenhle skript ten krok dělá za něj — o každém PR rozhodne Claude Code
běžící na serveru a rozhodnutí se rovnou provede.

DĚLBA PRÁCE (schválně nesymetrická):
  ÚSUDEK provádí Claude Code (`claude -p`). Dostane dossier: zadání úkolu z farmy,
  celý diff, seznam souborů, ostatní otevřené PR téhož repa (kvůli duplicitám) a
  cestu k pracovní kopii repa, do které se může podívat — právě tím se pozná
  „paralelní struktura, kterou nikdo nevolá" od kódu napojeného na aplikaci.

  PROVEDENÍ dělá tenhle kód. Guardraily NESMÍ záviset na tom, co model napíše:
  merge projde jen když GitHub hlásí mergeable, neběží failující check a diff
  nesahá na chráněné cesty. Když kterákoli podmínka padne, verdikt `merge` se
  degraduje na `revise` — nikdy naopak.

ROZHODNUTÍ:
  merge   → squash-merge, smazat větev, úkol ve farmě označit jako hotový
  revise  → PR zavřít s vysvětlením a založit ve farmě NÁSLEDNÝ úkol s konkrétním
            zadáním, co dodělat (parent_task_id ukazuje na původní) + zařadit do fronty
  close   → PR zavřít s vysvětlením (duplicita, škodlivé, mimo zadání), úkol neobnovovat

BEZPEČNOST: default je NÁHLED. Bez `--execute` se nic nemerguje ani nezavírá.

Použití: farm-pr-triage.py [--execute] [--repo=NAME] [--limit=N] [--max-merges=N]
"""
import json, os, re, subprocess, sys, tempfile, urllib.request, datetime

OWNER = "DanilaAnikin"
REPOS = ["contentgen", "ivanweb", "ripieno", "loot", "life-admin-agent", "hummy", "explain-and-act"]
WORKSPACES = "/var/lib/agent-farm/workspaces"
FARM_DB = "agentfarm-supabase-db-1"

EXECUTE = "--execute" in sys.argv
ONLY = next((a.split("=")[1] for a in sys.argv if a.startswith("--repo=")), None)
LIMIT = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--limit=")), "40"))
# Strop na merge za jeden běh. Kdyby se úsudek pokazil, omezí to škodu na pár PR
# místo na celý repozitář — a merge je jediná akce, která se špatně bere zpět.
MAX_MERGES = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--max-merges=")), "8"))

DIFF_CHARS = 60000
CLAUDE_TIMEOUT = 900

# Cesty, na které autonomní merge nesahá: deploy, CI a git hooky mají dopad mimo
# repozitář (spuštěné pipeline, běžící služby), takže je pouští jen člověk.
#
# Lockfiles tu SCHVÁLNĚ NEJSOU, i když je to lákavé. Přidání závislosti mění
# lockfile vždycky, takže by pravidlo zablokovalo skoro každé legitimní PR —
# poprvé to zavřelo i dobrý Sentry PR napojený na reálnou aplikaci. Riziko
# závislostí patří do posudku modelu (vidí celý diff), ne do plošného zákazu.
PROTECTED = re.compile(
    r"^(\.github/|\.gitlab-ci|Dockerfile|docker-compose|infra/|deploy/|\.env|"
    r"\.husky/|Makefile$)")

TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TG_TOKEN and os.environ.get("TELEGRAM_TOKEN_FILE"):
    # Soubor s tokenem je root-only, ale skript běží jako běžný uživatel (viz níže),
    # takže se čte přes sudo. Přímé open() se zkusí první kvůli běhu pod rootem jinde.
    try:
        TG_TOKEN = open(os.environ["TELEGRAM_TOKEN_FILE"]).read().strip()
    except Exception:
        r = subprocess.run(["sudo", "-n", "cat", os.environ["TELEGRAM_TOKEN_FILE"]],
                           capture_output=True, text=True, timeout=20)
        TG_TOKEN = (r.stdout or "").strip() or None

# `claude -p --dangerously-skip-permissions` odmítá běžet pod rootem, takže celý
# skript musí jet jako běžný uživatel a privilegované čtení dělat přes sudo.
if os.geteuid() == 0:
    print("CHYBA: spouštěj jako běžný uživatel (claude nepoběží pod rootem)."); sys.exit(1)
DOCKER = ["sudo", "-n", "docker"]

GH_TOKEN = os.environ.get("GITHUB_TOKEN") or ""
if not GH_TOKEN:
    # .env farmy je root-only (0600) — čte se přes sudo, do proměnné, ne na disk.
    r = subprocess.run(["sudo", "-n", "cat", "/srv/homelab/compose/agent-farm/app/.env"],
                       capture_output=True, text=True, timeout=20)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("GITHUB_ADMIN_PAT="):
            GH_TOKEN = ln.split("=", 1)[1].strip().strip("\"'"); break
if not GH_TOKEN:
    print("CHYBA: chybí GitHub token (GITHUB_ADMIN_PAT)."); sys.exit(1)


def gh(path, method="GET", body=None, accept="application/vnd.github+json"):
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": accept,
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "farm-pr-triage",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
        if "diff" in accept:
            return raw
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try: detail = e.read().decode()[:200]
            except Exception: pass
        print(f"    ⚠️ GitHub {method} {path}: {str(e)[:60]} {detail[:120]}")
        return None


def farm_sql(sql):
    r = subprocess.run(
        DOCKER + ["exec", "-i", FARM_DB, "psql", "-U", "postgres", "-d", "postgres",
                  "-tAF", "\t", "-c", sql], capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        print("    ⚠️ farm DB:", r.stderr[:160]); return []
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def ins_sql(sql):
    r = subprocess.run(
        DOCKER + ["exec", "-i", "postiz-postgres", "psql", "-U", "postiz", "-d", "insights",
                  "-tAF", "\t", "-c", sql], capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        print("    ⚠️ insights DB:", r.stderr[:160]); return []
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def esc(s):
    return str(s).replace("'", "''")


def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": text[:3800]}).encode(),
            headers={"Content-Type": "application/json"}), timeout=20)
    except Exception:
        pass


def norm_dedup(title, done_condition):
    """Musí odpovídat taskDedupKey() ve farmě (packages/core/src/dedup.ts), jinak by
    následné úkoly obcházely dedup, který jsme právě opravovali."""
    s = f"{title} {done_condition}".lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s.encode("ascii", "ignore").decode())
    return re.sub(r"\s+", " ", s).strip()


# --- dossier ---------------------------------------------------------------
def task_for_branch(branch):
    """Větev nese UUID úkolu: farm/task-<uuid>[-c<n>] (best-of-n kandidát)."""
    m = re.search(r"farm/task-([0-9a-f-]{36})", branch)
    if not m:
        return None
    # Přes row_to_json, ne přes sloupce oddělené tabulátorem: popis úkolu je
    # víceřádkový a tabulkový výstup psql se na jeho odřádkování rozpadne.
    rows = farm_sql(f"""select row_to_json(x) from (
        select t.id, t.title, t.description, t.done_condition, t.status,
               t.project_id, p.name as project, t.wish_id
        from tasks t join projects p on p.id = t.project_id
        where t.id = '{m.group(1)}') x;""")
    if not rows:
        return None
    try:
        return json.loads(rows[0][0])
    except Exception:
        return None


def build_dossier(repo, pr, detail, files, diff, siblings, decided):
    task = task_for_branch(pr["head"]["ref"])
    ws = os.path.join(WORKSPACES, task["project_id"]) if task else None
    checks = detail.get("mergeable_state")

    d = {
        "repo": repo,
        "pr": pr["number"],
        "title": pr["title"],
        "body": (pr.get("body") or "")[:2000],
        "branch": pr["head"]["ref"],
        "created_at": pr["created_at"],
        "additions": detail.get("additions"),
        "deletions": detail.get("deletions"),
        "changed_files": detail.get("changed_files"),
        "mergeable": detail.get("mergeable"),
        "mergeable_state": checks,
        "files": [f.get("filename") for f in files][:80],
        "zadani_z_farmy": task,
        "pracovni_kopie_repa": ws if ws and os.path.isdir(ws) else None,
        "ostatni_otevrene_PR_stejneho_repa": siblings,
        "uz_rozhodnuto_v_tomto_behu": decided,
        "diff": diff[:DIFF_CHARS],
        "diff_zkracen": len(diff) > DIFF_CHARS,
    }
    return d


PROMPT = """Jsi zodpovědný reviewer, který za majitele UZAVÍRÁ pull requesty vyrobené
automatickou farmou agentů. Tvoje rozhodnutí se PROVEDE — merge opravdu zmerguje,
close opravdu zavře. Rozhoduj tak, jak bys rozhodoval o vlastním repozitáři.

Dossier k jednomu PR je v souboru: {dossier}
Přečti si ho celý (Read). Obsahuje původní zadání z farmy, celý diff, seznam souborů
a ostatní otevřené PR téhož repa.

{ws_hint}

ROZHODNI jednu ze tří možností:

- "merge"  — splňuje zadání, kód je napojený na existující aplikaci (ne osamocený
             modul, který nikdo nevolá), nejsou tam podvody typu prázdný catch,
             natvrdo vrácená hodnota jen aby prošel test, TODO místo implementace
             nebo oslabené/smazané testy. Drobné nedostatky merge nebrání.

- "revise" — základ je použitelný, ale něco podstatného chybí (typicky: kód není
             nikde použitý, testy netestují nic reálného, pokrývá jen část zadání).
             Napiš do "followup" KONKRÉTNÍ zadání toho, co dodělat — ne obecné
             „vylepšit kvalitu". Ten úkol dostane farma zpátky a bude ho dělat.

- "close"  — duplicita jiného PR (podívej se na ostatní otevřené PR i na to, co už
             je rozhodnuté v tomto běhu), škodlivá změna (ruší funkční mechanismus
             a nahrazuje ho horším), nebo změna nesouvisí se zadáním. Bez následného
             úkolu — do "followup" dej null.

DUPLICITY: farma dlouho zakládala stejný úkol vícekrát (např. čtyřikrát „Result type
+ AppError hierarchy", česky i anglicky). Když vidíš dvě PR dělající totéž, JEDEN je
lepší → ten navrhni k merge nebo revise, ostatní "close" jako duplicitu a napiš
v reason číslo toho, který zůstává.

Odpověz POUZE tímto JSONem, nic jiného, žádný markdown blok:
{{"decision":"merge|revise|close",
  "confidence":"high|medium|low",
  "reason":"1-2 věty česky, proč",
  "pr_comment":"česky, co se napíše do PR jako vysvětlení rozhodnutí",
  "followup":{{"title":"...","description":"...","done_condition":"objektivně ověřitelné"}} nebo null}}
"""


def ask_claude(dossier):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(dossier, f, ensure_ascii=False, indent=1)
        path = f.name
    ws = dossier.get("pracovni_kopie_repa")
    ws_hint = (
        f"Pracovní kopie repozitáře je na {ws} — MŮŽEŠ do ní přes Bash/Grep nahlédnout "
        f"(např. `grep -rn \"NázevNovéFunkce\" {ws}/src`) a ověřit, jestli je nový kód "
        f"odněkud volaný, nebo tam leží nepoužitý. To je nejdůležitější rozdíl mezi "
        f"užitečným PR a kostrou. Pozor: kopie je na stavu main, změny z PR v ní NEJSOU."
        if ws else
        "Pracovní kopie repa není k dispozici — rozhoduj jen z diffu a zadání."
    )
    try:
        r = subprocess.run(
            ["claude", "-p", PROMPT.format(dossier=path, ws_hint=ws_hint),
             "--dangerously-skip-permissions",
             "--allowedTools", "Read,Bash,Grep,Glob",
             "--output-format", "text"],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
        out = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        print("    ⚠️ Claude timeout"); return None
    finally:
        # Agent má Bash, takže si dossier někdy po sobě sám smaže — na tom se nesmí
        # spadnout (jinak se běh utne uprostřed repa, jak se stalo u contentgenu).
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        print(f"    ⚠️ Claude nevrátil JSON: {out[:150]}"); return None
    raw = m.group(0)
    try:
        return json.loads(raw)
    except Exception:
        pass
    # Model občas obalí odpověď ```json blokem nebo použije jednoduché uvozovky /
    # koncovou čárku. Jedno PR se tím ztratilo, tak to radši zkusíme narovnat,
    # než ho celé zahodit.
    for fix in (lambda t: re.sub(r",\s*([}\]])", r"\1", t),
                lambda t: re.sub(r",\s*([}\]])", r"\1", t.replace("\n", " "))):
        try:
            return json.loads(fix(raw))
        except Exception:
            continue
    print(f"    ⚠️ nečitelný JSON: {raw[:120]}")
    return None


# --- guardraily ------------------------------------------------------------
def gate_merge(dossier):
    """Vrací None když merge smí projít, jinak důvod degradace. Tyhle podmínky
    kontroluje KÓD, ne model — model o nich nemá poslední slovo."""
    if dossier["mergeable"] is not True:
        return f"GitHub hlásí mergeable={dossier['mergeable']} (konflikt nebo se ještě počítá)"
    if dossier.get("mergeable_state") in ("dirty", "blocked", "behind"):
        return f"stav větve je '{dossier['mergeable_state']}'"
    hit = [f for f in dossier["files"] if PROTECTED.match(f or "")]
    if hit:
        return f"sahá na chráněné cesty ({', '.join(hit[:3])}) — merge patří člověku"
    return None


def _failed_names(repo, sha):
    runs = gh(f"/repos/{OWNER}/{repo}/commits/{sha}/check-runs")
    if not runs:
        return set()
    return {c["name"] for c in runs.get("check_runs", [])
            if c.get("status") == "completed" and c.get("conclusion") in ("failure", "timed_out")}


_baseline_cache = {}


def failing_checks(repo, sha):
    """Jen checky, které rozbil TENHLE PR — ne ty, co jsou červené i na main.

    Ripieno má `verify` failující přímo na main, takže původní verze pravidla
    blokovala všechny jeho PR napořád, i ty bezvadné. Rozbitá CI je problém repa,
    ne důvod zamrznout sklizeň; blokovat se má ten, kdo breakage způsobil."""
    pr_failed = _failed_names(repo, sha)
    if not pr_failed:
        return []
    if repo not in _baseline_cache:
        head = gh(f"/repos/{OWNER}/{repo}/commits/main")
        _baseline_cache[repo] = _failed_names(repo, head["sha"]) if head else set()
    return sorted(pr_failed - _baseline_cache[repo])


# --- provedení -------------------------------------------------------------
def do_comment(repo, num, text):
    gh(f"/repos/{OWNER}/{repo}/issues/{num}/comments", "POST",
       {"body": f"🤖 **Automatické vyřízení farmy**\n\n{text}"})


def do_merge(repo, num, title):
    res = gh(f"/repos/{OWNER}/{repo}/pulls/{num}/merge", "PUT",
             {"merge_method": "squash", "commit_title": f"{title} (#{num})"})
    return bool(res and res.get("merged"))


def do_close(repo, num):
    return gh(f"/repos/{OWNER}/{repo}/pulls/{num}", "PATCH", {"state": "closed"}) is not None


def do_delete_branch(repo, branch):
    gh(f"/repos/{OWNER}/{repo}/git/refs/heads/{branch}", "DELETE")


def do_followup_task(task, fu, pr_num, repo):
    """Založí ve farmě následný úkol a zařadí ho do fronty. Bez zařazení by ležel
    v DB a nikdo by ho nevzal — dispatcher čte pgmq, ne tabulku."""
    if not task:
        return None
    title = (fu.get("title") or "").strip()[:200]
    desc = (fu.get("description") or "").strip()
    done = (fu.get("done_condition") or "").strip()
    if not (title and done):
        return None
    desc = (f"{desc}\n\nKONTEXT: navazuje na PR #{pr_num} v {repo}, který byl zavřen "
            f"jako nedokončený. Nezakládej znovu, co už tam bylo — dodělej chybějící část.")
    key = norm_dedup(title, done)
    rows = farm_sql(f"""insert into tasks
        (project_id, parent_task_id, kind, title, description, done_condition,
         status, priority, max_attempts, dedup_key)
      values ('{task['project_id']}', '{task['id']}', 'code', '{esc(title)}', '{esc(desc)}',
              '{esc(done)}', 'queued', 90, 3, '{esc(key)}')
      returning id;""")
    if not rows:
        return None
    tid = rows[0][0]
    farm_sql(f"""select pgmq.send('q_tasks'::text,
      '{esc(json.dumps({"taskId": tid, "projectId": task["project_id"], "kind": "code"}))}'::jsonb,
      0::integer);""")
    return tid


def mark_task_done(task):
    if task and task["status"] not in ("done",):
        farm_sql(f"update tasks set status='done', updated_at=now() where id='{task['id']}';")


# --- hlavní smyčka ---------------------------------------------------------
ins_sql("""
create table if not exists farm_pr_triage (
  id bigserial primary key,
  decided_at timestamptz not null default now(),
  repo text not null, pr int not null, title text,
  decision text, confidence text, reason text,
  executed bool, gate_note text, followup_task uuid
);""")

print(f"{'PROVEDENÍ' if EXECUTE else 'NÁHLED (nic se nemění)'} — vyřizování PR farmy\n")
merges_done = 0
summary = {"merge": 0, "revise": 0, "close": 0, "skip": 0}
tg_lines = []

for repo in ([ONLY] if ONLY else REPOS):
    listing = gh(f"/repos/{OWNER}/{repo}/pulls?state=open&per_page=60&sort=created&direction=asc")
    if listing is None:
        continue
    prs = [p for p in listing if str(p.get("head", {}).get("ref", "")).startswith("farm/")]
    if not prs:
        continue
    print(f"=== {repo}: {len(prs)} otevřených PR ===")

    # Sourozenci se počítají jednou dopředu; rozhodnutá PR se přidávají průběžně,
    # aby druhý z dvojice duplicit viděl, že první už je vyřízený.
    all_meta = {p["number"]: {"pr": p["number"], "title": p["title"]} for p in prs}
    decided = []

    for p in prs[:LIMIT]:
      # Jedno rozbité PR nesmí shodit zbytek běhu — ostatní repozitáře by zůstaly
      # nevyřízené a příště by se začínalo znovu od začátku.
      try:
        num = p["number"]
        detail = gh(f"/repos/{OWNER}/{repo}/pulls/{num}")
        if not detail:
            continue
        files = gh(f"/repos/{OWNER}/{repo}/pulls/{num}/files?per_page=100") or []
        diff = gh(f"/repos/{OWNER}/{repo}/pulls/{num}", accept="application/vnd.github.v3.diff") or ""
        siblings = [v for k, v in all_meta.items() if k != num]
        dossier = build_dossier(repo, p, detail, files, diff, siblings, decided)

        v = ask_claude(dossier)
        if not v:
            summary["skip"] += 1
            continue

        decision = v.get("decision")
        conf = v.get("confidence", "low")
        reason = (v.get("reason") or "").strip()
        gate_note = ""

        # Guardraily: merge se smí jen degradovat, nikdy povýšit.
        if decision == "merge":
            g = gate_merge(dossier)
            if not g:
                bad = failing_checks(repo, p["head"]["sha"])
                if bad:
                    g = f"padají checky: {', '.join(bad[:3])}"
            if not g and conf == "low":
                g = "model si sám není jistý (confidence=low)"
            if not g and merges_done >= MAX_MERGES:
                g = f"strop {MAX_MERGES} mergů na běh vyčerpán"
            if g:
                gate_note = g
                decision = "revise"
                if not v.get("followup"):
                    v["followup"] = {
                        "title": f"Dokončit a zmergovat práci z PR #{num}",
                        "description": f"PR #{num} ({p['title']}) neprošel automatickým mergem: {g}.",
                        "done_condition": "Změna je v main a nic neblokuje merge.",
                    }

        mark = {"merge": "✅", "revise": "↩️", "close": "✖️"}.get(decision, "?")
        print(f"  {mark} #{num:<4} {decision:<7} [{conf}] {p['title'][:48]}")
        print(f"       {reason[:110]}")
        if gate_note:
            print(f"       ⛔ guardrail: {gate_note}")

        task = dossier["zadani_z_farmy"]
        fu_id = None
        executed = False

        if EXECUTE:
            comment = v.get("pr_comment") or reason
            if decision == "merge":
                do_comment(repo, num, comment)
                if do_merge(repo, num, p["title"]):
                    do_delete_branch(repo, p["head"]["ref"])
                    mark_task_done(task)
                    merges_done += 1
                    executed = True
                else:
                    print("       ⚠️ merge selhal, PR nechávám otevřený")
            elif decision == "revise":
                fu_id = do_followup_task(task, v.get("followup") or {}, num, repo)
                note = comment + (f"\n\nFarma dostala navazující úkol: `{fu_id}`." if fu_id
                                  else "\n\nNavazující úkol se nepodařilo založit.")
                do_comment(repo, num, note)
                if do_close(repo, num):
                    do_delete_branch(repo, p["head"]["ref"])
                    executed = True
            elif decision == "close":
                do_comment(repo, num, comment)
                if do_close(repo, num):
                    do_delete_branch(repo, p["head"]["ref"])
                    executed = True

        summary[decision] = summary.get(decision, 0) + 1
        decided.append({"pr": num, "title": p["title"], "decision": decision, "reason": reason})
        tg_lines.append(f"{mark} {repo}#{num} {decision}: {reason[:80]}")
        ins_sql(f"""insert into farm_pr_triage
          (repo, pr, title, decision, confidence, reason, executed, gate_note, followup_task)
          values ('{esc(repo)}', {num}, '{esc(p['title'])[:400]}', '{esc(decision)}',
                  '{esc(conf)}', '{esc(reason)[:600]}', {str(executed).lower()},
                  '{esc(gate_note)[:300]}', {f"'{fu_id}'" if fu_id else 'null'});""")
      except Exception as e:
        print(f"    ⚠️ PR #{p.get('number')} přeskočen kvůli chybě: {str(e)[:110]}")
        summary["skip"] += 1

head = (f"🤖 Vyřízení PR farmy: {summary['merge']}× merge, {summary['revise']}× vráceno "
        f"k dodělání, {summary['close']}× zavřeno" + (f", {summary['skip']}× přeskočeno" if summary["skip"] else ""))
print("\n" + head)
if not EXECUTE:
    print("(náhled — nic se neprovedlo; ostrý běh: --execute)")
elif tg_lines:
    tg(head + "\n\n" + "\n".join(tg_lines[:25]))
