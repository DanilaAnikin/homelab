"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@workspace/ui/lib/utils";
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
  InboxIcon,
  type LucideIcon,
} from "lucide-react";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@workspace/ui/components/sidebar";
import { Logo } from "@/components/logo";
import { ThemeToggle } from "@/components/theme-toggle";
import { AccountDropdown } from "@/components/account-dropdown";
import { OrgSwitcher } from "@/components/org-switcher";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Key used to attach a live count badge (resolved in DashboardSidebar). */
  badgeKey?: "inboxUnread";
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

// Grouped IA: Send / Audience / Deliverability. Settings (Organization) is
// pinned to the footer near the account control. Every href below maps to an
// existing dashboard route — keep these in sync with app/(dashboard)/dashboard.
const navGroups: NavGroup[] = [
  {
    label: "Send",
    items: [
      { href: "/dashboard", label: "Overview", icon: BarChart3Icon },
      { href: "/dashboard/broadcasts", label: "Broadcasts", icon: SendIcon },
      { href: "/dashboard/templates", label: "Templates", icon: LayoutTemplateIcon },
      { href: "/dashboard/smtp", label: "SMTP servers", icon: ServerIcon },
    ],
  },
  {
    label: "Inbox",
    items: [
      {
        href: "/dashboard/incoming-emails",
        label: "Mail",
        icon: InboxIcon,
        badgeKey: "inboxUnread",
      },
    ],
  },
  {
    label: "Audience",
    items: [
      { href: "/dashboard/audiences", label: "Audiences", icon: UsersIcon },
      { href: "/dashboard/forms", label: "Forms", icon: FileTextIcon },
      { href: "/dashboard/suppressions", label: "Suppressions", icon: ShieldBanIcon },
    ],
  },
  {
    label: "Deliverability",
    items: [
      { href: "/dashboard/domains", label: "Domains", icon: GlobeIcon },
      { href: "/dashboard/webhooks", label: "Webhooks", icon: WebhookIcon },
      { href: "/dashboard/analytics", label: "Analytics", icon: LineChartIcon },
      { href: "/dashboard/logs", label: "Email logs", icon: MailIcon },
    ],
  },
];

const settingsItem: NavItem = {
  href: "/dashboard/organization",
  label: "Organization",
  icon: SettingsIcon,
};

function isItemActive(href: string, pathname: string): boolean {
  return href === "/dashboard"
    ? pathname === "/dashboard"
    : pathname.startsWith(href);
}

export function DashboardSidebar({
  inboxUnread = 0,
}: {
  inboxUnread?: number;
}) {
  const pathname = usePathname();

  return (
    <Sidebar collapsible="offcanvas">
      <SidebarHeader className="gap-2 p-3">
        <Link
          href="/dashboard"
          className="flex h-9 items-center px-2 transition-opacity hover:opacity-80"
        >
          <Logo className="h-6 w-auto" />
        </Link>
        <OrgSwitcher />
      </SidebarHeader>

      <SidebarContent className="px-1">
        {navGroups.map((group) => (
          <SidebarGroup key={group.label}>
            <SidebarGroupLabel className="text-eyebrow text-muted-foreground">
              {group.label}
            </SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {group.items.map((item) => (
                  <NavMenuItem
                    key={item.href}
                    item={item}
                    active={isItemActive(item.href, pathname)}
                    badge={
                      item.badgeKey === "inboxUnread" ? inboxUnread : 0
                    }
                  />
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>

      <SidebarFooter className="gap-1 p-3">
        <SidebarMenu>
          <NavMenuItem
            item={settingsItem}
            active={isItemActive(settingsItem.href, pathname)}
          />
        </SidebarMenu>
        <AccountDropdown />
        <ThemeToggle />
      </SidebarFooter>
    </Sidebar>
  );
}

function NavMenuItem({
  item,
  active,
  badge = 0,
}: {
  item: NavItem;
  active: boolean;
  badge?: number;
}) {
  const { icon: Icon } = item;
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        asChild
        isActive={active}
        // Active item: 2px --brand-500 accent spine on the left, --brand-100
        // fill + --brand-800 label come from the sidebar-accent tokens that the
        // SidebarMenuButton applies via data-active.
        className={cn(
          "relative",
          active &&
            "before:absolute before:inset-y-1 before:left-0 before:w-0.5 before:rounded-full before:bg-brand-500",
        )}
      >
        <Link href={item.href}>
          <Icon className={cn(active ? "text-brand-700" : "text-muted-foreground")} />
          <span>{item.label}</span>
          {badge > 0 && (
            <span className="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-brand-500 px-1.5 text-[11px] font-semibold tabular-nums text-white">
              {badge > 99 ? "99+" : badge}
            </span>
          )}
        </Link>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}
