import { describeError } from "./safe-error";

// Graceful, bounded shutdown of the worker process (apps/api/src/worker.ts).
//
// Order matters because of one risk: a duplicate e-mail. BullMQ's
// `worker.close()` stops taking jobs and waits for the running ones, i.e. for
// SMTP transactions that are already under way. If the process exits before
// such a job has recorded its result, the job's lock expires, BullMQ marks it
// stalled and runs it again after the restart: the same message is sent twice.
// So the BullMQ workers (and the webhook outbox relay) start closing
// immediately, and nothing else may eat the deadline before them. The IMAP
// inbox is stopped at the same time, with its own short limit: on 20. 10. 2026
// Gmail needed 37 s for a login, and a LOGOUT to it used to be awaited first,
// for all 17 mailboxes, before BullMQ was even asked to close.

export interface Closable {
  close(): Promise<unknown>;
}

export interface Stoppable {
  stop(): Promise<unknown>;
}

export interface WorkerShutdownResources {
  /** BullMQ send worker: close() waits for in-flight SMTP jobs. */
  mailWorker: Closable;
  /** BullMQ webhook worker. */
  webhookWorker: Closable;
  webhookOutboxRelay: Stoppable;
  inboxIdle: Stoppable;
  /** Producer queues + shared Redis, closed once the workers are done. */
  closeConnections: () => Promise<unknown> | unknown;
}

export interface WorkerShutdownOptions {
  /** Hard limit for the whole shutdown; the process exits when it passes. */
  deadlineMs?: number;
  /** Upper bound for stopping the IMAP inbox (it bounds each LOGOUT itself). */
  imapStopMs?: number;
  exit?: (code: number) => void;
  log?: Pick<Console, "log" | "error">;
}

class StepTimeoutError extends Error {
  constructor(ms: number) {
    super(`did not finish within ${Math.round(ms / 100) / 10} s`);
    this.name = "StepTimeoutError";
  }
}

function within<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return Promise.race([
    promise,
    new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new StepTimeoutError(ms)), ms);
    }),
  ]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

/** Run `fn`, turning a synchronous throw into a rejected promise. */
async function call(fn: () => Promise<unknown> | unknown): Promise<unknown> {
  return fn();
}

/**
 * Returns `shutdown(reason, exitCode)`. Idempotent: a second call (another
 * signal, a fatal error during shutdown) is ignored. Exits exactly once,
 * either when everything is closed or when the deadline passes.
 */
export function createWorkerShutdown(
  resources: WorkerShutdownResources,
  options: WorkerShutdownOptions = {},
): (reason: string, exitCode: number) => Promise<void> {
  const deadlineMs = options.deadlineMs ?? 10_000;
  const imapStopMs = options.imapStopMs ?? 3_000;
  const exit = options.exit ?? ((code: number) => process.exit(code));
  const log = options.log ?? console;
  let started = false;
  let exited = false;

  const finish = (code: number) => {
    if (exited) return;
    exited = true;
    exit(code);
  };

  return async function shutdown(reason: string, exitCode: number): Promise<void> {
    if (started) return;
    started = true;
    log.log(`[worker] ${reason} — shutting down...`);
    const deadline = setTimeout(() => {
      log.error(
        `[worker] shutdown still running after ${deadlineMs / 1000} s — exiting anyway`,
      );
      finish(exitCode);
    }, deadlineMs);

    try {
      const steps: Array<[string, Promise<unknown>]> = [
        ["send worker", call(() => resources.mailWorker.close())],
        ["webhook worker", call(() => resources.webhookWorker.close())],
        ["webhook outbox relay", call(() => resources.webhookOutboxRelay.stop())],
        ["IMAP inbox", within(call(() => resources.inboxIdle.stop()), imapStopMs)],
      ];
      const results = await Promise.allSettled(steps.map(([, step]) => step));
      results.forEach((result, index) => {
        if (result.status === "rejected") {
          log.error(
            `[worker] ${steps[index]![0]} failed to stop: ${describeError(result.reason)}`,
          );
        }
      });
      await resources.closeConnections();
    } catch (err) {
      log.error(`[worker] Error during shutdown: ${describeError(err)}`);
    }
    clearTimeout(deadline);
    finish(exitCode);
  };
}
