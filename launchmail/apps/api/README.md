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

The API does not need a running worker to accept mail (jobs wait in Redis), so
a crashed process is restarted on its own: `--restart-tries 10 --restart-after
5000` restarts it in place after 5 s while the other one keeps running. Only
when one process keeps failing (more than 10 restarts in the container's
lifetime) or exits unexpectedly with code 0, `--kill-others` stops the other
one too and Docker restarts the whole container, which shows up in
`RestartCount`.

Until 20. 10. 2026 the script used `--kill-others-on-fail` without restarts:
every worker crash (an unhandled IMAP `error` event) also killed the API, and
the container restarted ~20× a day.

Both processes install `installProcessSafetyNet` (`@workspace/mail-queue`): an
unexpected `uncaughtException`/`unhandledRejection` is logged with its stack
frames and ends that process through a bounded graceful shutdown with exit
code 1; only an isolated ImapFlow connection error (already closed by
imapflow) is logged and survived.
