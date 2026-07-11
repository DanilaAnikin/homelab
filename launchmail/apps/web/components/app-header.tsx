"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";
import { ChevronRightIcon, MoonIcon, SearchIcon, SunIcon } from "lucide-react";

import { cn } from "@workspace/ui/lib/utils";
import { Button } from "@workspace/ui/components/button";
import { SidebarTrigger } from "@workspace/ui/components/sidebar";
import { AccountDropdown } from "@/components/account-dropdown";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@workspace/ui/components/command";
import {
  BarChart3Icon,
  LineChartIcon,
  ServerIcon,
  MailIcon,
  SettingsIcon,
  FileTextIcon,
  LayoutTemplateIcon,
  UsersIcon,
  SendIcon,
  GlobeIcon,
  ShieldBanIcon,
  WebhookIcon,
  type LucideIcon,
} from "lucide-react";

// Single source of truth for route → human label, used by both the breadcrumb
// trail and the ⌘K palette. Kept in sync with the sidebar IA.
interface Destination {
  href: string;
  label: string;
  icon: LucideIcon;
  group: string;
  keywords?: string;
}

const destinations: Destination[] = [
  { href: "/dashboard", label: "Overview", icon: BarChart3Icon, group: "Send", keywords: "home dashboard" },
  { href: "/dashboard/broadcasts", label: "Broadcasts", icon: SendIcon, group: "Send" },
  { href: "/dashboard/templates", label: "Templates", icon: LayoutTemplateIcon, group: "Send" },
  { href: "/dashboard/smtp", label: "SMTP servers", icon: ServerIcon, group: "Send", keywords: "transport server" },
  { href: "/dashboard/audiences", label: "Audiences", icon: UsersIcon, group: "Audience", keywords: "contacts lists" },
  { href: "/dashboard/forms", label: "Forms", icon: FileTextIcon, group: "Audience" },
  { href: "/dashboard/suppressions", label: "Suppressions", icon: ShieldBanIcon, group: "Audience", keywords: "blocklist unsubscribe" },
  { href: "/dashboard/domains", label: "Domains", icon: GlobeIcon, group: "Deliverability", keywords: "dns spf dkim" },
  { href: "/dashboard/webhooks", label: "Webhooks", icon: WebhookIcon, group: "Deliverability" },
  { href: "/dashboard/analytics", label: "Analytics", icon: LineChartIcon, group: "Deliverability", keywords: "metrics stats" },
  { href: "/dashboard/logs", label: "Email logs", icon: MailIcon, group: "Deliverability", keywords: "messages events" },
  { href: "/dashboard/organization", label: "Organization", icon: SettingsIcon, group: "Settings", keywords: "settings members billing" },
];

// Map of path segment → display label, for breadcrumb crumbs that do not have a
// dedicated destination entry (detail routes, sub-pages).
const segmentLabels: Record<string, string> = {
  dashboard: "Overview",
  analytics: "Analytics",
  smtp: "SMTP servers",
  forms: "Forms",
  templates: "Templates",
  audiences: "Audiences",
  broadcasts: "Broadcasts",
  domains: "Domains",
  suppressions: "Suppressions",
  webhooks: "Webhooks",
  logs: "Email logs",
  organization: "Organization",
  new: "New",
};

interface Crumb {
  label: string;
  href: string;
  isLast: boolean;
}

function buildCrumbs(pathname: string): Crumb[] {
  const segments = pathname.split("/").filter(Boolean);
  const crumbs: Crumb[] = [];
  let href = "";
  for (let i = 0; i < segments.length; i++) {
    const segment = segments[i]!;
    href += `/${segment}`;
    // Skip the leading "dashboard" segment as its own crumb — it is the root
    // ("Overview") and is rendered explicitly below.
    if (i === 0 && segment === "dashboard") continue;
    const label =
      segmentLabels[segment] ??
      // Dynamic/id segments: humanize without inventing data we do not have.
      segment.charAt(0).toUpperCase() + segment.slice(1);
    crumbs.push({ label, href, isLast: i === segments.length - 1 });
  }
  return crumbs;
}

