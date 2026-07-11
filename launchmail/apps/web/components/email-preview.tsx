"use client";

import { useEffect, useRef, useState } from "react";

const BASE_WIDTH = 640;
const BASE_HEIGHT = 820;

export function EmailPreview({
  html,
  subject,
}: {
  html: string;
  subject?: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0.6);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setScale(Math.min(1, el.clientWidth / BASE_WIDTH));
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
      <div className="flex items-center gap-2 border-b bg-muted/40 px-4 py-2.5">
        <div className="flex gap-1.5">
          <span className="size-2.5 rounded-full bg-muted-foreground/25" />
          <span className="size-2.5 rounded-full bg-muted-foreground/25" />
          <span className="size-2.5 rounded-full bg-muted-foreground/25" />
        </div>
        <div className="mx-auto max-w-[70%] truncate rounded-md bg-background px-3 py-1 text-xs text-muted-foreground">
          {subject || "Email preview"}
        </div>
      </div>
      <div
        ref={wrapRef}
        className="overflow-hidden"
        style={{ height: BASE_HEIGHT * scale }}
      >
        <iframe
          title="Email preview"
          srcDoc={html}
          style={{
            width: BASE_WIDTH,
            height: BASE_HEIGHT,
            border: 0,
            transformOrigin: "top left",
            transform: `scale(${scale})`,
          }}
        />
      </div>
    </div>
  );
}
