"use client";

import { useState } from "react";
import { StatusBadge } from "@/components/status-badge";
import { blockRemoteImages } from "@/lib/email-html";
import { Avatar } from "./mail-client";
import { ImageIcon } from "lucide-react";
import type { SentEmailFull } from "./actions";

export function SentReader({ detail }: { detail: SentEmailFull }) {
  const [showImages, setShowImages] = useState(false);
  const to = detail.to.map((r) => r.name || r.email).join(", ");
  const blocked = detail.html
    ? blockRemoteImages(detail.html)
    : { html: "", blocked: false };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-card-title leading-snug">
            {detail.subject || "(no subject)"}
          </h2>
          <StatusBadge status={detail.status} />
        </div>
        <div className="mt-3 flex items-start gap-3">
          <Avatar name={to || "?"} className="size-10" />
          <div className="min-w-0 flex-1">
            <p className="truncate text-caption text-muted-foreground">
              <span className="text-body-strong text-foreground">From</span>{" "}
              {detail.from}
            </p>
            <p className="truncate text-caption text-muted-foreground">
              <span className="text-body-strong text-foreground">To</span> {to || "—"}
            </p>
            <p className="mt-0.5 text-caption text-muted-foreground">
              {new Date(detail.createdAt).toLocaleString()}
              {detail.opens > 0 && ` · ${detail.opens} open${detail.opens > 1 ? "s" : ""}`}
              {detail.clicks > 0 && ` · ${detail.clicks} click${detail.clicks > 1 ? "s" : ""}`}
            </p>
          </div>
        </div>
        {detail.error && (
          <pre className="mt-3 overflow-x-auto rounded-md border border-destructive/30 bg-destructive-tint/40 p-3 font-mono text-mono whitespace-pre-wrap text-destructive-on">
            {detail.error}
          </pre>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {blocked.blocked && !showImages && (
          <div className="flex items-center justify-between gap-3 border-b border-border bg-warning-tint/40 px-5 py-2 text-caption">
            <span className="flex items-center gap-1.5 text-warning-on">
              <ImageIcon className="size-3.5" />
              Remote images blocked.
            </span>
            <button
              type="button"
              onClick={() => setShowImages(true)}
              className="font-medium text-brand hover:underline"
            >
              Show images
            </button>
          </div>
        )}
        {detail.html ? (
          <iframe
            title="Sent message"
            sandbox=""
            srcDoc={showImages ? detail.html : blocked.html}
            className="h-[56vh] w-full border-0 bg-white"
          />
        ) : (
          <pre className="px-5 py-4 font-sans text-body whitespace-pre-wrap break-words">
            {detail.text || "(no body stored)"}
          </pre>
        )}
      </div>
    </div>
  );
}
