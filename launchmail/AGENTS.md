# Agentic access — MCP server & CLI

Connect AI agents (or scripts) to LaunchMail with an **organization + role scoped API key**.
Create one in the dashboard: **Organization → API keys** (admin), or via the API. The key's role
(`admin` / `writer` / `reader`) bounds everything the agent can do — the server enforces it.

Both tools talk to the REST API over HTTP with `Authorization: Bearer <key>`; no database
credentials are needed on the agent's machine.

## Environment

```
LAUNCHMAIL_API_URL=http://localhost:3000   # or your deployed URL
LAUNCHMAIL_API_KEY=lm_xxx                   # org/role-scoped key
```

## MCP server (`apps/mcp`)

A Model Context Protocol server (stdio) exposing role-gated tools: `whoami`, `send_email`,
`list_email_logs`, `list_smtp_configs`, `create_smtp_config`, `test_smtp_connection`,
`list_api_keys`, `create_api_key`, `list_forms`, `create_form`, `get_form`, `list_submissions`.

Claude Desktop / Claude Code config:

```json
{
  "mcpServers": {
    "launchmail": {
      "command": "pnpm",
      "args": ["--filter", "mcp", "start"],
      "cwd": "/path/to/launchmail",
      "env": {
        "LAUNCHMAIL_API_URL": "http://localhost:3000",
        "LAUNCHMAIL_API_KEY": "lm_xxx"
      }
    }
  }
}
```

Then just ask your agent: *"send a test email to me@example.com"*, *"create a contact form that
emails support@acme.com"*, *"show the last 20 email logs"*.

## CLI (`apps/cli`)

```bash
export LAUNCHMAIL_API_URL=http://localhost:3000
export LAUNCHMAIL_API_KEY=lm_xxx

pnpm --filter cli start whoami
pnpm --filter cli start email send --to you@example.com --subject "Hi" --html "<p>hello</p>"
pnpm --filter cli start logs --limit 20
pnpm --filter cli start smtp list
pnpm --filter cli start forms create --name "Contact" --template contact --recipients support@acme.com
pnpm --filter cli start forms submissions <id>
pnpm --filter cli start keys create --name "ci" --role writer
```

Output is JSON, so it composes with `jq` and other tooling.

## Permissions

| Role   | Capabilities                                                            |
|--------|-------------------------------------------------------------------------|
| admin  | Everything (configs, keys, forms, send, logs).                          |
| writer | Manage SMTP configs/forms/keys, send email, read logs.                  |
| reader | Read-only (list configs/forms/logs); cannot send or mutate.             |

Organization/member administration (create org, invite/remove members, change roles) is
session-based in the dashboard, not exposed to API-key agents.
