import {
  startWorker,
  startInboxIdle,
  closeRedis,
} from "@workspace/mail-queue";

console.log("LaunchMail — Mail Queue Worker (BullMQ + Redis)");
console.log("=================================================");

const worker = startWorker();
// Real-time incoming mail (IMAP IDLE) + safety poll + history backfill.
const inboxIdle = startInboxIdle();

// Graceful shutdown: stop accepting new jobs, finish in-flight ones, then close
// the Redis connection before exiting so the orchestrator (Docker/PID1) sees a
// clean exit and doesn't leave a half-closed connection behind.
let shuttingDown = false;
async function shutdown(signal: string): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[worker] ${signal} received — shutting down...`);
  try {
    await inboxIdle.stop();
    await worker.close();
    await closeRedis();
  } catch (err) {
    console.error(
      `[worker] Error during shutdown: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  process.exit(0);
}

process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));
