import { Hono } from "hono";
import { mailQueue } from "@workspace/mail-queue";
import type { AppVariables } from ".";
import { describeRoute } from "hono-openapi";

const queueStatsRouter = new Hono<AppVariables>()
  .get(
    "/",
    describeRoute({
      summary: "Get queue stats",
      description:
        "Get counts of emails in each queue state (waiting, active, completed, failed, delayed)",
      tags: ["Queue"],
      security: [{ CookieAuth: [] }],
      responses: {
        "200": {
          description: "Queue statistics",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  waiting: { type: "integer" },
                  active: { type: "integer" },
                  completed: { type: "integer" },
                  failed: { type: "integer" },
                  delayed: { type: "integer" },
                  total: { type: "integer" },
                },
              },
            },
          },
        },
        "401": { description: "Unauthorized" },
      },
    }),
    async (c) => {
      const user = c.get("user");
      if (!user) return c.json({ error: "Unauthorized" }, 401);

      const [waiting, active, completed, failed, delayed] = await Promise.all([
        mailQueue.getWaitingCount(),
        mailQueue.getActiveCount(),
        mailQueue.getCompletedCount(),
        mailQueue.getFailedCount(),
        mailQueue.getDelayedCount(),
      ]);

      return c.json({
        waiting,
        active,
        completed,
        failed,
        delayed,
        total: waiting + active + completed + failed + delayed,
      });
    }
  );

export default queueStatsRouter;
