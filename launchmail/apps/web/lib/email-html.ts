// Neutralize remote content (tracking pixels, external images) in an HTML email
// body before it's rendered in the reading pane. Heuristic but covers the common
// vectors: <img src>, srcset, CSS url(), and background attributes pointing at
// http(s)/protocol-relative URLs. The body is always rendered in a sandboxed,
// script-less iframe regardless — this is purely a privacy ("don't phone home")
// control with a per-message "Show images" override.

const REMOTE = "(?:https?:|//)";

export function blockRemoteImages(html: string): {
  html: string;
  blocked: boolean;
} {
  let blocked = false;
  const mark = () => {
    blocked = true;
    return "";
  };

  let out = html
    // <img ... src="http..."> / src='...'
    .replace(
      new RegExp(`\\ssrc\\s*=\\s*(["'])${REMOTE}[^"']*\\1`, "gi"),
      () => mark() || " data-blocked-img",
    )
    // <img ... srcset="...">
    .replace(
      new RegExp(`\\ssrcset\\s*=\\s*(["'])[^"']*\\1`, "gi"),
      () => mark() || "",
    )
    // style="background[-image]: url(http...)" and inline url(...)
    .replace(
      new RegExp(`url\\(\\s*["']?${REMOTE}[^)]*\\)`, "gi"),
      () => mark() || "none",
    )
    // legacy background="http..." attribute
    .replace(
      new RegExp(`\\sbackground\\s*=\\s*(["'])${REMOTE}[^"']*\\1`, "gi"),
      () => mark() || "",
    );

  if (blocked) {
    // Replace any now-src-less <img data-blocked-img> with a subtle placeholder
    // so layout doesn't collapse to a broken-image glyph.
    out = out.replace(
      /<img\b([^>]*?)data-blocked-img([^>]*)>/gi,
      '<img$1$2 alt="" style="display:inline-block;min-width:8px;min-height:8px;background:#e5e7eb;border-radius:2px" />',
    );
  }

  return { html: out, blocked };
}
