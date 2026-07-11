import { Hono } from "hono";
import type { AppVariables } from ".";
import z from "zod";
import { zValidator } from "@hono/zod-validator";
import { describeRoute } from "hono-openapi";

const paramSchema = z.object({
  name: z.string().optional(),
});

const helloRouter = new Hono<AppVariables>()
  .get(
    "/",
    describeRoute({
      summary: "Hello world",
      description: "Returns a greeting message",
      tags: ["Health"],
      responses: {
        "200": {
          description: "Successful greeting",
          content: {
            "application/json": {
              schema: {
                type: "object",
                properties: {
                  message: { type: "string" },
                },
              },
            },
          },
        },
      },
    }),
    zValidator("query", paramSchema),
    (c) => {
      const validated = c.req.valid("query");

      return c.json({
        message: `Hello ${validated.name ?? c.get("user")?.name ?? "visitor"}, welcome to the Launchday stack!`,
      });
    }
  );

export default helloRouter;
