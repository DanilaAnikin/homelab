"use client";

import { useState, useRef } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { FormField, FormLabel, FormControl, FormDescription } from "@workspace/ui/components/form-field";
import { Separator } from "@workspace/ui/components/separator";
import { toast } from "@workspace/ui/components/sonner";
import { CheckCircle2Icon, AlertCircleIcon, PlugIcon } from "lucide-react";
import { createSmtpConfigAction, testImapRawAction } from "./actions";

export function NewSmtpForm({
  verifiedDomains,
}: {
  verifiedDomains: string[];
}) {
  const router = useRouter();
  const formRef = useRef<HTMLFormElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [testingImap, setTestingImap] = useState(false);
  const [fromAddress, setFromAddress] = useState("");

  async function testImap() {
    const fd = new FormData(formRef.current!);
    const imapHost = ((fd.get("imapHost") as string) || "").trim();
    const imapPassword = (fd.get("imapPassword") as string) || "";
    if (!imapHost) return toast.error("Enter the IMAP host first");
    if (!imapPassword) return toast.error("Enter the IMAP password to test");
    setTestingImap(true);
    const res = await testImapRawAction({
      imapHost,
      imapPort: parseInt(fd.get("imapPort") as string) || 993,
      imapUsername: ((fd.get("imapUsername") as string) || "").trim() || undefined,
      imapPassword,
      username: ((fd.get("username") as string) || "").trim() || undefined,
    });
    setTestingImap(false);
    if (res.success) toast.success("IMAP connection works");
    else toast.error(res.error || "IMAP connection failed");
  }

  const fromDomain = fromAddress.includes("@")
    ? fromAddress.split("@")[1]?.toLowerCase().trim()
    : "";
  const isVerified = !!fromDomain && verifiedDomains.includes(fromDomain);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const result = await createSmtpConfigAction(new FormData(e.currentTarget));
    if (result.error) {
      setError(result.error);
      setLoading(false);
    } else {
      router.push(`/dashboard/smtp/${result.id}`);
    }
  }

  return (
    <form ref={formRef} onSubmit={handleSubmit} className="space-y-6">
      {error && (
        <div className="rounded-lg bg-destructive/10 px-4 py-3 text-body text-destructive">
          {error}
        </div>
      )}

      <FormField>
        <FormLabel>Display name</FormLabel>
        <FormControl>
          <Input name="name" placeholder="SendGrid Production" required />
        </FormControl>
        <FormDescription>
          A friendly name to identify this configuration
        </FormDescription>
      </FormField>

      <Separator />

      <section className="space-y-3">
        <p className="text-eyebrow">Connection</p>
        <div className="grid grid-cols-3 gap-3">
          <FormField className="col-span-2">
            <FormLabel>SMTP host</FormLabel>
            <FormControl>
              <Input name="host" placeholder="smtp.sendgrid.net" required />
            </FormControl>
          </FormField>
          <FormField>
            <FormLabel>Port</FormLabel>
            <FormControl>
              <Input name="port" type="number" defaultValue={587} required />
            </FormControl>
          </FormField>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <FormField>
            <FormLabel>Username</FormLabel>
            <FormControl>
              <Input name="username" placeholder="apikey" autoComplete="off" required />
            </FormControl>
          </FormField>
          <FormField>
            <FormLabel>Password</FormLabel>
            <FormControl>
              <Input name="password" type="password" placeholder="••••••••" autoComplete="new-password" required />
            </FormControl>
          </FormField>
        </div>
      </section>

      <Separator />

      <section className="space-y-3">
        <p className="text-eyebrow">Sender identity</p>
        <div className="grid grid-cols-2 gap-3">
          <FormField>
            <FormLabel>From address</FormLabel>
            <FormControl>
              <Input
                name="fromAddress"
                type="email"
                placeholder="noreply@example.com"
                value={fromAddress}
                onChange={(e) => setFromAddress(e.target.value)}
                required
              />
            </FormControl>
          </FormField>
          <FormField>
            <FormLabel>From name</FormLabel>
            <FormControl>
              <Input name="fromName" placeholder="My App" />
            </FormControl>
            <FormDescription>Optional</FormDescription>
          </FormField>
        </div>
        {fromDomain &&
          (isVerified ? (
            <p className="flex items-center gap-1.5 text-caption text-success">
              <CheckCircle2Icon className="size-3.5" />
              <span className="text-mono text-xs">{fromDomain}</span> is a verified sending domain — mail will be DKIM-signed.
            </p>
          ) : (
            <p className="flex items-center gap-1.5 text-caption text-warning">
              <AlertCircleIcon className="size-3.5" />
              <span className="text-mono text-xs">{fromDomain}</span> isn&apos;t verified.{" "}
              <Link href="/dashboard/domains" className="underline underline-offset-2">
                Add it under Domains
              </Link>{" "}
              to DKIM-sign mail and improve deliverability.
            </p>
          ))}
      </section>

      <Separator />

      <section className="space-y-3">
        <div className="space-y-1">
          <p className="text-eyebrow">Incoming mail (IMAP) — optional</p>
          <p className="text-caption text-muted-foreground">
            Add IMAP details to also receive and read replies to this address
            under{" "}
            <span className="text-body-strong">Incoming emails</span>. Leave
            blank to send only. Gmail/Outlook require an app-specific password.
          </p>
        </div>
        <div className="grid grid-cols-3 gap-3">
          <FormField className="col-span-2">
            <FormLabel>IMAP host</FormLabel>
            <FormControl>
              <Input name="imapHost" placeholder="imap.example.com" />
            </FormControl>
          </FormField>
          <FormField>
            <FormLabel>Port</FormLabel>
            <FormControl>
              <Input name="imapPort" type="number" defaultValue={993} />
            </FormControl>
          </FormField>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <FormField>
            <FormLabel>IMAP username</FormLabel>
            <FormControl>
              <Input name="imapUsername" placeholder="Defaults to SMTP username" autoComplete="off" />
            </FormControl>
          </FormField>
          <FormField>
            <FormLabel>IMAP password</FormLabel>
            <FormControl>
              <Input name="imapPassword" type="password" placeholder="••••••••" autoComplete="new-password" />
            </FormControl>
          </FormField>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={testImap}
          loading={testingImap}
        >
          <PlugIcon className="mr-1.5 size-4" />
          {testingImap ? "Testing…" : "Test IMAP connection"}
        </Button>
      </section>

      <Separator />

      <Button type="submit" className="w-full" loading={loading}>
        {loading ? "Creating..." : "Create configuration"}
      </Button>
    </form>
  );
}
