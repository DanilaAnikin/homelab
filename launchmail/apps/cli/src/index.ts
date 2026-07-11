import { Command } from "commander";
import { LaunchMailClient } from "@workspace/sdk";

function client(): LaunchMailClient {
  const apiKey = process.env.LAUNCHMAIL_API_KEY;
  if (!apiKey) {
    console.error(
      "LAUNCHMAIL_API_KEY is not set. Create a key in the dashboard (Organization → API keys).",
    );
    process.exit(1);
  }
  return new LaunchMailClient({
    baseUrl: process.env.LAUNCHMAIL_API_URL ?? "http://localhost:5000",
    apiKey,
  });
}

function print(data: unknown) {
  console.log(JSON.stringify(data, null, 2));
}

async function handle(fn: () => Promise<unknown>) {
  try {
    print(await fn());
  } catch (error) {
    console.error((error as Error).message);
    process.exit(1);
  }
}

const program = new Command();
program
  .name("launchmail")
  .description("LaunchMail CLI — manage email, SMTP, forms, and keys")
  .version("0.0.1");

program
  .command("whoami")
  .description("Show the API key's organization and role")
  .action(() => handle(() => client().whoami()));

const email = program.command("email").description("Send and inspect email");
email
  .command("send")
  .requiredOption("--to <emails>", "comma-separated recipients")
  .requiredOption("--subject <subject>")
  .option("--html <html>")
  .option("--text <text>")
  .option("--from <from>")
  .option("--reply-to <replyTo>")
  .action((opts) =>
    handle(() =>
      client().sendEmail({
        to: String(opts.to)
          .split(",")
          .map((e) => ({ email: e.trim() })),
        subject: opts.subject,
        html: opts.html,
        text: opts.text,
        from: opts.from,
        replyTo: opts.replyTo,
      }),
    ),
  );

program
  .command("logs")
  .description("List recent email send logs")
  .option("--limit <n>", "max rows", "50")
  .action((opts) => handle(() => client().listLogs(Number(opts.limit))));

// Also available as `email logs` (mirrors the "Send and inspect email" group).
email
  .command("logs")
  .description("List recent email send logs")
  .option("--limit <n>", "max rows", "50")
  .action((opts) => handle(() => client().listLogs(Number(opts.limit))));

const smtp = program.command("smtp").description("SMTP connections");
smtp.command("list").action(() => handle(() => client().listSmtpConfigs()));
smtp
  .command("create")
  .requiredOption("--name <name>")
  .requiredOption("--host <host>")
  .requiredOption("--port <port>")
  .requiredOption("--username <username>")
  .requiredOption("--password <password>")
  .requiredOption("--from <fromAddress>")
  .option("--from-name <fromName>")
  .action((opts) =>
    handle(() =>
      client().createSmtpConfig({
        name: opts.name,
        host: opts.host,
        port: Number(opts.port),
        username: opts.username,
        password: opts.password,
        fromAddress: opts.from,
        fromName: opts.fromName,
      }),
    ),
  );
smtp
  .command("test <id>")
  .action((id) => handle(() => client().testSmtpConnection(id)));

const keys = program.command("keys").description("API keys");
keys.command("list").action(() => handle(() => client().listApiKeys()));
keys
  .command("create")
  .requiredOption("--name <name>")
  .option("--role <role>", "admin|writer|reader", "writer")
  .action((opts) =>
    handle(() =>
      client().createApiKey({ name: opts.name, role: opts.role }),
    ),
  );

const forms = program.command("forms").description("Formspree-style forms");
forms.command("list").action(() => handle(() => client().listForms()));
forms
  .command("create")
  .requiredOption("--name <name>")
  .requiredOption("--template <key>")
  .requiredOption("--recipients <emails>", "comma-separated")
  .option("--subject <subject>")
  .action((opts) =>
    handle(() =>
      client().createForm({
        name: opts.name,
        templateKey: opts.template,
        recipients: String(opts.recipients)
          .split(",")
          .map((e) => e.trim()),
        subject: opts.subject,
      }),
    ),
  );
forms.command("get <id>").action((id) => handle(() => client().getForm(id)));
forms
  .command("submissions <id>")
  .action((id) => handle(() => client().listSubmissions(id)));

program.parseAsync(process.argv);
