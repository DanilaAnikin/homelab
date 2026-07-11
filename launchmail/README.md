# Launchday Stack

A modern full-stack monorepo template with Next.js 16, Better Auth, Drizzle ORM, and PostgreSQL.

## Tech Stack

| Layer | Choice |
|-------|--------|
| **Framework** | [Next.js 16](https://nextjs.org) (App Router, Turbopack) |
| **Auth** | [Better Auth](https://better-auth.com) (email/password) |
| **ORM** | [Drizzle ORM](https://orm.drizzle.team) |
| **Database** | [PostgreSQL](https://postgresql.org) |
| **Validation** | [Zod](https://zod.dev) |
| **API Layer** | [Hono](https://hono.dev) |
| **Language** | [TypeScript](https://typescriptlang.org) |
| **Styling** | [Tailwind CSS v4](https://tailwindcss.com) + [shadcn/ui](https://ui.shadcn.com) |
| **Monorepo** | [Turborepo](https://turbo.build) + [pnpm](https://pnpm.io) workspaces |

## Prerequisites

- **Node.js** >= 20
- **pnpm** >= 9 (install: `npm i -g pnpm`)
- **PostgreSQL** running locally (or a remote connection string)

## Setup

```bash
# Install dependencies
pnpm install

# Copy environment variables
cp .env.example .env

# Update .env with your database URL and auth secret
# Generate a secure secret: pnpm exec better-auth secret

# Push the database schema
pnpm --filter @workspace/db run db:push
```

## Development

```bash
pnpm dev
```

Starts all workspaces in development mode. The web app is available at `http://localhost:3000`.

## Project Structure

```
├── apps/
│   └── web/                    # Next.js application
│       ├── app/
│       │   ├── (auth)/         # Login & sign-up pages
│       │   ├── account/        # Account & session management
│       │   ├── api/auth/       # Better Auth API handler
│       │   ├── layout.tsx      # Root layout
│       │   └── page.tsx        # Home page
│       └── components/         # App-specific components
│           ├── auth-navbar.tsx
│           ├── logo.tsx
│           ├── tech-stack.tsx
│           └── theme-provider.tsx
│
├── packages/
│   ├── auth/                   # Better Auth server configuration
│   ├── db/                     # Drizzle ORM schema & client
│   ├── ui/                     # Shared UI components (shadcn)
│   │   └── src/components/     # Installed shadcn components
│   ├── eslint-config/          # Shared ESLint configuration
│   └── typescript-config/      # Shared TypeScript configuration
│
├── .env                        # Environment variables (gitignored)
├── .env.example                # Environment variable template
├── turbo.json                  # Turborepo configuration
└── pnpm-workspace.yaml         # pnpm workspace definition
```

## Available Commands

| Command | Description |
|---------|-------------|
| `pnpm dev` | Start all workspaces in dev mode |
| `pnpm build` | Build all workspaces |
| `pnpm lint` | Lint all workspaces |
| `pnpm typecheck` | Run TypeScript type checking |
| `pnpm format` | Format code with Prettier |
| `pnpm --filter web ...` | Run command only for the web app |
| `pnpm --filter @workspace/db run db:push` | Push Drizzle schema to database |
| `pnpm --filter @workspace/db run db:generate` | Generate Drizzle migrations |
| `pnpm --filter @workspace/db run db:migrate` | Apply Drizzle migrations |
| `pnpm --filter @workspace/db run db:studio` | Open Drizzle Studio |

## Adding shadcn Components

```bash
pnpm dlx shadcn@latest add button -c apps/web
```

Components are installed into `packages/ui/src/components/` and can be imported from `@workspace/ui/components/component-name`.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `BETTER_AUTH_SECRET` | Secret key for signing auth tokens (min 32 chars) |
| `BETTER_AUTH_URL` | Public URL of your app (e.g. `http://localhost:3000`) |
