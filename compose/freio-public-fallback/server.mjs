import http from "node:http";

const host = "0.0.0.0";
const port = Number.parseInt(process.env.PORT ?? "8080", 10);

if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
  throw new Error("invalid PORT");
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
  res.writeHead(status, headers);
  res.end(method === "HEAD" ? undefined : body);
}

const server = http.createServer((req, res) => {
  const method = req.method ?? "GET";
  const url = new URL(req.url ?? "/", "http://fallback.invalid");

  if (
    (method === "GET" || method === "HEAD") &&
    url.pathname === "/healthz"
  ) {
    send(
      res,
      200,
      {
        "Cache-Control": "no-store",
        "Content-Type": "application/json; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      },
      '{"status":"ok","mode":"static_fallback"}\n',
      method,
    );
    return;
  }

  if (url.pathname === "/robots.txt" && (method === "GET" || method === "HEAD")) {
    send(
      res,
      200,
      { ...fallbackHeaders, "Content-Type": "text/plain; charset=utf-8" },
      "User-agent: *\nDisallow: /\n",
      method,
    );
    return;
  }

  const isReadOnlyPage =
    (method === "GET" || method === "HEAD") &&
    !url.pathname.startsWith("/api/") &&
    !url.pathname.startsWith("/_next/");

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
});

server.requestTimeout = 10_000;
server.headersTimeout = 5_000;
server.keepAliveTimeout = 5_000;
server.maxRequestsPerSocket = 100;

server.listen(port, host, () => {
  process.stdout.write(
    JSON.stringify({ event: "fallback_listening", host, port }) + "\n",
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
