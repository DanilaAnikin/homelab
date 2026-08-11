#!/usr/bin/env python3
"""
farm-workspace-gc.py — uklidí git worktree adresáře agent-farmy po DOKONČENÝCH taskách.

PROČ: každý task, ve kterém agent spustí instalaci závislostí, nechá worktree
s node_modules (~1,6 GB). Nic je nemaže → 47 GB za týden při 2 projektech.
Se 7 projekty by to zaplnilo disk. Tenhle uklízeč maže JEN worktrees, jejichž
task je v terminálním stavu a nemá běžící pokus.

BEZPEČNOST (několik pojistek, aby se nikdy nesmazalo nic živého):
  - maže jen adresáře tvaru <uuid>--<uuid> → základní klon <uuid> nikdy
  - task musí být v terminálním stavu (ne queued/running/judging/specifying)
  - task nesmí mít pokus se statusem 'running'
  - worktree musí být starší než GRACE_HOURS (default 6 h)
  - preferuje `git worktree remove` (udrží git metadata konzistentní),
    rm -rf až jako fallback, vždy následuje `git worktree prune`

Použití:
  farm-workspace-gc.py            # DRY-RUN (jen vypíše, co by smazal)
  farm-workspace-gc.py apply      # skutečně uklidí
"""
import json, os, re, shutil, subprocess, sys

ROOT = "/var/lib/agent-farm/workspaces"
GRACE_HOURS = int(os.environ.get("FARM_GC_GRACE_HOURS", "6"))
# Stavy, při kterých se worktree NESMÍ sáhnout (task ještě žije).
LIVE_TASK_STATES = ("queued", "running", "judging", "specifying", "planning")
APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"
PAIR = re.compile(r"^[0-9a-f-]{36}--[0-9a-f-]{36}", re.I)


def psql(sql):
    r = subprocess.run(
        ["docker", "exec", "-i", "agentfarm-supabase-db-1",
         "psql", "-U", "supabase_admin", "-d", "postgres", "-tAF", "\t", "-c", sql],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:400]); sys.exit(1)
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def du_mb(path):
    try:
        r = subprocess.run(["du", "-sm", path], capture_output=True, text=True, timeout=120)
        return int(r.stdout.split()[0]) if r.returncode == 0 else 0
    except Exception:
        return 0


# --- Které tasky jsou ještě živé + které mají běžící pokus -------------------
live_tasks = {r[0] for r in psql(
    "select id::text from tasks where status in (%s);" %
    ",".join("'%s'" % s for s in LIVE_TASK_STATES))}
running_attempt_tasks = {r[0] for r in psql(
    "select distinct task_id::text from attempts where status='running';")}
protected = live_tasks | running_attempt_tasks

if not os.path.isdir(ROOT):
    print(f"{ROOT} neexistuje"); sys.exit(0)

now = __import__("time").time()
cands, skipped_live, skipped_young = [], 0, 0

for name in sorted(os.listdir(ROOT)):
    path = os.path.join(ROOT, name)
    if not os.path.isdir(path):
        continue
    if not PAIR.match(name):        # základní klon <uuid> → NIKDY
        continue
    task_id = name.split("--", 1)[1][:36]
    if task_id in protected:
        skipped_live += 1
        continue
    age_h = (now - os.path.getmtime(path)) / 3600.0
    if age_h < GRACE_HOURS:
        skipped_young += 1
        continue
    cands.append((path, name, du_mb(path), age_h))

total_mb = sum(c[2] for c in cands)
print(f"Worktrees k úklidu: {len(cands)}  |  uvolní ~{total_mb/1024:.1f} GB")
print(f"Přeskočeno: {skipped_live} živých, {skipped_young} mladších než {GRACE_HOURS} h")
if not APPLY:
    for p, n, mb, age in cands[:10]:
        print(f"  DRY {mb:6d} MB  {age:5.1f} h  {n}")
    if len(cands) > 10:
        print(f"  … a dalších {len(cands)-10}")
    print("\n(DRY-RUN — spusť s 'apply' pro skutečný úklid)")
    sys.exit(0)

freed, failed = 0, 0
bases = set()
for path, name, mb, _age in cands:
    base = os.path.join(ROOT, name.split("--", 1)[0])
    bases.add(base)
    ok = False
    if os.path.isdir(os.path.join(base, ".git")) or os.path.isfile(os.path.join(base, ".git")):
        r = subprocess.run(["git", "-C", base, "worktree", "remove", "--force", path],
                           capture_output=True, text=True, timeout=180)
        ok = r.returncode == 0
    if not ok and os.path.isdir(path):
        try:
            shutil.rmtree(path); ok = True
        except Exception as e:
            print(f"  ✗ {name}: {e}"); failed += 1
    if ok:
        freed += mb

for base in bases:                  # srovnej git metadata po mazání
    subprocess.run(["git", "-C", base, "worktree", "prune"],
                   capture_output=True, text=True, timeout=120)

print(f"Uklizeno: {freed/1024:.1f} GB ({len(cands)-failed} worktrees, {failed} chyb)")
