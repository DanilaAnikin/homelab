import { escapeHtml } from "./render-helpers";
import type { LayoutContext } from "./types";

interface Palette {
  body: string;
  card: string;
  border: string;
  panel: string;
  panelBorder: string;
  heading: string;
  muted: string;
  faint: string;
  badgeText: string;
}

const LIGHT: Palette = {
  body: "#f4f5f7",
  card: "#ffffff",
  border: "#e8eaef",
  panel: "#f8fafc",
  panelBorder: "#eef1f6",
  heading: "#0f172a",
  muted: "#64748b",
  faint: "#94a3b8",
  badgeText: "#ffffff",
};

const DARK: Palette = {
  body: "#09090b",
  card: "#131316",
  border: "#26262b",
  panel: "#1a1a1f",
  panelBorder: "#26262b",
  heading: "#f8fafc",
  muted: "#a1a1aa",
  faint: "#71717a",
  badgeText: "#ffffff",
};

function render(ctx: LayoutContext, p: Palette): string {
  const brand = escapeHtml(ctx.brandName);
  const heading = escapeHtml(ctx.heading);
  const intro = escapeHtml(ctx.intro);
  const accent = ctx.accent;
  const initial = escapeHtml((ctx.brandName.trim()[0] || "L").toUpperCase());
  return `<!doctype html>
<html>
  <head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" /></head>
  <body style="margin:0;padding:0;background:${p.body};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:${p.body};padding:40px 16px;">
      <tr><td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;border-collapse:separate;">
          <tr><td style="background:${p.card};border:1px solid ${p.border};border-radius:16px;overflow:hidden;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr><td style="height:4px;background:${accent};font-size:0;line-height:0;">&nbsp;</td></tr>
              <tr><td style="padding:28px 40px 0;">
                <table role="presentation" cellpadding="0" cellspacing="0"><tr>
                  <td style="vertical-align:middle;">
                    <span style="display:inline-block;width:34px;height:34px;border-radius:9px;background:${accent};color:${p.badgeText};font-size:16px;font-weight:700;text-align:center;line-height:34px;">${initial}</span>
                  </td>
                  <td style="vertical-align:middle;padding-left:12px;font-size:15px;font-weight:600;color:${p.heading};letter-spacing:-0.01em;">${brand}</td>
                </tr></table>
              </td></tr>
              <tr><td style="padding:22px 40px 0;">
                <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:${accent};">New submission</div>
                <div style="margin-top:10px;font-size:23px;font-weight:700;line-height:1.25;color:${p.heading};letter-spacing:-0.02em;">${heading}</div>
                <div style="margin-top:8px;font-size:15px;line-height:1.6;color:${p.muted};">${intro}</div>
              </td></tr>
              <tr><td style="padding:24px 40px 0;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:${p.panel};border:1px solid ${p.panelBorder};border-radius:12px;">
                  <tr><td style="padding:6px 20px;">${ctx.fieldsHtml}</td></tr>
                </table>
              </td></tr>
              <tr><td style="padding:28px 40px 30px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid ${p.panelBorder};">
                  <tr>
                    <td style="padding-top:16px;font-size:12px;color:${p.faint};vertical-align:middle;">${escapeHtml(ctx.footer)}</td>
                    <td style="padding-top:16px;font-size:12px;color:${p.faint};text-align:right;vertical-align:middle;">${escapeHtml(ctx.submittedAt)}</td>
                  </tr>
                </table>
              </td></tr>
            </table>
          </td></tr>
          <tr><td style="padding:18px 4px 0;text-align:center;font-size:11px;color:${p.faint};">Powered by ${brand}</td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>`;
}

export function renderLayout(dark: boolean, ctx: LayoutContext): string {
  return render(ctx, dark ? DARK : LIGHT);
}
