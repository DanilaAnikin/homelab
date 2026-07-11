import { serve } from "@hono/node-server";
import { Hono } from "hono";
import app, { formsPublicApp } from "@workspace/server";

const root = new Hono();
root.route("/", app); // /api/* (incl. Better Auth at /api/auth/*)
root.route("/", formsPublicApp); // public form endpoints /f/:token
root.get("/health", (c) => c.json({ status: "ok" }));

const port = Number(process.env.PORT ?? 5000);

const server = serve({ fetch: root.fetch, port, hostname: "0.0.0.0" }, (info) => {
  console.log(`[api] LaunchMail API on http://0.0.0.0:${info.port}`);
});

// Graceful shutdown: stop accepting connections and let in-flight requests
// drain before exiting so the orchestrator sees a clean exit.
let shuttingDown = false;
function shutdown(signal: string): void {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[api] ${signal} received — shutting down...`);
  server.close(() => process.exit(0));
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
