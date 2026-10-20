import { boundPgText, pgErrorCode } from "./pg-text";

// One-line, bounded, content-free description of an error, safe to log and to
// store in smtp_configs.imap_last_sync_error (which the UI and API show).
//
// Why not `error.message`: drizzle's DrizzleQueryError message is
// "Failed query: <sql>\nparams: <every bound value>" — for an incoming_emails
// insert that is the full text/HTML of up to 50 messages. Logging it leaked
// mail content into `docker logs` (10 MB rotations every few minutes), and
// storing it in imap_last_sync_error failed a second time on the very NUL
// byte that broke the insert. Here the driver error underneath is reported
// instead (SQLSTATE + message, which never contain the bound values).

const DEFAULT_MAX = 500;

function oneLine(value: string): string {
  // Control characters (incl. NUL, CR/LF, TAB) become spaces; runs collapse.
  // eslint-disable-next-line no-control-regex
  return value.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s{2,}/g, " ").trim();
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

/** "insert into incoming_emails" from the SQL text — never the values. */
function statementTarget(query: unknown): string | undefined {
  if (typeof query !== "string") return undefined;
  const m = /^\s*(insert\s+into|update|delete\s+from)\s+"?([A-Za-z0-9_.]+)"?/i.exec(
    query,
  );
  return m ? `${m[1]!.toLowerCase().replace(/\s+/g, " ")} ${m[2]}` : undefined;
}

function describeUnbounded(error: unknown): string {
  if (!error || typeof error !== "object") return String(error);
  const e = error as {
    message?: unknown;
    code?: unknown;
    cause?: unknown;
    query?: unknown;
    params?: unknown;
    responseText?: unknown;
    serverResponseCode?: unknown;
  };

  if ("query" in e && "params" in e) {
    // DrizzleQueryError: report the driver error underneath, never `params`.
    const cause = e.cause as { message?: unknown } | undefined;
    const sqlState = pgErrorCode(e.cause);
    const target = statementTarget(e.query);
    return [
      `Database error${sqlState ? ` ${sqlState}` : ""}: ${str(cause?.message) ?? "query failed"}`,
      target ? `(${target})` : "",
    ]
      .filter(Boolean)
      .join(" ");
  }

  const sqlState = pgErrorCode(e);
  const message = str(e.message) ?? String(error);
  const parts = [sqlState ? `Database error ${sqlState}: ${message}` : message];
  // ImapFlow: `code` (ETIMEOUT, NoConnection, …) and the server's own words for
  // a failed command, e.g. "[AUTHENTICATIONFAILED] Invalid credentials" —
  // exactly what an operator needs to tell a wrong app password from a
  // network problem.
  const code = str(e.code);
  if (code && !sqlState && !message.includes(code)) parts.push(`[${code}]`);
  const serverCode = str(e.serverResponseCode);
  if (serverCode && !message.includes(serverCode)) parts.push(`(${serverCode})`);
  const response = str(e.responseText);
  if (response && !message.includes(response)) {
    parts.push(`- server: ${boundPgText(oneLine(response), 200).value}`);
  }
  return parts.join(" ");
}

export function describeError(error: unknown, max = DEFAULT_MAX): string {
  const clean = boundPgText(oneLine(describeUnbounded(error)), max);
  if (!clean.truncated) return clean.value;
  return `${boundPgText(clean.value, Math.max(0, max - 1)).value}…`;
}

/** Stack frames only (no message line, which may carry data), for fatal logs. */
export function stackFrames(error: unknown, limit = 12): string {
  if (!(error instanceof Error) || typeof error.stack !== "string") return "";
  return error.stack
    .split("\n")
    .filter((line) => /^\s+at /.test(line))
    .slice(0, limit)
    .join("\n");
}
