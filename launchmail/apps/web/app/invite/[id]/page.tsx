import Link from "next/link";
import { MailWarningIcon } from "lucide-react";
import { apiGet } from "@/lib/api";
import { getSession } from "@/lib/utils";
import { Button } from "@workspace/ui/components/button";
import { AcceptInvite } from "./accept-invite";

export const dynamic = "force-dynamic";

interface InviteInfo {
  id: string;
  email: string;
  role: string | null;
  status: string;
  expiresAt: string;
  orgName: string;
  inviterName: string;
}

export default async function InvitePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  const invite = await apiGet<InviteInfo>(`/public/invitation/${id}`);
  const session = await getSession();

  const invalid =
    !invite ||
    invite.status !== "pending" ||
    new Date(invite.expiresAt) < new Date();

  if (invalid) {
    return (
      <div className="space-y-6">
        <span className="flex size-12 items-center justify-center rounded-xl bg-destructive/10 text-destructive">
          <MailWarningIcon className="size-6" />
        </span>
        <div className="space-y-1.5">
          <h1 className="text-display text-foreground">Invitation unavailable</h1>
          <p className="text-caption text-muted-foreground">
            This invitation is invalid, has already been used, or has expired.
            Ask an admin to send you a new one.
          </p>
        </div>
        <Button asChild className="w-full">
          <Link href="/dashboard">Go to dashboard</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <span className="flex size-12 items-center justify-center rounded-xl bg-brand/10 text-brand text-lg font-semibold">
        {invite!.orgName.charAt(0).toUpperCase()}
      </span>
      <div className="space-y-1.5">
        <h1 className="text-display text-foreground">
          Join {invite!.orgName}
        </h1>
        <p className="text-caption text-muted-foreground">
          {invite!.inviterName} invited you to join{" "}
          <span className="font-medium text-foreground">{invite!.orgName}</span>{" "}
          as {invite!.role ?? "member"}.
        </p>
      </div>

      {session ? (
        <AcceptInvite invitationId={invite!.id} />
      ) : (
        <div className="space-y-3">
          <p className="text-body text-muted-foreground">
            Sign in or create an account with{" "}
            <span className="font-medium text-foreground">{invite!.email}</span>,
            then reopen this link to accept.
          </p>
          <div className="flex gap-2">
            <Button asChild className="flex-1">
              <Link href={`/sign-up?redirect=/invite/${invite!.id}`}>
                Create account
              </Link>
            </Button>
            <Button asChild variant="outline" className="flex-1">
              <Link href={`/login?redirect=/invite/${invite!.id}`}>Sign in</Link>
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