// True once mounted on the client without a setState-in-effect (mirrors the
// pattern in theme-toggle.tsx so the icon does not flash on hydration).
const subscribeNoop = () => () => {};
const getMountedClient = () => true;
const getMountedServer = () => false;

export function AppHeader() {
  const pathname = usePathname();
  const router = useRouter();
  const [commandOpen, setCommandOpen] = React.useState(false);

  const crumbs = React.useMemo(() => buildCrumbs(pathname), [pathname]);

  // Global ⌘K / Ctrl+K to open the command palette.
  React.useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setCommandOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const navigateTo = React.useCallback(
    (href: string) => {
      setCommandOpen(false);
      router.push(href);
    },
    [router],
  );

  const groupedDestinations = React.useMemo(() => {
    const map = new Map<string, Destination[]>();
    for (const dest of destinations) {
      const list = map.get(dest.group) ?? [];
      list.push(dest);
      map.set(dest.group, list);
    }
    return Array.from(map.entries());
  }, []);

  return (
    <header className="sticky top-0 z-30 flex h-12 items-center gap-2 border-b bg-background/80 px-4 backdrop-blur">
      <SidebarTrigger className="md:hidden" />

      <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5">
        <Link
          href="/dashboard"
          className={cn(
            "truncate text-caption transition-colors hover:text-foreground",
            crumbs.length === 0 ? "text-foreground" : "text-muted-foreground",
          )}
        >
          Overview
        </Link>
        {crumbs.map((crumb) => (
          <React.Fragment key={crumb.href}>
            <ChevronRightIcon className="size-3.5 shrink-0 text-muted-foreground/60" />
            {crumb.isLast ? (
              <span className="truncate text-caption font-medium text-foreground">
                {crumb.label}
              </span>
            ) : (
              <Link
                href={crumb.href}
                className="truncate text-caption text-muted-foreground transition-colors hover:text-foreground"
              >
                {crumb.label}
              </Link>
            )}
          </React.Fragment>
        ))}
      </nav>

      <div className="flex flex-1 justify-center px-2">
        <button
          type="button"
          onClick={() => setCommandOpen(true)}
          className="flex h-8 w-full max-w-sm items-center gap-2 rounded-md border border-input bg-muted/40 px-2.5 text-caption text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <SearchIcon className="size-4 shrink-0 opacity-60" />
          <span className="flex-1 truncate text-left">Search…</span>
          <kbd className="pointer-events-none hidden items-center gap-0.5 rounded border border-border bg-background px-1.5 font-mono text-[10px] text-muted-foreground sm:inline-flex">
            ⌘K
          </kbd>
        </button>
      </div>

      <div className="flex shrink-0 items-center gap-1">
        <HeaderThemeToggle />
        <div className="w-auto">
          <AccountDropdown />
        </div>
      </div>

      <CommandDialog open={commandOpen} onOpenChange={setCommandOpen}>
        <Command>
          <CommandInput placeholder="Search pages…" />
          <CommandList>
            <CommandEmpty>No results found.</CommandEmpty>
            {groupedDestinations.map(([group, items]) => (
              <CommandGroup key={group} heading={group}>
                {items.map((dest) => {
                  const Icon = dest.icon;
                  return (
                    <CommandItem
                      key={dest.href}
                      value={`${dest.label} ${dest.keywords ?? ""}`}
                      onSelect={() => navigateTo(dest.href)}
                    >
                      <Icon className="text-muted-foreground" />
                      <span>{dest.label}</span>
                      {dest.href === "/dashboard" && (
                        <CommandShortcut>⌘1</CommandShortcut>
                      )}
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </CommandDialog>
    </header>
  );
}

function HeaderThemeToggle() {
  const { setTheme, resolvedTheme } = useTheme();
  const mounted = useSyncExternalStore(
    subscribeNoop,
    getMountedClient,
    getMountedServer,
  );
  const isDark = resolvedTheme === "dark";

  return (
    <Button
      variant="ghost"
      size="icon-sm"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={mounted ? (isDark ? "Switch to light mode" : "Switch to dark mode") : "Toggle theme"}
    >
      <SunIcon className="size-4 dark:hidden" />
      <MoonIcon className="hidden size-4 dark:block" />
    </Button>
  );
}
