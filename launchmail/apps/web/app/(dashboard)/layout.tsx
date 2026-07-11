import { getSession } from "@/lib/utils";
import { apiFetch } from "@/lib/api";
import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import {
  SidebarInset,
  SidebarProvider,
} from "@workspace/ui/components/sidebar";
import { DashboardSidebar } from "@/components/dashboard-sidebar";
import { AppHeader } from "@/components/app-header";

// Total unread across all receive-enabled mailboxes, for the sidebar badge.
// Fully best-effort: a hiccup here must never break the dashboard shell, so it
// uses apiFetch directly (no 401→/login redirect) and swallows everything.
async function inboxUnreadCount(): Promise<number> {
  try {
    const res = await apiFetch("/api/incoming-emails/mailboxes");
    if (!res.ok) return 0;
    const mailboxes = (await res.json()) as { unread?: number }[];
    return mailboxes.reduce((sum, m) => sum + (m.unread ?? 0), 0);
  } catch {
    return 0;
  }
}

// Every dashboard page (and this layout) reads the lm_token cookie via
// next/headers, so the whole segment must be dynamically rendered. Declaring it
// here once makes all current and future dashboard routes inherit it, instead
// of relying on each page remembering to opt out of static prerendering.
export const dynamic = "force-dynamic";

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await getSession();
  if (!session) redirect("/login");

  // Persist the rail's collapsed/expanded state across reloads (cookie written
  // by SidebarProvider). Reading it here keeps SSR and the client in sync so
  // the rail does not flash open on a collapsed reload.
  const cookieStore = await cookies();
  const defaultOpen = cookieStore.get("sidebar_state")?.value !== "false";
  const inboxUnread = await inboxUnreadCount();

  return (
    <SidebarProvider defaultOpen={defaultOpen}>
      <DashboardSidebar inboxUnread={inboxUnread} />
      <SidebarInset className="min-w-0 bg-background">
        <AppHeader />
        <div className="mx-auto w-full max-w-6xl px-6 py-8 lg:px-8">
          {children}
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
