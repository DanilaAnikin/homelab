import {
  startWorker,
  startWebhookWorker,
  startWebhookOutboxRelay,
  startInboxIdle,
  closeWebhookQueue,
  closeRedis,
  installProcessSafetyNet,
  createWorkerShutdown,
} from "@workspace/mail-queue";

console.log("LaunchMail — Mail Queue Worker (BullMQ + Redis)");
console.log("=================================================");

const worker = startWorker();
const webhookWorker = startWebhookWorker();
const webhookOutboxRelay = startWebhookOutboxRelay();
// Real-time incoming mail (IMAP IDLE) + safety poll + history backfill.
const inboxIdle = startInboxIdle();

// Graceful shutdown, bounded by a 10 s deadline so a hung close can never keep
// a broken worker alive. BullMQ closes first and waits for in-flight SMTP jobs
// (cutting one short makes it run again after the restart: a duplicate mail);
// the IMAP inbox stops at the same time with its own short limit; Redis last.
// See worker-shutdown.ts.
const shutdown = createWorkerShutdown(
  {
    mailWorker: worker,
    webhookWorker,
    webhookOutboxRelay,
    inboxIdle,
    closeConnections: async () => {
      await closeWebhookQueue();
      closeRedis();
    },
  },
  { deadlineMs: 10_000 },
);

process.on("SIGTERM", () => void shutdown("SIGTERM received", 0));
process.on("SIGINT", () => void shutdown("SIGINT received", 0));

// Unexpected errors are logged with context; an isolated IMAP connection error
// is survivable, anything else ends this process (exit 1). `start:all` then
// stops the API too and the container restarts (visible in RestartCount); see
// process-safety.ts and README.md.
installProcessSafetyNet({
  name: "worker",
  onFatal: (kind) => shutdown(`fatal ${kind}`, 1),
});
