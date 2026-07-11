import Link from "next/link";
import { Logo } from "@/components/logo";
import { UsersIcon } from "lucide-react";

const POINTS = [
  "Shared SMTP configs, domains, and API keys",
  "Roles and permissions for every teammate",
  "One workspace for transactional email & forms",
];

export default function InviteLayout({
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
          <span className="flex size-10 items-center justify-center rounded-xl bg-white/10">
            <UsersIcon className="size-5" />
          </span>
          <h2 className="text-section max-w-md text-balance text-white">
            You&apos;ve been invited to collaborate.
          </h2>
          <ul className="space-y-3">
            {POINTS.map((p) => (
              <li
                key={p}
                className="text-body flex items-center gap-3 text-white/70"
              >
                <span className="size-1.5 shrink-0 rounded-full bg-white/40" />
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
