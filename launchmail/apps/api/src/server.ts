import { serve } from "@hono/node-server";
import { Hono } from "hono";
import app, { formsPublicApp } from "@workspace/server";
import { installProcessSafetyNet } from "@workspace/mail-queue";

const root = new Hono();
root.route("/", app); // /api/* (incl. Better Auth at /api/auth/*)
root.route("/", formsPublicApp); // public form endpoints /f/:token
root.get("/health", (c) => c.json({ status: "ok" }));

const port = Number(process.env.PORT ?? 5000);

const server = serve({ fetch: root.fetch, port, hostname: "0.0.0.0" }, (info) => {
  console.log(`[api] LaunchMail API on http://0.0.0.0:${info.port}`);
});

// Graceful shutdown: stop accepting connections and let in-flight requests
// drain before exiting so the orchestrator sees a clean exit. Bounded, so a
// lingering connection cannot keep a broken process alive.
const SHUTDOWN_DEADLINE_MS = 10_000;
let shuttingDown = false;
function shutdown(reason: string, exitCode: number): void {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[api] ${reason} — shutting down...`);
  const deadline = setTimeout(() => process.exit(exitCode), SHUTDOWN_DEADLINE_MS);
  server.close(() => {
    clearTimeout(deadline);
    process.exit(exitCode);
  });
}

process.on("SIGTERM", () => shutdown("SIGTERM received", 0));
process.on("SIGINT", () => shutdown("SIGINT received", 0));

// Same policy as the worker (see process-safety.ts): log with context, survive
// an isolated IMAP connection error (the API opens IMAP for manual sync,
// attachment download and the connection test), otherwise exit 1. `start:all`
// then stops the worker too and the container restarts (see README.md).
installProcessSafetyNet({
  name: "api",
  onFatal: (kind) => shutdown(`fatal ${kind}`, 1),
});
