import {
  startWorker,
  startWebhookWorker,
  startWebhookOutboxRelay,
  startInboxIdle,
  closeWebhookQueue,
  closeRedis,
  installProcessSafetyNet,
  describeError,
} from "@workspace/mail-queue";

console.log("LaunchMail — Mail Queue Worker (BullMQ + Redis)");
console.log("=================================================");

const worker = startWorker();
const webhookWorker = startWebhookWorker();
const webhookOutboxRelay = startWebhookOutboxRelay();
// Real-time incoming mail (IMAP IDLE) + safety poll + history backfill.
const inboxIdle = startInboxIdle();

// Graceful shutdown: stop accepting new jobs, finish in-flight ones, then close
// the Redis connection before exiting so the orchestrator (Docker/PID1) sees a
// clean exit and doesn't leave a half-closed connection behind. Bounded, so a
// hung close (e.g. a job stuck on a dead SMTP connection) can never keep a
// broken worker alive: the supervisor only restarts a process that exited.
const SHUTDOWN_DEADLINE_MS = 10_000;
let shuttingDown = false;
async function shutdown(reason: string, exitCode: number): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[worker] ${reason} — shutting down...`);
  const deadline = setTimeout(() => {
    console.error(
      `[worker] shutdown still running after ${SHUTDOWN_DEADLINE_MS / 1000} s — exiting anyway`,
    );
    process.exit(exitCode);
  }, SHUTDOWN_DEADLINE_MS);
  try {
    await inboxIdle.stop();
    await webhookOutboxRelay.stop();
    await worker.close();
    await webhookWorker.close();
    await closeWebhookQueue();
    await closeRedis();
  } catch (err) {
    console.error(`[worker] Error during shutdown: ${describeError(err)}`);
  }
  clearTimeout(deadline);
  process.exit(exitCode);
}

process.on("SIGTERM", () => void shutdown("SIGTERM received", 0));
process.on("SIGINT", () => void shutdown("SIGINT received", 0));

// Unexpected errors are logged with context; an isolated IMAP connection error
// is survivable, anything else ends this process (exit 1) and `start:all`
// restarts only the worker — the API keeps serving. See process-safety.ts.
installProcessSafetyNet({
  name: "worker",
  onFatal: (kind) => shutdown(`fatal ${kind}`, 1),
});
