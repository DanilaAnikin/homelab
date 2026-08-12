const ALLOWED_HOSTS = new Set(["freio.cz", "www.freio.cz"]);
const HEALTH_PATH = "/__freio-edge-health";
const ORIGIN_TIMEOUT_MS = 4_000;
const FALLBACK_VERSION = "static-v1";

const FALLBACK_HEADERS = Object.freeze({
  "Cache-Control": "no-store, max-age=0",
  "Content-Security-Policy":
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy":
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
  "Retry-After": "20",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-Freio-Edge-Fallback": FALLBACK_VERSION,
  "X-Robots-Tag": "noindex, nofollow, noarchive",
});

const FALLBACK_HTML = `<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow,noarchive">
  <meta http-equiv="refresh" content="20">
  <title>Freio · záložní režim</title>
  <style>
    :root { color-scheme: light; --ink:#183b39; --muted:#58716f; --paper:#f4f1e9; --teal:#1c6d67; --line:#c8d3cf; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px; background:var(--paper); color:var(--ink); font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { width:min(720px,100%); border-top:4px solid var(--teal); padding:clamp(28px,6vw,64px) 0; }
    .brand { margin:0 0 56px; font-size:22px; font-weight:800; letter-spacing:-.04em; }
    .eyebrow { margin:0 0 12px; color:var(--teal); font-size:13px; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
    h1 { max-width:650px; margin:0; font-size:clamp(38px,8vw,72px); line-height:.98; letter-spacing:-.055em; }
    p { max-width:590px; margin:28px 0 0; color:var(--muted); font-size:clamp(17px,2.5vw,21px); line-height:1.55; }
    .status { display:flex; align-items:center; gap:10px; margin-top:42px; padding-top:22px; border-top:1px solid var(--line); color:var(--ink); font-size:14px; font-weight:700; }
    .dot { width:10px; height:10px; border-radius:50%; background:var(--teal); box-shadow:0 0 0 6px rgb(28 109 103 / 12%); }
  </style>
</head>
<body>
  <main>
    <div class="brand">Freio</div>
    <div class="eyebrow">Záložní režim je aktivní</div>
    <h1>Hlavní aplikaci právě vracíme do provozu.</h1>
    <p>Tvoje data jsou v bezpečí. Tato stránka se sama zkusí znovu připojit za 20 sekund. Pokud něco potřebuješ hned, napiš na contact@freio.cz.</p>
    <div class="status"><span class="dot" aria-hidden="true"></span> Záložní web funguje · zápisové operace jsou dočasně pozastavené</div>
  </main>
</body>
</html>`;

function responseBody(method, body) {
  return method === "HEAD" ? null : body;
}

function failClosed(method) {
  return new Response(
    responseBody(
      method,
      '{"error":"service_temporarily_unavailable","edge_fallback":true}\n',
    ),
    {
      status: 503,
      headers: {
        ...FALLBACK_HEADERS,
        "Content-Type": "application/json; charset=utf-8",
      },
    },
  );
}

function fallbackPage(method) {
  return new Response(responseBody(method, FALLBACK_HTML), {
    status: 200,
    headers: {
      ...FALLBACK_HEADERS,
      "Content-Type": "text/html; charset=utf-8",
    },
  });
}

function healthResponse(method) {
  return new Response(
    responseBody(
      method,
      '{"status":"ok","component":"freio-edge-fallback","mode":"standby"}\n',
    ),
    {
      status: 200,
      headers: {
        "Cache-Control": "no-store, max-age=0",
        "Content-Type": "application/json; charset=utf-8",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Freio-Edge-Fallback": "health-v1",
      },
    },
  );
}

function httpsRedirect(url) {
  const destination = new URL(url);
  destination.protocol = "https:";
  return new Response(null, {
    status: 308,
    headers: {
      "Cache-Control": "no-store, max-age=0",
      Location: destination.toString(),
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function isUnambiguousPath(pathname) {
  // Percent-encoded and repeated-slash paths deliberately fail closed. This
  // avoids accidentally presenting a friendly HTML success page for an
  // encoded /api or /_next request while the origin is unavailable.
  return (
    pathname.startsWith("/") &&
    !pathname.includes("%") &&
    !pathname.includes("\\") &&
    !pathname.includes("//") &&
    !pathname.includes("\u0000")
  );
}

function isApplicationPath(pathname) {
  return (
    pathname === "/api" ||
    pathname.startsWith("/api/") ||
    pathname === "/_next" ||
    pathname.startsWith("/_next/")
  );
}

function isHtmlNavigation(request, url, method) {
  if (method !== "GET" && method !== "HEAD") return false;
  if (!isUnambiguousPath(url.pathname) || isApplicationPath(url.pathname)) {
    return false;
  }

  const accept = request.headers?.get?.("accept") ?? "";
  if (!accept.toLowerCase().includes("text/html")) return false;

  const mode = request.headers?.get?.("sec-fetch-mode");
  if (mode !== null && mode !== "navigate") return false;

  const destination = request.headers?.get?.("sec-fetch-dest");
  if (destination !== null && destination !== "document") return false;

  return true;
}

function isFallbackStatus(status) {
  return status >= 500 && status <= 504;
}

async function discardBody(response) {
  try {
    await response.body?.cancel();
  } catch {
    // A locked or already consumed body needs no further action.
  }
}

/**
 * Dependency injection is intentionally limited to the origin fetch and
 * timeout so the same contract can be tested without touching Cloudflare.
 */
export async function handleRequest(
  request,
  fetchOrigin = globalThis.fetch,
  originTimeoutMs = ORIGIN_TIMEOUT_MS,
) {
  const method = String(request?.method ?? "").toUpperCase();
  let url;

  try {
    url = new URL(request?.url);
  } catch {
    return failClosed(method);
  }

  if (!ALLOWED_HOSTS.has(url.hostname)) return failClosed(method);
  if (url.protocol === "http:") return httpsRedirect(url);
  if (url.protocol !== "https:") return failClosed(method);

  if (
    url.pathname === HEALTH_PATH &&
    (method === "GET" || method === "HEAD")
  ) {
    return healthResponse(method);
  }

  const navigation = isHtmlNavigation(request, url, method);
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(new Error("origin_timeout")),
    originTimeoutMs,
  );

  try {
    // Exactly one origin attempt. In particular, write requests are never
    // replayed after an exception, timeout, or 5xx response.
    const originResponse = await fetchOrigin(request, {
      redirect: "manual",
      signal: controller.signal,
    });

    if (!isFallbackStatus(originResponse.status)) return originResponse;

    await discardBody(originResponse);
    return navigation ? fallbackPage(method) : failClosed(method);
  } catch {
    return navigation ? fallbackPage(method) : failClosed(method);
  } finally {
    clearTimeout(timeout);
  }
}

export default {
  async fetch(request) {
    return handleRequest(request);
  },
};
