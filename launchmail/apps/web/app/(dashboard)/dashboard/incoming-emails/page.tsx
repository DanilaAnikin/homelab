import { apiGet } from "@/lib/api";
import { PageHeader } from "@/components/page-header";
import {
  MailClient,
  type Mailbox,
  type MessageSummary,
  type SentSummary,
  type MailTab,
  type Folder,
} from "./mail-client";

export const dynamic = "force-dynamic";

export default async function MailPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const tab: MailTab = sp.tab === "sent" ? "sent" : "incoming";
  const q = typeof sp.q === "string" ? sp.q : "";
  const folder: Folder =
    sp.folder === "starred" || sp.folder === "archived" ? sp.folder : "inbox";

  const mailboxes =
    (await apiGet<Mailbox[]>("/api/incoming-emails/mailboxes")) ?? [];

  const selectedId =
    typeof sp.mailbox === "string" && mailboxes.some((m) => m.id === sp.mailbox)
      ? sp.mailbox
      : (mailboxes[0]?.id ?? "");

  let incoming: MessageSummary[] = [];
  if (tab === "incoming" && selectedId) {
    const params = new URLSearchParams({
      smtpConfigId: selectedId,
      folder,
      limit: "100",
    });
    if (q) params.set("q", q);
    incoming =
      (await apiGet<MessageSummary[]>(`/api/incoming-emails?${params}`)) ?? [];
  }

  let sent: SentSummary[] = [];
  if (tab === "sent") {
    const params = new URLSearchParams({ limit: "100" });
    if (q) params.set("q", q);
    sent = (await apiGet<SentSummary[]>(`/api/sent-emails?${params}`)) ?? [];
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <PageHeader
        title="Mail"
        description="Read, reply to, and review the mail your connections send and receive."
      />
      {/* Key on the active view so changing tab/mailbox/folder/search remounts
          with fresh state (no reading pane left open on a stale message), while
          a same-view router.refresh() keeps it mounted. */}
      <MailClient
        key={`${tab}:${selectedId}:${folder}:${q}`}
        tab={tab}
        mailboxes={mailboxes}
        selectedId={selectedId}
        folder={folder}
        q={q}
        incoming={incoming}
        sent={sent}
      />
    </div>
  );
}
