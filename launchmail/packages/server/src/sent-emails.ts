import { Hono } from "hono";
import { listSentEmails, getSentEmail } from "@workspace/mail-queue";
import type { AppVariables } from ".";
import { requirePerm } from "./org-context";

// Outgoing mail for the "Sent" view — a clean, body-inclusive read over
// email_logs. Reuses the existing `email` read permission.
const sentEmailsRouter = new Hono<AppVariables>()
  .get("/", async (c) => {
    const denied = requirePerm(c, "email", "read");
    if (denied) return denied;
    const limit = Number(c.req.query("limit") ?? 50);
    const beforeRaw = c.req.query("before");
    const before = beforeRaw ? new Date(beforeRaw) : undefined;
    return c.json(
      await listSentEmails(c.get("organizationId")!, {
        q: c.req.query("q") || undefined,
        limit: Number.isFinite(limit) ? limit : 50,
        before: before && !Number.isNaN(before.getTime()) ? before : undefined,
      }),
    );
  })
  .get("/:id", async (c) => {
    const denied = requirePerm(c, "email", "read");
    if (denied) return denied;
    const row = await getSentEmail(c.req.param("id"), c.get("organizationId")!);
    if (!row) return c.json({ error: "Not found" }, 404);
    return c.json(row);
  });

export default sentEmailsRouter;
