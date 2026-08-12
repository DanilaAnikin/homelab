FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46

ENV NODE_ENV=production \
    PORT=8080

WORKDIR /app

RUN groupadd --system --gid 10001 freio-fallback \
    && useradd --system --uid 10001 --gid freio-fallback \
        --home-dir /app --shell /usr/sbin/nologin freio-fallback

COPY --chown=freio-fallback:freio-fallback server.mjs /app/server.mjs

USER freio-fallback

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:8080/healthz').then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))"

CMD ["node", "/app/server.mjs"]
