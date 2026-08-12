import http from "node:http";

const host = "0.0.0.0";
const port = parsePort(process.env.PORT ?? "8080", "PORT");
const primaryHost = process.env.PRIMARY_HOST ?? "freio-xkgrrq";
const primaryPort = parsePort(process.env.PRIMARY_PORT ?? "3000", "PRIMARY_PORT");
const connectTimeoutMs = 900;
const readOnlyHeaderTimeoutMs = 4_000;
const writeHeaderTimeoutMs = 30_000;
const publicHosts = new Set(["freio.cz", "www.freio.cz"]);
const hopByHopHeaders = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

if (!/^[a-z0-9.-]+$/u.test(primaryHost)) {
  throw new Error("invalid PRIMARY_HOST");
}

function parsePort(value, name) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`invalid ${name}`);
  }
  return parsed;
}

const fallbackHeaders = Object.freeze({
  "Cache-Control": "no-store, max-age=0",
  "Content-Security-Policy":
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy":
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()",
  "Referrer-Policy": "no-referrer",
  "Retry-After": "20",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-Freio-Fallback": "static-v1",
  "X-Robots-Tag": "noindex, nofollow, noarchive",
});

const html = `<!doctype html>
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
    a { color:var(--teal); text-underline-offset:3px; }
  </style>
</head>
<body>
  <main>
    <div class="brand">Freio</div>
    <div class="eyebrow">Záložní režim je aktivní</div>
    <h1>Hlavní aplikaci právě vracíme do provozu.</h1>
    <p>Tvoje data jsou v bezpečí. Tato stránka se sama zkusí znovu připojit za 20 sekund. Pokud něco potřebuješ hned, napiš na <a href="mailto:contact@freio.cz">contact@freio.cz</a>.</p>
    <div class="status"><span class="dot" aria-hidden="true"></span> Záložní web funguje · zápisové operace jsou dočasně pozastavené</div>
  </main>
</body>
</html>`;

function send(res, status, headers, body, method) {
  if (res.headersSent || res.destroyed) return;
  res.writeHead(status, headers);
  res.end(method === "HEAD" ? undefined : body);
}

function withoutHopByHopHeaders(headers) {
  const connectionTokens = String(headers.connection ?? "")
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
  const excluded = new Set([
    ...hopByHopHeaders,
    ...connectionTokens,
    "x-freio-fallback",
  ]);
  return Object.fromEntries(
    Object.entries(headers).filter(
      ([name]) => !excluded.has(name.toLowerCase()),
    ),
  );
}

