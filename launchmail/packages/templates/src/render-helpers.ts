export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function humanizeKey(key: string): string {
  const spaced = key
    .replace(/[_-]+/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function isInternalField(key: string): boolean {
  return key.startsWith("_") || key.toLowerCase() === "g-recaptcha-response";
}

export function visibleFields(
  data: Record<string, string>,
): [string, string][] {
  return Object.entries(data).filter(([key]) => !isInternalField(key));
}

/**
 * Substitute `{{ key }}` placeholders with plain (un-escaped) data values.
 * Use ONLY for non-HTML contexts such as the email subject line, where HTML
 * entities would corrupt the rendered value. For any HTML output use
 * {@link interpolateHtml} so submitter-controlled values cannot inject markup.
 */
export function interpolate(
  template: string,
  data: Record<string, string>,
): string {
  return template.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_, key: string) => {
    const value = data[key];
    return value === undefined || value === null ? "" : String(value);
  });
}

/**
 * Substitute `{{ key }}` placeholders into an HTML template, HTML-escaping
 * every data value so submitter-controlled content cannot inject markup.
 *
 * Keys listed in `rawKeys` are treated as already-safe, pre-built HTML
 * fragments (e.g. `_fields`, `_brand`, `_heading`) and are injected verbatim
 * so they are not double-escaped.
 */
export function interpolateHtml(
  template: string,
  data: Record<string, string>,
  rawKeys?: Iterable<string>,
): string {
  const raw = rawKeys instanceof Set ? rawKeys : new Set(rawKeys ?? []);
  return template.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_, key: string) => {
    const value = data[key];
    if (value === undefined || value === null) return "";
    const str = String(value);
    return raw.has(key) ? str : escapeHtml(str);
  });
}

export function buildFieldsHtml(
  data: Record<string, string>,
  options: { dark?: boolean } = {},
): string {
  const border = options.dark ? "#27272a" : "#f0f0f0";
  const labelColor = options.dark ? "#a1a1aa" : "#737373";
  const valueColor = options.dark ? "#fafafa" : "#171717";
  const rows = visibleFields(data)
    .map(([key, value], i) => {
      const label = escapeHtml(humanizeKey(key));
      const val = escapeHtml(String(value)).replace(/\n/g, "<br />") || "—";
      const topBorder = i === 0 ? "" : `border-top:1px solid ${border};`;
      return `<tr>
        <td style="${topBorder}padding:12px 0;font-size:13px;color:${labelColor};width:34%;vertical-align:top;">${label}</td>
        <td style="${topBorder}padding:12px 0 12px 20px;font-size:14px;line-height:1.5;color:${valueColor};vertical-align:top;word-break:break-word;">${val}</td>
      </tr>`;
    })
    .join("");
  return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;table-layout:fixed;">${rows}</table>`;
}

export function buildFieldsText(data: Record<string, string>): string {
  return visibleFields(data)
    .map(([key, value]) => `${humanizeKey(key)}: ${value}`)
    .join("\n");
}
