/* global process */
/** @type {import('next').NextConfig} */
const API_URL = (process.env.API_URL ?? "http://localhost:5000").replace(
  /\/$/,
  "",
)

const nextConfig = {
  output: "standalone",
  transpilePackages: [
    "@workspace/ui",
    "@workspace/auth",
    "@workspace/db",
    "@workspace/mail-queue",
    "@workspace/server",
    "@workspace/templates",
  ],
  // Same-origin proxy to the backend API. The browser only ever talks to the
  // web origin (e.g. mail.launchday.cz); Next forwards these paths to the
  // backend (http://api:5000 in prod, http://localhost:5000 in dev). This
  // avoids CORS, mixed-content, and any NEXT_PUBLIC_API_URL build-time baking.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_URL}/api/:path*` },
      { source: "/f/:path*", destination: `${API_URL}/f/:path*` },
      { source: "/t/:path*", destination: `${API_URL}/t/:path*` },
      { source: "/u/:path*", destination: `${API_URL}/u/:path*` },
      { source: "/public/:path*", destination: `${API_URL}/public/:path*` },
    ]
  },
}

export default nextConfig
