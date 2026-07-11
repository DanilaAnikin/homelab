import Link from "next/link";
import { Logo } from "@/components/logo";
import { CheckIcon } from "lucide-react";

const POINTS = [
  "Bring your own SMTP — no vendor lock-in",
  "Formspree-style endpoints for any HTML form",
  "Teams, roles, API keys, and a built-in MCP server",
];

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <div className="relative hidden flex-col justify-between overflow-hidden bg-neutral-950 p-12 text-white lg:flex">
        {/* Brand wash driven from --brand (no hard-coded rgba) */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              "radial-gradient(60% 50% at 20% 0%, color-mix(in oklch, var(--brand-500) 32%, transparent), transparent 70%), radial-gradient(50% 45% at 100% 100%, color-mix(in oklch, var(--brand-400) 20%, transparent), transparent 70%)",
          }}
        />
        <Link href="/" className="relative">
          <Logo className="h-7 w-auto text-white" />
        </Link>
        <div className="relative space-y-6">
          <h2 className="text-section max-w-md text-balance text-white">
            Self-hosted transactional email &amp; forms, on your own SMTP.
          </h2>
          <ul className="space-y-3">
            {POINTS.map((p) => (
              <li key={p} className="text-body flex items-center gap-3 text-white/70">
                <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-white/10">
                  <CheckIcon className="size-3" />
                </span>
                {p}
              </li>
            ))}
          </ul>
        </div>
        <p className="text-caption relative text-white/40">
          © {new Date().getFullYear()} LaunchMail
        </p>
      </div>

      <div className="flex items-center justify-center p-6 sm:p-10">
        <div className="w-full max-w-sm">
          <div className="mb-8 lg:hidden">
            <Logo className="h-7 w-auto" />
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}
