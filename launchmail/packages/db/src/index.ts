import { drizzle } from "drizzle-orm/node-postgres";
import { Pool } from "pg";
import * as schema from "./schemas/index";

// Harden the pool. Behind Docker/overlay networking, idle TCP connections can be
// silently dropped by conntrack/NAT; a pooled client then looks alive but its
// next query hangs or errors, and better-auth swallows that into an empty
// session (intermittent "logged out" under load, recovering once idle clients
// are recycled). keepAlive + a bounded connect timeout + surfacing pool errors
// makes reads deterministic.
const pool = new Pool({
  connectionString: process.env.DATABASE_URL!,
  max: 20,
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 10_000,
  keepAlive: true,
  keepAliveInitialDelayMillis: 10_000,
});

pool.on("error", (err) => {
  console.error("[db-pool] idle client error:", err.message);
});

export const db = drizzle(pool, { schema });
