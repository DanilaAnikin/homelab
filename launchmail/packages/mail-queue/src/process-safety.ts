import { describeError, stackFrames } from "./safe-error";

// Process-level last line of defence for the api and worker processes.
//
// The pattern: an error that reaches `uncaughtException`/`unhandledRejection`
// is logged with context and, unless it is provably isolated, treated as a bug
// that ends the process through a graceful shutdown with a non-zero exit code.
// `start:all` (concurrently --kill-others) then stops the other process too and
// Docker restarts the container, so the crash shows up in RestartCount.
// Swallowing everything would keep a process alive in an unknown state and hide
// logic errors; dying on everything is what made one flaky IMAP account take
// the whole API down twenty times a day.
//
// "Provably isolated" = an ImapFlow connection error: ImapFlow tags whatever it
// emits with `_connId`, and it has already closed that one connection (and
// rejected its pending commands) before emitting. No shared state is left
// half-updated, so the process keeps running. Normally such errors never get
// here — createImapClient attaches a listener — so reaching this branch means
// some code created an ImapFlow without it; the log line says so.

export type ProcessFailureKind = "uncaughtException" | "unhandledRejection";

export interface ProcessSafetyNetOptions {
  /** Log prefix, e.g. "worker" or "api". */
  name: string;
  /** Graceful shutdown that ends the process with a non-zero exit code. */
  onFatal: (kind: ProcessFailureKind, error: unknown) => void | Promise<void>;
  log?: Pick<Console, "error">;
}

export function isImapConnectionError(error: unknown): boolean {
  return (
    !!error &&
    typeof error === "object" &&
    typeof (error as { _connId?: unknown })._connId === "string"
  );
}

/**
 * Decide what to do with one process-level failure. Exported for tests;
 * `state.fatal` makes sure onFatal runs once even if failures keep coming.
 */
export function handleProcessFailure(
  kind: ProcessFailureKind,
  error: unknown,
  options: ProcessSafetyNetOptions,
  state: { fatal: boolean },
): "continue" | "fatal" {
  const log = options.log ?? console;
  const frames = stackFrames(error);
  if (isImapConnectionError(error)) {
    log.error(
      `[${options.name}] ${kind}: IMAP connection error without a listener ` +
        `(that connection is already closed; process continues, but this is a bug: ` +
        `create IMAP clients with createImapClient): ${describeError(error)}` +
        (frames ? `\n${frames}` : ""),
    );
    return "continue";
  }
  log.error(
    `[${options.name}] FATAL ${kind}: ${describeError(error)}` +
      (frames ? `\n${frames}` : ""),
  );
  if (!state.fatal) {
    state.fatal = true;
    Promise.resolve()
      .then(() => options.onFatal(kind, error))
      .catch((e: unknown) => {
        log.error(`[${options.name}] shutdown after ${kind} failed: ${describeError(e)}`);
        process.exit(1);
      });
  }
  return "fatal";
}

export function installProcessSafetyNet(options: ProcessSafetyNetOptions): void {
  const state = { fatal: false };
  process.on("uncaughtException", (error) => {
    handleProcessFailure("uncaughtException", error, options, state);
  });
  process.on("unhandledRejection", (reason) => {
    handleProcessFailure("unhandledRejection", reason, options, state);
  });
}
