import { ImapFlow, type ImapFlowOptions } from "imapflow";
import { describeError } from "./safe-error";

/**
 * The only place an ImapFlow is constructed. ImapFlow is an EventEmitter that
 * reports connection-level failures as an 'error' EVENT: a socket timeout
 * during a command, or a session that dies while connect() is still running
 * its startup commands after it already rejected (the orphaned startSession
 * then emits "Connection not available"). Without a listener Node rethrows
 * the event as an uncaught exception and the whole process exits — on
 * 20. 10. 2026 that restarted launchmail-api twenty times in one day.
 *
 * ImapFlow schedules close() before it emits, and close() rejects every
 * pending command and mailbox lock, so the awaiting caller still receives its
 * own error. The listener therefore only logs and lets the owner react.
 */
export function createImapClient(
  options: ImapFlowOptions,
  label: string,
  onError?: (error: unknown) => void,
): ImapFlow {
  const client = new ImapFlow(options);
  client.on("error", (error: unknown) => {
    console.warn(`[imap] ${label}: connection error: ${describeError(error)}`);
    if (!onError) return;
    try {
      onError(error);
    } catch (e) {
      console.error(`[imap] ${label}: error handler failed: ${describeError(e)}`);
    }
  });
  return client;
}
