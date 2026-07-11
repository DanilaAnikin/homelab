import { createAuthClient } from "better-auth/react"
import { organizationClient } from "better-auth/client/plugins"
import { ac, admin, writer, reader } from "@workspace/auth/permissions"

// Always talk to the current origin; Next rewrites /api/* to the backend.
// This keeps auth same-origin in every environment (no CORS, no baked URL).
// We intentionally do NOT set baseURL: Better Auth defaults to the current
// origin, which is exactly what the same-origin proxy needs. Pointing it at
// NEXT_PUBLIC_API_URL would bypass the proxy and break in the browser.
const TOKEN_COOKIE = "lm_token"

function getToken(): string {
  if (typeof document === "undefined") return ""
  const match = document.cookie.match(/(?:^|; )lm_token=([^;]*)/)
  return match ? decodeURIComponent(match[1]!) : ""
}

function setToken(token: string) {
  if (typeof document === "undefined") return
  const secure = location.protocol === "https:" ? "; secure" : ""
  document.cookie = `${TOKEN_COOKIE}=${encodeURIComponent(token)}; path=/; max-age=${60 * 60 * 24 * 7}; samesite=lax${secure}`
}

export function clearToken() {
  if (typeof document === "undefined") return
  document.cookie = `${TOKEN_COOKIE}=; path=/; max-age=0`
}

export const authClient = createAuthClient({
  plugins: [
    organizationClient({
      ac,
      roles: { admin, writer, reader },
    }),
  ],
  fetchOptions: {
    credentials: "omit",
    auth: {
      type: "Bearer",
      token: () => getToken(),
    },
    onSuccess: (ctx) => {
      const token = ctx.response.headers.get("set-auth-token")
      if (token) setToken(token)
    },
  },
})
