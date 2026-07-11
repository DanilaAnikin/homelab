FROM node:20-alpine AS base

FROM base AS pruner
RUN --mount=type=cache,target=/root/.npm \
    npm install -g turbo
WORKDIR /app
COPY . .
RUN turbo prune web --docker

FROM base AS installer
WORKDIR /app
COPY --from=pruner /app/out/json/ .
COPY --from=pruner /app/out/pnpm-lock.yaml .
RUN --mount=type=cache,target=/root/.local/share/pnpm/store,id=pnpm \
    --mount=type=cache,target=/root/.npm \
    npm install -g pnpm && pnpm install --frozen-lockfile

FROM base AS builder
WORKDIR /app
# Next.js bakes the next.config rewrite destinations into routes-manifest.json
# at BUILD time, so the backend proxy target must be set here (not just at
# runtime). Defaults to the in-cluster service name; override via build args.
ARG API_URL=http://api:5000
ENV API_URL=$API_URL
COPY --from=pruner /app/out/pnpm-lock.yaml ./
COPY --from=installer /app/node_modules ./node_modules
COPY --from=pruner /app/out/full/ .
RUN --mount=type=cache,target=/root/.local/share/pnpm/store,id=pnpm \
    --mount=type=cache,target=/app/.turbo,id=turbo-web \
    npm install -g pnpm turbo && pnpm install --offline && pnpm turbo run build --filter=web
RUN mkdir -p apps/web/public

FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./apps/web/.next/static
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/public ./apps/web/public
USER nextjs
EXPOSE 3000
ENV PORT=3000
CMD ["node", "apps/web/server.js"]
