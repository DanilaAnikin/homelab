import { Hono } from "hono";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppVariables } from ".";

const queueMocks = vi.hoisted(() => ({
  createApiToken: vi.fn(),
  listApiTokens: vi.fn(),
  deleteApiToken: vi.fn(),
  getSmtpConfig: vi.fn(),
  recordAudit: vi.fn(),
}));

vi.mock("@workspace/mail-queue", () => queueMocks);
vi.mock("./org-context", () => ({ requirePerm: () => null }));

import { apiKeysRouter } from "./management";

const ORGANIZATION_ID = "60ef206b-f766-4eb8-8ea0-ac3ca1e05cf9";
const OWN_CONFIG_ID = "88d218c0-a39a-419b-9b44-2688967f971a";
const FOREIGN_CONFIG_ID = "954ac519-8d37-430e-a570-becb9f64a44d";

function app(role: "admin" | "writer" | "reader" = "admin") {
  return new Hono<AppVariables>()
    .basePath("/api")
    .use("*", async (c, next) => {
      c.set("organizationId", ORGANIZATION_ID);
      c.set("role", role);
      c.set("user", null);
      c.set("session", null);
      c.set("apiTokenName", null);
      c.set("apiTokenSmtpConfigId", null);
      await next();
    })
    .route("/api-keys", apiKeysRouter);
}

function post(body: unknown, role?: "admin" | "writer" | "reader") {
  return app(role).request("/api/api-keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  queueMocks.createApiToken.mockImplementation(async (_org: string, input: Record<string, unknown>) => ({
    id: "token-id",
    tokenHash: "hash",
    tokenPrefix: "lm_abcd1234",
    plaintext: "lm_secret",
    smtpConfigId: null,
    ...input,
  }));
  queueMocks.getSmtpConfig.mockImplementation(async (id: string, org: string) =>
    id === OWN_CONFIG_ID && org === ORGANIZATION_ID ? { id } : null,
  );
});

describe("organization API keys", () => {
  it("creates a whole-organization key without a mailbox binding", async () => {
    const response = await post({ name: "Mail agent reader", role: "reader" });

    expect(response.status).toBe(201);
    expect(queueMocks.createApiToken).toHaveBeenCalledWith(ORGANIZATION_ID, {
      name: "Mail agent reader",
      role: "reader",
    });
    const json = await response.json();
    expect(json).not.toHaveProperty("tokenHash");
    // The web UI reads the one-time secret from this field.
    expect(json.plaintext).toBe("lm_secret");
    expect(queueMocks.getSmtpConfig).not.toHaveBeenCalled();
  });

  it("binds a key to a mailbox of the same organization", async () => {
    const response = await post({ name: "Writer", role: "writer", smtpConfigId: OWN_CONFIG_ID });

    expect(response.status).toBe(201);
    expect(queueMocks.createApiToken).toHaveBeenCalledWith(
      ORGANIZATION_ID,
      expect.objectContaining({ smtpConfigId: OWN_CONFIG_ID, role: "writer" }),
    );
  });

  it("refuses a mailbox from another organization", async () => {
    const response = await post({ name: "Writer", role: "writer", smtpConfigId: FOREIGN_CONFIG_ID });

    expect(response.status).toBe(404);
    expect(queueMocks.createApiToken).not.toHaveBeenCalled();
  });

  it("never grants a role above the caller's", async () => {
    const response = await post({ name: "Escalation", role: "admin" }, "writer");

    expect(response.status).toBe(403);
    expect(queueMocks.createApiToken).not.toHaveBeenCalled();
  });
});
