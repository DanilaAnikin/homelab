import type { Metadata } from "next"
import { Geist, Geist_Mono } from "next/font/google"

import "@workspace/ui/globals.css"
import { ThemeProvider } from "@/components/theme-provider"
import { Toaster } from "@workspace/ui/components/sonner"
import { cn } from "@workspace/ui/lib/utils";

const geist = Geist({ subsets: ['latin'], variable: '--font-sans' })

const fontMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
})

export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.BETTER_AUTH_URL ?? "http://localhost:3000"
  ),
  title: {
    default: "LaunchMail — Self-hostable email platform",
    template: "%s · LaunchMail",
  },
  description:
    "Self-hostable transactional email, forms, and broadcasts on your own SMTP transport — with an MCP server so your agents can drive it directly.",
  applicationName: "LaunchMail",
  openGraph: {
    title: "LaunchMail — Self-hostable email platform",
    description:
      "Self-hostable transactional email, forms, and broadcasts on your own SMTP transport — with an MCP server so your agents can drive it directly.",
    siteName: "LaunchMail",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "LaunchMail — Self-hostable email platform",
    description:
      "Self-hostable transactional email, forms, and broadcasts on your own SMTP transport.",
  },
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={cn("antialiased", fontMono.variable, "font-sans", geist.variable)}
    >
      <body className="flex min-h-svh flex-col">
        <ThemeProvider>
          <div className="flex-1 overflow-y-auto">
            {children}
          </div>
          <Toaster position="bottom-right" />
        </ThemeProvider>
      </body>
    </html>
  )
}
