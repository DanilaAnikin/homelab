"use client"

import { useState } from "react"
import Link from "next/link"
import { EyeIcon, EyeOffIcon } from "lucide-react"
import { authClient } from "@/lib/auth-client"
import { Button } from "@workspace/ui/components/button"
import { Input } from "@workspace/ui/components/input"
import { Label } from "@workspace/ui/components/label"

// Only allow relative, same-origin paths to prevent open-redirect attacks via
// ?redirect=. Protocol-relative ("//evil.com") and absolute URLs are rejected.
function safeRedirect(raw: string | null): string {
  return raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/dashboard"
}

export default function LoginPage() {
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)

    const redirectTo = safeRedirect(
      new URLSearchParams(window.location.search).get("redirect"),
    )
    const { error: authError } = await authClient.signIn.email({
      email,
      password,
      callbackURL: redirectTo,
    })

    if (authError) {
      setError(authError.message ?? "Something went wrong")
      setLoading(false)
    } else {
      // Full-page navigation (not router.push): the session lives in the
      // lm_token cookie that better-auth's onSuccess just wrote client-side, and
      // a soft RSC navigation can serve a prefetched logged-out /dashboard from
      // the router cache (its RSC render ran getSession before the cookie
      // existed) -> instant bounce back to /login. A hard load guarantees the
      // server renders /dashboard with the cookie present.
      window.location.href = redirectTo
    }
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1.5">
        <h1 className="text-display text-foreground">Welcome back</h1>
        <p className="text-caption text-muted-foreground">
          Sign in to your LaunchMail workspace.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div
            role="alert"
            className="text-body rounded-lg border border-destructive/20 bg-destructive/10 px-3 py-2 text-destructive"
          >
            {error}
          </div>
        )}
        <div className="space-y-2">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={error ? true : undefined}
            required
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="password">Password</Label>
          <div className="relative">
            <Input
              id="password"
              type={showPassword ? "text" : "password"}
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-invalid={error ? true : undefined}
              className="pr-9"
              required
            />
            <button
              type="button"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              className="absolute inset-y-0 right-0 flex w-9 items-center justify-center rounded-r-md text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              {showPassword ? (
                <EyeOffIcon className="size-4" />
              ) : (
                <EyeIcon className="size-4" />
              )}
            </button>
          </div>
        </div>
        <Button type="submit" className="w-full" loading={loading}>
          {loading ? "Signing in…" : "Sign in"}
        </Button>
      </form>

      <p className="text-caption text-center text-muted-foreground">
        Don&apos;t have an account?{" "}
        <Link
          href="/sign-up"
          className="font-medium text-foreground underline-offset-4 hover:underline"
        >
          Sign up
        </Link>
      </p>
    </div>
  )
}
