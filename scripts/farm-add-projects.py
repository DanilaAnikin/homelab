#!/usr/bin/env python3
"""
farm-add-projects.py — přidá projekty do agent-farmy ve stavu 'stopped' (čekárna).

Proč 'stopped' a ne 'paused': budget-hold smyčka probouzí KAŽDÝ 'paused' projekt
zpět na 'active' po denním resetu (budget-hold.ts) → všech 5 by naskočilo naráz.
'stopped' je platný stav, na který nesahá žádná smyčka orchestrátoru → skutečná
čekárna. Do 'active' je pak přepíná POSTUPNĚ farm-project-rollout.py podle zdraví.

Idempotentní: existující projekt (dle jména) přeskočí, nezaloží duplicitu.
Použití: farm-add-projects.py [apply]     (bez 'apply' = DRY-RUN)
"""
import json, subprocess, sys

APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"
OWNER = "f63b0f81-199e-4daf-88b8-41959bc0b050"   # stejný vlastník jako ivanweb/contentgen

# autoDeliver=False zásadně: řídí auto-deploy na produkci (farm-autodeploy-check.sh).
# Nové projekty tedy VYVÍJEJÍ a otevírají PR, ale na živé weby nic samo nenasadí.
AUTONOMY = {"selfRun": True, "proactive": True, "autoDeliver": False, "deliverDailyCap": 100}

PROJECTS = [
    ("ripieno", "https://github.com/DanilaAnikin/ripieno",
     "Zpevnit jádro: testy, typová bezpečnost a chybové stavy",
     "Projdi existující kód fáze 0/1 (orchestrace agentů) a systematicky zvyšuj jeho spolehlivost: "
     "doplň chybějící unit testy u kritických cest, odstraň any/nekontrolované přetypování, "
     "ošetři chybové stavy a okrajové případy (timeout agenta, selhání sítě, nekonzistentní stav). "
     "Pracuj po malých, samostatně recenzovatelných celcích.\n\n"
     "DŮLEŽITÉ: www.ripieno.xyz je ŽIVÝ web. Neměň runtime chování ani veřejné API, "
     "nedělej destruktivní migrace. Jen kvalita, testy a robustnost."),

    ("loot", "https://github.com/DanilaAnikin/loot",
     "Otestovat a zpevnit peněžní smyčku (escrow ledger + RLS)",
     "Escrow ledger musí vždy sedět (locked = released + refunded + remaining). "
     "Doplň testy, které tuhle invariantu ověří na reálných scénářích: financování, "
     "schválení, auto-schválení, expirace s refundem, souběžné operace. "
     "Prověř RLS politiky (brand/creator/admin) testy, které se pokusí je obejít, "
     "a ošetři okrajové případy v SECURITY DEFINER RPC.\n\n"
     "Stripe je v test módu — nikdy nepřepínej na live klíče."),

    ("life-admin-agent", "https://github.com/DanilaAnikin/life-admin-agent",
     "Rozšířit pokrytí testy u klasifikace příčin zpoždění",
     "Jádro hodnoty je nezávislé ověření příčiny zpoždění a klasifikace proti judikatuře ECJ. "
     "Doplň testy pro klasifikátor (počasí/stávka/technická závada/rotace letadla), "
     "včetně sporných případů, kde aerolinka tvrdí mimořádnou okolnost neprávem. "
     "Ošetři chybějící/nekonzistentní data z externích zdrojů (METAR, statusy letů) "
     "tak, aby engine nikdy netvrdil víc, než na co má důkazy."),

    ("hummy", "https://github.com/DanilaAnikin/hummy",
     "Zpevnit chování bez klíčů a při chybách sítě",
     "MVP je hotové, ale chybí reálné klíče (ACRCloud/Spotify) a validace na zařízení. "
     "Zaměř se na to, co jde zlepšit bez klíčů: čitelné chybové stavy místo pádů, "
     "offline režim, timeouty a retry u rozpoznávání, prázdné stavy v UI, "
     "a testy pro tyhle cesty. Nepřidávej žádné klíče ani je nehardcoduj."),

    ("explain-and-act", "https://github.com/DanilaAnikin/explain-and-act",
     "Rozšířit playbooky a jejich testy",
     "Aplikace vrací strukturovanou analýzu dokumentů podle playbooků. "
     "Rozšiř pokrytí testy u existujících playbooků (úřední dopis/faktura, smlouva) "
     "na okrajové případy: chybějící částka, více termínů, cizojazyčný text, nečitelné skeny. "
     "Ověř, že model nikdy nevrací prózu místo dat a že capability-flagged funkce "
     "(předvyplnění formuláře, překlad) zůstávají poctivě označené jako nedostupné."),
]


def psql(sql):
    r = subprocess.run(
        ["docker", "exec", "-i", "agentfarm-supabase-db-1",
         "psql", "-U", "supabase_admin", "-d", "postgres", "-tAF", "\t", "-c", sql],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("PSQL ERR:", r.stderr[:500]); sys.exit(1)
    return [ln.split("\t") for ln in r.stdout.strip().split("\n") if ln]


def esc(s):
    return s.replace("'", "''")


existing = {r[0] for r in psql("select name from projects;")}
created = 0
for name, repo, wish_title, wish_desc in PROJECTS:
    if name in existing:
        print(f"  = {name:18s} už ve farmě → přeskakuji")
        continue
    if not APPLY:
        print(f"  + {name:18s} (stopped) + přání: {wish_title[:50]}")
        created += 1
        continue
    rows = psql(f"""
        insert into projects (user_id, name, kind, repo_mode, repo_url, env_recipe,
                              status, monthly_budget_usd, daily_cap_usd, trust_mode, autonomy)
        values ('{OWNER}', '{esc(name)}', 'code', 'existing', '{esc(repo)}', '{{}}'::jsonb,
                'stopped', 150, 3, true, '{esc(json.dumps(AUTONOMY))}'::jsonb)
        returning id::text;""")
    pid = rows[0][0]
    psql(f"""insert into wishes (project_id, title, description, source, status, budget_usd)
             values ('{pid}', '{esc(wish_title)}', '{esc(wish_desc)}', 'dashboard', 'new', 20);""")
    print(f"  + {name:18s} založen (stopped) + úvodní přání")
    created += 1

print(f"\n{'DRY-RUN: založilo by' if not APPLY else 'Založeno'}: {created} projektů")
if not APPLY:
    print("(spusť s 'apply' pro skutečné založení)")
