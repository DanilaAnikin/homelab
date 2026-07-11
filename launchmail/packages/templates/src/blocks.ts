import { escapeHtml } from "./render-helpers";

export type EmailBlock =
  | { id: string; type: "heading"; text: string }
  | { id: string; type: "text"; text: string }
  | { id: string; type: "button"; text: string; url: string }
  | { id: string; type: "image"; url: string; alt?: string }
  | { id: string; type: "divider" }
  | { id: string; type: "spacer"; size?: number };

interface RenderOpts {
  brandName?: string;
  accent?: string;
  dark?: boolean;
  data?: Record<string, string>;
}

// HTML-escape interpolated data values so submitter-controlled content can
// never inject markup into the rendered email. The surrounding template
// (block text/url, custom HTML) is author-controlled and left intact.
function interpolate(s: string, data?: Record<string, string>): string {
  if (!data) return s;
  return s.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_, k) => {
    const value = data[k];
    return value === undefined || value === null ? "" : escapeHtml(String(value));
  });
}

function blockHtml(block: EmailBlock, o: RenderOpts, p: Palette): string {
  const accent = o.accent || "#5b5bd6";
  switch (block.type) {
    case "heading":
      return `<h1 style="margin:0 0 10px;font-size:24px;line-height:1.25;font-weight:700;letter-spacing:-0.02em;color:${p.heading};">${interpolate(
        block.text,
        o.data,
      )}</h1>`;
    case "text":
      return `<p style="margin:0 0 16px;font-size:15px;line-height:1.65;color:${p.muted};">${interpolate(
        block.text,
        o.data,
      ).replace(/\n/g, "<br />")}</p>`;
    case "button":
      return `<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 20px;"><tr><td style="border-radius:8px;background:${accent};"><a href="${interpolate(
        block.url,
        o.data,
      )}" style="display:inline-block;padding:12px 24px;font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px;">${interpolate(
        block.text,
        o.data,
      )}</a></td></tr></table>`;
    case "image":
      return `<img src="${interpolate(block.url, o.data)}" alt="${escapeHtml(
        block.alt ?? "",
      )}" style="display:block;max-width:100%;height:auto;border-radius:10px;margin:0 0 16px;" />`;
    case "divider":
      return `<hr style="border:none;border-top:1px solid ${p.border};margin:24px 0;" />`;
    case "spacer":
      return `<div style="height:${Math.max(4, Math.min(96, block.size ?? 24))}px;line-height:1px;font-size:1px;">&nbsp;</div>`;
    default:
      return "";
  }
}

interface Palette {
  body: string;
  card: string;
  border: string;
  heading: string;
  muted: string;
  faint: string;
}

const LIGHT: Palette = {
  body: "#f4f5f7",
  card: "#ffffff",
  border: "#e8eaef",
  heading: "#0f172a",
  muted: "#475569",
  faint: "#94a3b8",
};
const DARK: Palette = {
  body: "#09090b",
  card: "#131316",
  border: "#26262b",
  heading: "#f8fafc",
  muted: "#c7c7cc",
  faint: "#71717a",
};

export function renderBlocks(blocks: EmailBlock[], o: RenderOpts = {}): string {
  const p = o.dark ? DARK : LIGHT;
  const accent = o.accent || "#5b5bd6";
  const brand = escapeHtml(o.brandName || "LaunchMail");
  const body = (blocks || []).map((b) => blockHtml(b, o, p)).join("\n");
  return `<!doctype html>
<html>
  <head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" /></head>
  <body style="margin:0;padding:0;background:${p.body};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:${p.body};padding:40px 16px;">
      <tr><td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;">
          <tr><td style="background:${p.card};border:1px solid ${p.border};border-radius:16px;overflow:hidden;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr><td style="height:4px;background:${accent};font-size:0;line-height:0;">&nbsp;</td></tr>
              <tr><td style="padding:32px 40px;">${body || `<p style="color:${p.faint};">Empty template</p>`}</td></tr>
            </table>
          </td></tr>
          <tr><td style="padding:18px 4px 0;text-align:center;font-size:11px;color:${p.faint};">Powered by ${brand}</td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>`;
}

export function renderCustomHtml(
  html: string,
  data?: Record<string, string>,
): string {
  return interpolate(html, data);
}
