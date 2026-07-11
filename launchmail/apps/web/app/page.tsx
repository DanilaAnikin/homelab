import Link from "next/link"
import { redirect } from "next/navigation"
import {
  ArrowRightIcon,
  InboxIcon,
  MegaphoneIcon,
  PlugIcon,
  ServerIcon,
} from "lucide-react"

import { Button } from "@workspace/ui/components/button"
import { Logo } from "@/components/logo"
import { TechStack } from "@/components/tech-stack"
import { getSession } from "@/lib/utils"

// Reads the session cookie (via getSession) to decide whether to send an
// already-authenticated user straight to their dashboard, so it must render
// dynamically rather than be statically prerendered.
export const dynamic = "force-dynamic"

const GITHUB_URL = "https://github.com/launchday/launchmail"

// lucide-react v1 dropped brand glyphs, so the GitHub mark is inlined (matching
// the inline-SVG convention already used by the TechStack credibility strip).
function GithubIcon() {
  return (
    <svg fill="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0 1 12 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222 0 1.606-.014 2.898-.014 3.293 0 .322.216.694.825.576C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
    </svg>
  )
}

const features = [
  {
    icon: ServerIcon,
    title: "Bring your own SMTP",
    description:
      "Connect any transport — Postfix, SES, Resend, your own relay. Credentials are encrypted at rest and never leave your infrastructure.",
  },
  {
    icon: InboxIcon,
    title: "Forms",
    description:
      "Spin up hosted endpoints for contact and signup forms. Spam-filtered submissions land in one place, with signed unsubscribe links built in.",
  },
  {
    icon: MegaphoneIcon,
    title: "Broadcasts",
    description:
      "Send to whole audiences with per-message tracking. An idempotent worker retries failures and surfaces a real failed count, not a guess.",
  },
  {
    icon: PlugIcon,
    title: "MCP server",
    description:
      "Drive sending, forms, and logs from any agent over the Model Context Protocol. Your tooling talks to LaunchMail directly.",
  },
]

export default async function Home() {
  const session = await getSession()

  // Authenticated users skip the marketing page and go straight to the app.
  if (session) {
    redirect("/dashboard")
  }

  return (
    <main className="flex min-h-svh flex-col">
      {/* Top bar */}
      <header className="sticky top-0 z-10 border-b border-border bg-background/80 backdrop-blur">
        <div className="mx-auto flex h-14 w-full max-w-6xl items-center justify-between px-6 lg:px-8">
          <Logo className="h-6 w-auto" />
          <nav className="flex items-center gap-2">
            <Button variant="ghost" size="sm" asChild>
              <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
                <GithubIcon />
                GitHub
              </a>
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link href="/login">Sign in</Link>
            </Button>
            <Button size="sm" asChild>
              <Link href="/sign-up">Get started</Link>
            </Button>
          </nav>
        </div>
      </header>

      {/* Hero */}
      <section className="relative overflow-hidden border-b border-border bg-[radial-gradient(120%_120%_at_50%_0%,var(--brand-50),transparent_60%)] dark:bg-[radial-gradient(120%_120%_at_50%_0%,var(--brand-subtle),transparent_55%)]">
        <div className="mx-auto flex w-full max-w-3xl flex-col items-center px-6 py-24 text-center lg:px-8 lg:py-32">
          <span className="inline-flex items-center gap-2 rounded-full border border-brand-200 bg-brand-100 px-3 py-1 text-eyebrow text-brand-800 dark:border-transparent dark:bg-brand-subtle dark:text-brand-emphasis">
            Self-hostable email platform
          </span>
          <h1 className="mt-6 text-balance text-4xl font-semibold tracking-tight sm:text-5xl">
            Transactional email and forms you actually own
          </h1>
          <p className="mt-5 text-pretty text-base text-muted-foreground sm:text-lg">
            LaunchMail is a self-hostable platform for sending, forms,
            broadcasts, and logs — running on your own SMTP transport, with an
            MCP server so your agents can drive it directly.
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
            <Button size="lg" asChild>
              <Link href="/sign-up">
                Get started
                <ArrowRightIcon />
              </Link>
            </Button>
            <Button variant="outline" size="lg" asChild>
              <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
                <GithubIcon />
                View on GitHub
              </a>
            </Button>
          </div>
        </div>
      </section>

      {/* Feature grid */}
      <section className="border-b border-border">
        <div className="mx-auto w-full max-w-6xl px-6 py-20 lg:px-8 lg:py-24">
          <div className="mx-auto max-w-2xl text-center">
            <p className="text-eyebrow text-brand-600 dark:text-brand-400">
              What you get
            </p>
            <h2 className="mt-3 text-section sm:text-3xl">
              Everything to run email yourself
            </h2>
            <p className="mt-3 text-pretty text-muted-foreground">
              The pieces of a managed email service, on infrastructure you
              control.
            </p>
          </div>
          <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {features.map((feature) => (
              <div
                key={feature.title}
                className="flex flex-col rounded-xl border border-border bg-card p-6 shadow-[var(--elevation-1)] transition-shadow duration-[var(--duration-fast)] ease-[var(--ease-standard)] hover:shadow-[var(--elevation-2)]"
              >
                <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-brand-100 text-brand-700 dark:bg-brand-subtle dark:text-brand-emphasis">
                  <feature.icon className="size-5" />
                </div>
                <h3 className="mt-4 text-card-title">{feature.title}</h3>
                <p className="mt-2 text-pretty text-caption text-muted-foreground">
                  {feature.description}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Tech-stack credibility strip */}
      <section className="border-b border-border bg-surface">
        <div className="mx-auto w-full max-w-6xl px-6 py-16 lg:px-8">
          <p className="text-center text-eyebrow text-muted-foreground">
            Built with
          </p>
          <div className="mt-8">
            <TechStack />
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="mt-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col items-center justify-between gap-4 px-6 py-10 sm:flex-row lg:px-8">
          <Logo className="h-5 w-auto text-muted-foreground" />
          <p className="text-caption text-muted-foreground">
            Self-hostable email infrastructure for builders.
          </p>
          <div className="flex items-center gap-4">
            <Link
              href="/login"
              className="text-caption text-muted-foreground transition-colors hover:text-foreground"
            >
              Sign in
            </Link>
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-caption text-muted-foreground transition-colors hover:text-foreground"
            >
              GitHub
            </a>
          </div>
        </div>
      </footer>
    </main>
  )
}
