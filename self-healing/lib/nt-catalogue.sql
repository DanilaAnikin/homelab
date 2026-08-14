-- ===========================================================================
-- nt-catalogue.sql — one canonical security catalogue, used by BOTH the
-- recovery-set builder (against production) and the restore drill (against
-- the clone). Byte-identical query text on both sides is the whole point.
--
-- WHY THIS EXISTS (audit findings H-3 and H-4)
-- -------------------------------------------
-- The previous "schema fingerprint" was:
--
--   sha256( string_agg( nspname||'.'||relname||':'||relkind||':'||owner ) )
--
-- That is a list of object NAMES. It says nothing about columns, types,
-- defaults, constraints, indexes, triggers, RLS, policies, ACLs or default
-- privileges. A restored database that lost EVERY ROW-LEVEL SECURITY POLICY
-- produces a byte-identical fingerprint and a green verdict — a certified
-- data-exposure event. That is the legacy failure mode ("count things that
-- happen to exist") moved up exactly one level of abstraction.
--
-- H-3: the two sides also disagreed on schema exclusion. The builder wrote
-- NOT LIKE 'pg_%', where `_` is a LIKE wildcard, so it silently excluded
-- pgsodium, pgbouncer and pgtle. The drill wrote 'pg\_%' (literal). They
-- coincided only because production currently has none of those schemas.
-- This file fixes the escaping in ONE place, so the sides cannot diverge.
--
-- OUTPUT: one line per catalogue fact, ordered. The caller hashes the whole
-- stream. The text itself is PRIVATE evidence (it carries password-verifier
-- digests and ACLs); only its sha256 is ever published.
-- ===========================================================================
-- QUIET must come first: without it psql echoes "Output format is unaligned."
-- and friends into the stream, and those lines would be hashed as catalogue
-- facts.
\set QUIET on
\set ON_ERROR_STOP on
\pset tuples_only on
\pset format unaligned
\pset pager off
\pset null __NTV_NULL__

WITH incl AS (
  -- the single shared inclusion rule; `pg\_%` uses a literal underscore so
  -- pg_catalog/pg_toast/pg_temp_* are excluded but pgsodium/pgbouncer/pgtle
  -- are NOT (they are application-relevant and must be compared)
  SELECT oid, nspname FROM pg_namespace
   WHERE nspname <> 'information_schema'
     AND nspname NOT LIKE 'pg\_%'
),
lines AS (

-- ── database ───────────────────────────────────────────────────────────────
SELECT format('database|%s|encoding=%s|collate=%s|ctype=%s|acl=%s',
              d.datname, pg_encoding_to_char(d.encoding), d.datcollate, d.datctype,
              coalesce(array_to_string(d.datacl::text[], ','), '')) AS l
  FROM pg_database d WHERE d.datname = current_database()

-- ── roles, attributes, and a private commitment to the password verifier ───
UNION ALL
SELECT format('role|%s|super=%s|inherit=%s|createrole=%s|createdb=%s|login=%s|repl=%s|bypassrls=%s|connlimit=%s|validuntil=%s',
              r.rolname, r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
              r.rolcanlogin, r.rolreplication, r.rolbypassrls, r.rolconnlimit,
              coalesce(r.rolvaliduntil::text, ''))
  FROM pg_roles r
UNION ALL
-- the verifier itself never appears; this stream stays on the host and only
-- its aggregate digest is published
SELECT format('pwverifier|%s|%s', a.rolname,
              encode(sha256(convert_to('ntv2pw:' || a.rolname || ':' || coalesce(a.rolpassword, ''), 'UTF8')), 'hex'))
  FROM pg_authid a
UNION ALL
SELECT format('rolemember|%s|%s|admin=%s|grantor=%s',
              r.rolname, m.rolname, am.admin_option, g.rolname)
  FROM pg_auth_members am
  JOIN pg_roles r ON r.oid = am.roleid
  JOIN pg_roles m ON m.oid = am.member
  LEFT JOIN pg_roles g ON g.oid = am.grantor
UNION ALL
SELECT format('setting|%s|%s|%s',
              coalesce(r.rolname, '-'), coalesce(d.datname, '-'),
              array_to_string(s.setconfig, ','))
  FROM pg_db_role_setting s
  LEFT JOIN pg_roles r ON r.oid = s.setrole
  LEFT JOIN pg_database d ON d.oid = s.setdatabase

-- ── schemas ────────────────────────────────────────────────────────────────
UNION ALL
SELECT format('schema|%s|owner=%s|acl=%s',
              n.nspname, pg_get_userbyid(n.nspowner),
              coalesce(array_to_string(n.nspacl::text[], ','), ''))
  FROM pg_namespace n JOIN incl i ON i.oid = n.oid

-- ── types and enum labels ──────────────────────────────────────────────────
UNION ALL
SELECT format('type|%s.%s|kind=%s|owner=%s|acl=%s',
              i.nspname, t.typname, t.typtype, pg_get_userbyid(t.typowner),
              coalesce(array_to_string(t.typacl::text[], ','), ''))
  FROM pg_type t JOIN incl i ON i.oid = t.typnamespace
UNION ALL
SELECT format('enumlabel|%s.%s|%s|%s',
              i.nspname, t.typname, e.enumsortorder, e.enumlabel)
  FROM pg_enum e
  JOIN pg_type t ON t.oid = e.enumtypid
  JOIN incl i ON i.oid = t.typnamespace
UNION ALL
SELECT format('domainconstraint|%s.%s|%s|validated=%s|def=%s',
              i.nspname, t.typname, con.conname, con.convalidated,
              pg_get_constraintdef(con.oid))
  FROM pg_constraint con
  JOIN pg_type t ON t.oid = con.contypid
  JOIN incl i ON i.oid = t.typnamespace

-- ── relations, columns ─────────────────────────────────────────────────────
UNION ALL
SELECT format('rel|%s.%s|kind=%s|owner=%s|persistence=%s|rls=%s|rlsforce=%s|acl=%s|options=%s',
              i.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner),
              c.relpersistence, c.relrowsecurity, c.relforcerowsecurity,
              coalesce(array_to_string(c.relacl::text[], ','), ''),
              coalesce(array_to_string(c.reloptions, ','), ''))
  FROM pg_class c JOIN incl i ON i.oid = c.relnamespace
 WHERE c.relkind IN ('r','p','v','m','S','f')
UNION ALL
SELECT format('col|%s.%s|%s|%s|type=%s|notnull=%s|default=%s|identity=%s|generated=%s|collation=%s|acl=%s',
              i.nspname, c.relname, a.attnum, a.attname,
              format_type(a.atttypid, a.atttypmod), a.attnotnull,
              coalesce(pg_get_expr(ad.adbin, ad.adrelid), ''),
              a.attidentity, a.attgenerated,
              coalesce(co.collname, ''),
              coalesce(array_to_string(a.attacl::text[], ','), ''))
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN incl i ON i.oid = c.relnamespace
  LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
  LEFT JOIN pg_collation co ON co.oid = a.attcollation
 WHERE a.attnum > 0 AND NOT a.attisdropped
   AND c.relkind IN ('r','p','v','m','f')
UNION ALL
SELECT format('viewdef|%s.%s|%s',
              i.nspname, c.relname,
              replace(pg_get_viewdef(c.oid, true), E'\n', '\\n'))
  FROM pg_class c JOIN incl i ON i.oid = c.relnamespace
 WHERE c.relkind IN ('v','m')

-- ── constraints, indexes, sequences ────────────────────────────────────────
UNION ALL
SELECT format('constraint|%s.%s|%s|type=%s|validated=%s|deferrable=%s|deferred=%s|def=%s',
              i.nspname, c.relname, con.conname, con.contype,
              con.convalidated, con.condeferrable, con.condeferred,
              pg_get_constraintdef(con.oid))
  FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid
  JOIN incl i ON i.oid = c.relnamespace
UNION ALL
SELECT format('index|%s.%s|%s|valid=%s|ready=%s|live=%s|primary=%s|unique=%s|def=%s',
              i.nspname, c.relname, ic.relname,
              ix.indisvalid, ix.indisready, ix.indislive, ix.indisprimary, ix.indisunique,
              pg_get_indexdef(ix.indexrelid))
  FROM pg_index ix
  JOIN pg_class ic ON ic.oid = ix.indexrelid
  JOIN pg_class c ON c.oid = ix.indrelid
  JOIN incl i ON i.oid = c.relnamespace
UNION ALL
SELECT format('sequence|%s.%s|start=%s|min=%s|max=%s|increment=%s|cycle=%s|last_value=%s|ownedby=%s',
              s.schemaname, s.sequencename, s.start_value, s.min_value, s.max_value,
              s.increment_by, s.cycle, coalesce(s.last_value::text, 'unread'),
              coalesce((SELECT format('%s.%s.%s', dn.nspname, dc.relname, da.attname)
                          FROM pg_depend dep
                          JOIN pg_class dc ON dc.oid = dep.refobjid
                          JOIN pg_namespace dn ON dn.oid = dc.relnamespace
                          JOIN pg_attribute da ON da.attrelid = dep.refobjid AND da.attnum = dep.refobjsubid
                         WHERE dep.classid = 'pg_class'::regclass
                           AND dep.objid = (quote_ident(s.schemaname) || '.' || quote_ident(s.sequencename))::regclass
                           AND dep.deptype = 'a'
                         LIMIT 1), '-'))
  FROM pg_sequences s JOIN incl i ON i.nspname = s.schemaname

-- ── routines: definition, owner, security mode, volatility, search path ────
UNION ALL
SELECT format('routine|%s.%s(%s)|kind=%s|owner=%s|secdef=%s|volatility=%s|parallel=%s|strict=%s|leakproof=%s|lang=%s|config=%s|acl=%s|def=%s',
              i.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
              p.prokind, pg_get_userbyid(p.proowner), p.prosecdef,
              p.provolatile, p.proparallel, p.proisstrict, p.proleakproof,
              l.lanname,
              coalesce(array_to_string(p.proconfig, ','), ''),
              coalesce(array_to_string(p.proacl::text[], ','), ''),
              replace(CASE WHEN p.prokind IN ('f','p','w') THEN pg_get_functiondef(p.oid)
                           ELSE coalesce(p.prosrc, '') END, E'\n', '\\n'))
  FROM pg_proc p
  JOIN incl i ON i.oid = p.pronamespace
  JOIN pg_language l ON l.oid = p.prolang
UNION ALL
SELECT format('aggregate|%s.%s(%s)|transfn=%s|finalfn=%s|initcond=%s',
              i.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
              ag.aggtransfn::text, ag.aggfinalfn::text, coalesce(ag.agginitval, ''))
  FROM pg_aggregate ag
  JOIN pg_proc p ON p.oid = ag.aggfnoid
  JOIN incl i ON i.oid = p.pronamespace

-- ── triggers ───────────────────────────────────────────────────────────────
UNION ALL
SELECT format('trigger|%s.%s|%s|enabled=%s|def=%s',
              i.nspname, c.relname, t.tgname, t.tgenabled,
              replace(pg_get_triggerdef(t.oid), E'\n', '\\n'))
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN incl i ON i.oid = c.relnamespace
 WHERE NOT t.tgisinternal

-- ── row-level security policies ────────────────────────────────────────────
UNION ALL
SELECT format('policy|%s.%s|%s|cmd=%s|permissive=%s|roles=%s|using=%s|withcheck=%s',
              pol.schemaname, pol.tablename, pol.policyname, pol.cmd, pol.permissive,
              array_to_string(pol.roles, ','),
              coalesce(pol.qual, ''), coalesce(pol.with_check, ''))
  FROM pg_policies pol JOIN incl i ON i.nspname = pol.schemaname

-- ── default privileges, publications, extensions, large objects ────────────
UNION ALL
SELECT format('defaultacl|%s|%s|%s|%s',
              pg_get_userbyid(da.defaclrole), coalesce(dn.nspname, '-'),
              da.defaclobjtype, array_to_string(da.defaclacl::text[], ','))
  FROM pg_default_acl da LEFT JOIN pg_namespace dn ON dn.oid = da.defaclnamespace
UNION ALL
SELECT format('publication|%s|owner=%s|alltables=%s|insert=%s|update=%s|delete=%s|truncate=%s',
              pub.pubname, pg_get_userbyid(pub.pubowner), pub.puballtables,
              pub.pubinsert, pub.pubupdate, pub.pubdelete, pub.pubtruncate)
  FROM pg_publication pub
UNION ALL
SELECT format('publicationrel|%s|%s.%s', pub.pubname, i.nspname, c.relname)
  FROM pg_publication_rel pr
  JOIN pg_publication pub ON pub.oid = pr.prpubid
  JOIN pg_class c ON c.oid = pr.prrelid
  JOIN incl i ON i.oid = c.relnamespace
UNION ALL
SELECT format('extension|%s|version=%s|schema=%s',
              e.extname, e.extversion, coalesce(en.nspname, '-'))
  FROM pg_extension e LEFT JOIN pg_namespace en ON en.oid = e.extnamespace
UNION ALL
SELECT format('extconfig|%s|%s.%s', e.extname, cn.nspname, cc.relname)
  FROM pg_extension e
  CROSS JOIN LATERAL unnest(coalesce(e.extconfig, '{}'::oid[])) AS cfg(objoid)
  JOIN pg_class cc ON cc.oid = cfg.objoid
  JOIN pg_namespace cn ON cn.oid = cc.relnamespace
UNION ALL
SELECT format('largeobject|%s|owner=%s|acl=%s',
              lo.oid, pg_get_userbyid(lo.lomowner),
              coalesce(array_to_string(lo.lomacl::text[], ','), ''))
  FROM pg_largeobject_metadata lo
)
SELECT l FROM lines ORDER BY l;
