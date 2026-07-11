"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@workspace/ui/components/button";
import { Card, CardContent } from "@workspace/ui/components/card";
import { Badge } from "@workspace/ui/components/badge";
import { StatusBadge } from "@/components/status-badge";
import { toast } from "@workspace/ui/components/sonner";
import {
  RefreshCwIcon,
  CopyIcon,
  CheckIcon,
  Trash2Icon,
  CheckCircle2Icon,
  CircleDashedIcon,
} from "lucide-react";
import { verifyDomainAction, deleteDomainAction } from "../actions";

interface DnsRecord {
  type: string;
  name: string;
  value: string;
  purpose: string;
  verified: boolean;
}
interface DomainView {
  id: string;
  domain: string;
  verified: boolean;
  lastCheckedAt: string | null;
  records: DnsRecord[];
}

function Copyable({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      toast.error("Couldn't copy to clipboard");
    }
  }
  return (
    <button
      onClick={copy}
      className="inline-flex shrink-0 items-center rounded p-1 text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
      title="Copy"
    >
      {copied ? (
        <CheckIcon className="size-3.5 text-success" />
      ) : (
        <CopyIcon className="size-3.5" />
      )}
    </button>
  );
}

export function DomainDetail({ domain }: { domain: DomainView }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function verify() {
    setBusy(true);
    const res = await verifyDomainAction(domain.id);
    setBusy(false);
    if (res?.error) return toast.error(res.error);
    toast.success("Re-checked DNS records");
    router.refresh();
  }

  async function remove() {
    if (!confirm(`Delete ${domain.domain}?`)) return;
    setBusy(true);
    const res = await deleteDomainAction(domain.id);
    if (res?.error) {
      toast.error(res.error);
      setBusy(false);
    }
  }

  return (
    <div className="space-y-10">
      <section className="space-y-4">
        <p className="text-eyebrow">Status</p>
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <StatusBadge status={domain.verified ? "verified" : "pending"} />
            {domain.lastCheckedAt && (
              <span className="text-caption text-muted-foreground">
                Last checked {new Date(domain.lastCheckedAt).toLocaleString()}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={verify} loading={busy}>
              {!busy && <RefreshCwIcon className="mr-1 size-4" />}
              {busy ? "Checking…" : "Verify"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive"
              onClick={remove}
              disabled={busy}
            >
              <Trash2Icon className="size-4" />
            </Button>
          </div>
        </div>
      </section>

      <section className="space-y-4">
        <div className="space-y-1">
          <p className="text-eyebrow">DNS records</p>
          <h2 className="text-section text-foreground">Authentication</h2>
          <p className="text-caption text-muted-foreground">
            Add these TXT records at your DNS provider, then click Verify.
          </p>
        </div>

        <div className="space-y-3">
          {domain.records.map((r) => (
            <Card key={r.purpose}>
              <CardContent className="space-y-3 p-5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {r.verified ? (
                      <CheckCircle2Icon className="size-4 text-success" />
                    ) : (
                      <CircleDashedIcon className="size-4 text-muted-foreground" />
                    )}
                    <span className="text-body-strong">{r.purpose}</span>
                    <Badge variant="outline" className="text-mono text-[10px]">
                      {r.type}
                    </Badge>
                  </div>
                  <StatusBadge status={r.verified ? "verified" : "pending"} />
                </div>
                <Field label="Name" value={r.name} />
                <Field label="Value" value={r.value} />
              </CardContent>
            </Card>
          ))}
        </div>
      </section>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="mb-1 text-eyebrow">{label}</p>
      <div className="flex items-start gap-2 rounded-md bg-muted px-3 py-2">
        <code className="min-w-0 flex-1 break-all text-mono text-xs">
          {value}
        </code>
        <Copyable text={value} />
      </div>
    </div>
  );
}