function isPrivatePath(rawTarget) {
  const rawPath = rawTarget.split(/[?#]/u, 1)[0];
  if (!rawPath.startsWith("/") || rawPath.startsWith("//") || rawPath.includes("\\")) {
    return true;
  }

  let pathname;
  try {
    const parsed = new URL(rawTarget, "http://gateway.invalid");
    pathname = decodeURIComponent(parsed.pathname);
  } catch {
    return true;
  }

  if (pathname.startsWith("//") || pathname.includes("\\")) {
    return true;
  }

  return (
    pathname === "/api" ||
    pathname.startsWith("/api/") ||
    pathname === "/_next" ||
    pathname.startsWith("/_next/")
  );
}

function originalScheme(headers) {
  const cfVisitor = headers["cf-visitor"];
  if (typeof cfVisitor === "string") {
    try {
      const parsed = JSON.parse(cfVisitor);
      if (parsed?.scheme === "http" || parsed?.scheme === "https") {
        return parsed.scheme;
      }
    } catch {
      return null;
    }
  }

  const forwardedProto = headers["x-forwarded-proto"];
  if (typeof forwardedProto !== "string") return null;
  const scheme = forwardedProto.split(",", 1)[0].trim().toLowerCase();
  return scheme === "http" || scheme === "https" ? scheme : null;
}

function primaryRequestHeaders(headers) {
  const forwarded = withoutHopByHopHeaders(headers);
  const scheme = originalScheme(headers);

  // cloudflared terminates public TLS before its internal HTTP hop to
  // Traefik, so Traefik correctly describes that last hop as http. CF-Visitor
  // is the authoritative public scheme on this Cloudflare-only ingress and
  // already controls the redirect boundary above. Rebuild X-Forwarded-Proto
  // from the same parsed value before the secretless gateway reaches Next.js;
  // never forward a conflicting client value. If CF-Visitor is malformed,
  // originalScheme deliberately returns null and this function grants no
  // public-HTTPS authority.
  if (scheme === null) {
    delete forwarded["x-forwarded-proto"];
  } else {
    forwarded["x-forwarded-proto"] = scheme;
  }
  return forwarded;
}

function publicHost(headers) {
  const value = headers.host;
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (publicHosts.has(normalized)) return normalized;
  if (normalized.endsWith(":80")) {
    const withoutPort = normalized.slice(0, -3);
    if (publicHosts.has(withoutPort)) return withoutPort;
  }
  return null;
}

function sendHttpsRedirect(req, res, hostName) {
  const method = req.method ?? "GET";
  const rawTarget = req.url ?? "/";
  if (
    !rawTarget.startsWith("/") ||
    rawTarget.startsWith("//") ||
    rawTarget.includes("\\") ||
    /[\r\n]/u.test(rawTarget)
  ) {
    send(
      res,
      400,
      {
        "Cache-Control": "no-store",
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
      '{"error":"invalid_request_target"}\n',
      method,
    );
    return;
  }

  send(
    res,
    308,
    {
      "Cache-Control": "no-store",
      Location: `https://${hostName}${rawTarget}`,
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
    },
    "",
    method,
  );
}

function sendFallback(req, res) {
  const method = req.method ?? "GET";
  const rawTarget = req.url ?? "/";
  const isReadOnlyPage =
    (method === "GET" || method === "HEAD") && !isPrivatePath(rawTarget);

  if (isReadOnlyPage) {
    send(
      res,
      200,
      { ...fallbackHeaders, "Content-Type": "text/html; charset=utf-8" },
      html,
      method,
    );
    return;
  }

  send(
    res,
    503,
    { ...fallbackHeaders, "Content-Type": "application/json; charset=utf-8" },
    '{"error":"service_temporarily_unavailable","fallback":true}\n',
    method,
  );
}

function proxyToPrimary(req, res) {
  let responseStarted = false;
  let settled = false;
  let upstreamResponse;

  const method = req.method ?? "GET";
  const rawTarget = req.url ?? "/";
  const responseHeaderTimeoutMs =
    (method === "GET" || method === "HEAD") && !isPrivatePath(rawTarget)
      ? readOnlyHeaderTimeoutMs
      : writeHeaderTimeoutMs;

  const upstream = http.request({
    host: primaryHost,
    port: primaryPort,
    method,
    path: req.url,
    headers: primaryRequestHeaders(req.headers),
    agent: false,
  });

  const connectionTimer = setTimeout(() => {
    if (!settled) upstream.destroy(new Error("primary_connect_timeout"));
  }, connectTimeoutMs);

  const headerTimer = setTimeout(() => {
    if (!settled) upstream.destroy(new Error("primary_header_timeout"));
  }, responseHeaderTimeoutMs);

  upstream.once("socket", (socket) => {
    if (socket.connecting) {
      socket.once("connect", () => clearTimeout(connectionTimer));
    } else {
      clearTimeout(connectionTimer);
    }
  });

  upstream.once("response", (response) => {
    upstreamResponse = response;
    settled = true;
    clearTimeout(connectionTimer);
    clearTimeout(headerTimer);

    const status = response.statusCode ?? 502;
    if (status >= 500 && status <= 504) {
      response.resume();
      sendFallback(req, res);
      return;
    }

    responseStarted = true;
    res.writeHead(status, withoutHopByHopHeaders(response.headers));
    response.on("error", () => res.destroy());
    response.pipe(res);
  });

  upstream.once("error", () => {
    settled = true;
    clearTimeout(connectionTimer);
    clearTimeout(headerTimer);
    if (!responseStarted) sendFallback(req, res);
  });

  req.once("aborted", () => upstream.destroy());
  req.once("error", () => upstream.destroy());
  res.once("close", () => {
    if (!res.writableEnded) {
      upstreamResponse?.destroy();
      upstream.destroy();
    }
  });
  req.pipe(upstream);
}

function handleRequest(req, res) {
  const method = req.method ?? "GET";
  let url;
  try {
    url = new URL(req.url ?? "/", "http://gateway.invalid");
  } catch {
    send(
      res,
      400,
      {
        "Cache-Control": "no-store",
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
      '{"error":"invalid_request_target"}\n',
      method,
    );
    return;
  }

  if (originalScheme(req.headers) === "http") {
    const hostName = publicHost(req.headers);
    if (!hostName) {
      send(
        res,
        400,
        {
          "Cache-Control": "no-store",
          "Content-Type": "application/json; charset=utf-8",
          "X-Content-Type-Options": "nosniff",
        },
        '{"error":"invalid_public_host"}\n',
        method,
      );
      return;
    }
    sendHttpsRedirect(req, res, hostName);
    return;
  }

  if ((method === "GET" || method === "HEAD") && url.pathname === "/healthz") {
    send(
      res,
      200,
      {
        "Cache-Control": "no-store",
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
      '{"status":"ok","mode":"request_aware_gateway"}\n',
      method,
    );
    return;
  }

  proxyToPrimary(req, res);
}

const server = http.createServer(handleRequest);
server.on("checkContinue", (req, res) => {
  send(
    res,
    417,
    { ...fallbackHeaders, "Content-Type": "application/json; charset=utf-8" },
    '{"error":"expectation_failed"}\n',
    req.method ?? "POST",
  );
});

server.requestTimeout = 35_000;
server.headersTimeout = 5_000;
server.keepAliveTimeout = 5_000;
server.maxRequestsPerSocket = 100;

server.listen(port, host, () => {
  process.stdout.write(
    JSON.stringify({ event: "fallback_gateway_listening", host, port }) + "\n",
  );
});

function stop(signal) {
  server.close((error) => {
    if (error) {
      process.stderr.write(
        JSON.stringify({ event: "fallback_shutdown_error", signal }) + "\n",
      );
      process.exitCode = 1;
      return;
    }
    process.exitCode = 0;
  });
}

process.once("SIGTERM", () => stop("SIGTERM"));
process.once("SIGINT", () => stop("SIGINT"));
