```
npm install
npm run dev
```

```
open http://localhost:3000
```

## Processes in production (`pnpm run start:all`)

The `launchmail-api` container runs two Node processes under `concurrently`:

- `api` (`src/server.ts`) — HTTP API; it only enqueues mail into Redis.
- `worker` (`src/worker.ts`) — BullMQ send/webhook workers and the IMAP inbox
  (IDLE + safety poll).

```
concurrently --kill-others --kill-timeout 15000 -n api,worker -c blue,green \
  "node --import tsx src/server.ts" "node --import tsx src/worker.ts"
```

**Either process ends → both end → Docker restarts the container**
(`restart: unless-stopped`), so every crash is visible in `RestartCount`.
`--kill-others` (not `--kill-others-on-fail`) also covers a process that exits
with code 0 on its own: a container in which only the API runs and nothing
sends mail must not exist. `--kill-timeout 15000` SIGKILLs a process that is
still alive 15 s after the SIGTERM (each process has its own 10 s shutdown
deadline, so this only hits a blocked event loop).

**No restarts in place (`--restart-tries`).** concurrently 10.0.3 restarts every
process that exits non-zero, including the one `--kill-others` has just stopped
(it does not check `killed`, and a death by signal counts as exit code
`"SIGTERM"`). Once the worker had used up its restarts, the API could be
restarted on its own and the container kept running without a worker, with a
green `/health`. Restarts in place would also hide crashes from `RestartCount`.

**`node --import tsx`, not the `tsx` CLI.** The CLI runs the script in a child
process and relays signals to it: it waits 30 ms for the child to acknowledge a
SIGTERM and, when the child's event loop is busy for longer, SIGKILLs it and
exits with 143 (tsx 4.21). The worker's graceful shutdown then never ran: BullMQ
did not get to finish SMTP jobs that were already being sent, and such a job
runs again after the restart, so the mail goes out twice. With
`node --import tsx` there is no relay; the signal reaches the process that
handles it. That matters because concurrently signals only its direct child:
in the Alpine image busybox `ps` has no `--ppid`, so tree-kill never finds the
grandchildren. Same loader and the same TypeScript as the CLI; `start` and
`start:worker` (the mail egress node) use it too. `dev` keeps `tsx watch`.

Until 20. 10. 2026 the script ran the `tsx` CLI under `--kill-others-on-fail`.
The crash loop of 16.–20. 10. 2026 (IMAP clients without an `error` listener
and one message whose NULs wedged a mailbox) is fixed in code, so a container
restart should now be rare, and when it happens it is visible.

Both processes install `installProcessSafetyNet` (`@workspace/mail-queue`): an
unexpected `uncaughtException`/`unhandledRejection` is logged with its stack
frames and ends that process through a bounded graceful shutdown with exit
code 1; only an isolated ImapFlow connection error (already closed by
imapflow) is logged and survived.

Worker shutdown (`createWorkerShutdown`, 10 s deadline): the BullMQ send and
webhook workers and the webhook outbox relay start closing at once, BullMQ
waits for the jobs already running. The IMAP inbox stops in parallel and
bounded (LOGOUT at most 2 s per connection, then the socket is closed; the
whole step at most 3 s), so a slow IMAP server cannot use up the deadline the
SMTP jobs need. Redis is closed last.
