import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { LaunchMailClient } from "@workspace/sdk";

const baseUrl = process.env.LAUNCHMAIL_API_URL ?? "http://localhost:5000";
const apiKey = process.env.LAUNCHMAIL_API_KEY;

if (!apiKey) {
  console.error(
    "LAUNCHMAIL_API_KEY is required. Create a key in the dashboard (Organization → API keys) and set LAUNCHMAIL_API_KEY + LAUNCHMAIL_API_URL.",
  );
  process.exit(1);
}

const client = new LaunchMailClient({ baseUrl, apiKey });

type ToolResult = {
  content: { type: "text"; text: string }[];
  isError?: boolean;
};

function ok(data: unknown): ToolResult {
  return {
    content: [
      {
        type: "text",
        text: typeof data === "string" ? data : JSON.stringify(data, null, 2),
      },
    ],
  };
}

function fail(error: unknown): ToolResult {
  return {
    content: [{ type: "text", text: `Error: ${(error as Error).message}` }],
    isError: true,
  };
}

async function run(fn: () => Promise<unknown>): Promise<ToolResult> {
  try {
    return ok(await fn());
  } catch (error) {
    return fail(error);
  }
}

const server = new McpServer({ name: "launchmail", version: "1.0.0" });

server.tool(
  "whoami",
  "Show the organization and role the configured API key acts as.",
  {},
  () => run(() => client.whoami()),
);

server.tool(
  "send_email",
  "Send an email through the organization's default SMTP connection.",
  {
    to: z.string().describe("Recipient email(s), comma-separated"),
    subject: z.string(),
    html: z.string().optional(),
    text: z.string().optional(),
    from: z.string().optional().describe("Optional; defaults to the SMTP from"),
    replyTo: z
      .string()
      .optional()
      .describe("Optional Reply-To address for the message"),
  },
  (a) =>
    run(() =>
      client.sendEmail({
        to: a.to.split(",").map((e) => ({ email: e.trim() })),
        subject: a.subject,
        html: a.html,
        text: a.text,
        from: a.from,
        replyTo: a.replyTo,
      }),
    ),
);

server.tool(
  "list_email_logs",
  "List recent sent/failed emails for the organization.",
  { limit: z.number().int().min(1).max(200).optional() },
  (a) => run(() => client.listLogs(a.limit ?? 50)),
);

server.tool(
  "list_smtp_configs",
  "List the organization's SMTP connections.",
  {},
  () => run(() => client.listSmtpConfigs()),
);

server.tool(
  "create_smtp_config",
  "Create an SMTP connection for the organization (writer/admin).",
  {
    name: z.string(),
    host: z.string(),
    port: z.number().int(),
    username: z.string(),
    password: z.string(),
    fromAddress: z.string().email(),
    fromName: z.string().optional(),
  },
  (a) => run(() => client.createSmtpConfig(a)),
);

server.tool(
  "test_smtp_connection",
  "Verify an SMTP connection by id.",
  { id: z.string() },
  (a) => run(() => client.testSmtpConnection(a.id)),
);

server.tool(
  "list_api_keys",
  "List the organization's API keys (hashes are never returned).",
  {},
  () => run(() => client.listApiKeys()),
);

server.tool(
  "create_api_key",
  "Create an API key for the organization (admin). Returns the plaintext once.",
  {
    name: z.string(),
    role: z.enum(["admin", "writer", "reader"]).optional(),
    smtpConfigId: z.string().optional(),
  },
  (a) => run(() => client.createApiKey(a)),
);

server.tool(
  "list_forms",
  "List the organization's Formspree-style forms.",
  {},
  () => run(() => client.listForms()),
);

server.tool(
  "create_form",
  "Create a form and get its public endpoint token.",
  {
    name: z.string(),
    templateKey: z.string().describe("e.g. contact, feedback, support"),
    recipients: z.array(z.string().email()).min(1),
    subject: z.string().optional(),
  },
  (a) => run(() => client.createForm(a)),
);

server.tool(
  "get_form",
  "Get a form by id (includes its endpoint token).",
  { id: z.string() },
  (a) => run(() => client.getForm(a.id)),
);

server.tool(
  "list_submissions",
  "List submissions captured by a form.",
  { id: z.string() },
  (a) => run(() => client.listSubmissions(a.id)),
);

async function main() {
  await server.connect(new StdioServerTransport());
  console.error("[launchmail-mcp] ready (stdio)");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
