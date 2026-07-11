import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Pool } from "pg";

async function waitForDb(retries = 60, delay = 2000) {
  for (let i = 0; i < retries; i++) {
    try {
      const pool = new Pool({ connectionString: process.env.DATABASE_URL! });
      await pool.query("SELECT 1");
      await pool.end();
      return;
    } catch {
      console.log(`Waiting for database... (${i + 1}/${retries})`);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  throw new Error("Database not available after retries");
}

// Object-already-exists / duplicate codes we tolerate so the idempotent
// CREATE ... IF NOT EXISTS / guarded ALTER statements stay safe on re-run.
const IGNORED_CODES = new Set(["42P07", "42710"]);

async function run() {
  await waitForDb();
  const pool = new Pool({ connectionString: process.env.DATABASE_URL! });

  // Track which migration files have already been applied so files are not
  // re-run on every boot.
  await pool.query(
    `CREATE TABLE IF NOT EXISTS _migrations (
       name text PRIMARY KEY,
       applied_at timestamp DEFAULT now() NOT NULL
     )`,
  );
  const { rows: appliedRows } = await pool.query<{ name: string }>(
    "SELECT name FROM _migrations",
  );
  const applied = new Set(appliedRows.map((r) => r.name));

  const dir = join(
    dirname(fileURLToPath(import.meta.url)),
    "..",
    "migrations",
  );
  const files = readdirSync(dir)
    .filter((f) => f.endsWith(".sql"))
    .sort();

  for (const file of files) {
    if (applied.has(file)) {
      console.log(`Skipped (already applied): ${file}`);
      continue;
    }
    const sql = readFileSync(join(dir, file), "utf8");
    const statements = sql
      .split("--> statement-breakpoint")
      .map((s) => s.trim())
      .filter(Boolean);
    for (const stmt of statements) {
      try {
        await pool.query(stmt);
      } catch (e: unknown) {
        const code = (e as { code?: string }).code;
        if (code && IGNORED_CODES.has(code)) continue;
        throw e;
      }
    }
    await pool.query(
      "INSERT INTO _migrations (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
      [file],
    );
    console.log(`Applied: ${file}`);
  }

  console.log("Migration OK");
  await pool.end();
  process.exit(0);
}

run().catch((e) => {
  console.error("Migration FAIL:", e);
  process.exit(1);
});
