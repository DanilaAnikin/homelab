import { db } from "@workspace/db";
import { emailLogs } from "@workspace/db/schemas";
import { and, eq, gte, sql } from "drizzle-orm";
import { sign, signId } from "./crypto";

const TRACK_BASE = (
  process.env.PUBLIC_API_URL ??
  process.env.API_URL ??
  "http://localhost:5000"
).replace(/\/$/, "");

export function trackingEnabled(): boolean {
  return process.env.TRACKING_DISABLED !== "true";
}

// `id` is the raw email-log id; it is HMAC-signed here so the /t endpoints can
// reject forged ids. The click destination is also HMAC-signed (`&s=`) so it
// cannot be tampered into an open redirect.
export function injectTracking(html: string, id: string): string {
  const token = signId(id);
  let out = html.replace(
    /href="(https?:\/\/[^"]+)"/gi,
    (_m, url) =>
      `href="${TRACK_BASE}/t/c/${token}?u=${encodeURIComponent(
        url,
      )}&s=${sign(url)}"`,
  );
  const pixel = `<img src="${TRACK_BASE}/t/o/${token}.gif" width="1" height="1" alt="" style="display:none;max-height:0;overflow:hidden" />`;
  if (/<\/body>/i.test(out)) out = out.replace(/<\/body>/i, `${pixel}</body>`);
  else out += pixel;
  return out;
}

export async function recordOpen(id: string): Promise<void> {
  await db
    .update(emailLogs)
    .set({
      opens: sql`${emailLogs.opens} + 1`,
      openedAt: sql`COALESCE(${emailLogs.openedAt}, now())`,
    })
    .where(eq(emailLogs.id, id))
    .catch(() => undefined);
}

export async function recordClick(id: string): Promise<void> {
  await db
    .update(emailLogs)
    .set({
      clicks: sql`${emailLogs.clicks} + 1`,
      clickedAt: sql`COALESCE(${emailLogs.clickedAt}, now())`,
      opens: sql`GREATEST(${emailLogs.opens}, 1)`,
      openedAt: sql`COALESCE(${emailLogs.openedAt}, now())`,
    })
    .where(eq(emailLogs.id, id))
    .catch(() => undefined);
}

export interface AnalyticsDay {
  date: string;
  sent: number;
  failed: number;
  opens: number;
  clicks: number;
}

export interface Analytics {
  daily: AnalyticsDay[];
  totals: {
    sent: number;
    failed: number;
    opened: number;
    clicked: number;
    deliveryRate: number;
    openRate: number;
    clickRate: number;
  };
}

export async function getAnalytics(
  organizationId: string,
  days = 30,
): Promise<Analytics> {
  const since = new Date(Date.now() - days * 86400000);
  const rows = await db
    .select({
      status: emailLogs.status,
      opens: emailLogs.opens,
      clicks: emailLogs.clicks,
      createdAt: emailLogs.createdAt,
    })
    .from(emailLogs)
    .where(
      and(
        eq(emailLogs.organizationId, organizationId),
        gte(emailLogs.createdAt, since),
      ),
    );

  const byDay = new Map<string, AnalyticsDay>();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10);
    byDay.set(d, { date: d, sent: 0, failed: 0, opens: 0, clicks: 0 });
  }

  let sent = 0;
  let failed = 0;
  let opened = 0;
  let clicked = 0;
  for (const r of rows) {
    const key = new Date(r.createdAt).toISOString().slice(0, 10);
    const day = byDay.get(key);
    if (r.status === "sent") {
      sent++;
      if (day) day.sent++;
      if (r.opens > 0) opened++;
      if (r.clicks > 0) clicked++;
      if (day) {
        day.opens += r.opens;
        day.clicks += r.clicks;
      }
    } else if (r.status === "failed") {
      failed++;
      if (day) day.failed++;
    }
  }

  const total = sent + failed;
  return {
    daily: [...byDay.values()],
    totals: {
      sent,
      failed,
      opened,
      clicked,
      deliveryRate: total ? Math.round((sent / total) * 100) : 0,
      openRate: sent ? Math.round((opened / sent) * 100) : 0,
      clickRate: sent ? Math.round((clicked / sent) * 100) : 0,
    },
  };
}
