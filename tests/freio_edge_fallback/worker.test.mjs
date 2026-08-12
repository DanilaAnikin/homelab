import assert from "node:assert/strict";
import test from "node:test";

import {
  handleRequest,
} from "../../compose/cloudflare/freio-edge-fallback/worker.mjs";

function request(path = "/", options = {}) {
  return new Request(`https://freio.cz${path}`, {
    method: options.method ?? "GET",
    headers: {
      accept: options.accept ?? "text/html,application/xhtml+xml",
      ...(options.headers ?? {}),
    },
    body: options.body,
  });
}

function originResponse(status = 200, body = "origin") {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain" },
  });
}

async function assertFallback(response, method = "GET") {
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-freio-edge-fallback"), "static-v1");
  assert.equal(response.headers.get("cache-control"), "no-store, max-age=0");
  assert.equal(response.headers.get("retry-after"), "20");
  assert.match(response.headers.get("content-security-policy"), /default-src 'none'/);
  const body = await response.text();
  if (method === "HEAD") {
    assert.equal(body, "");
  } else {
    assert.match(body, /Záložní režim je aktivní/);
    assert.doesNotMatch(body, /<script|https?:\/\//i);
  }
}

async function assertFailClosed(response) {
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("x-freio-edge-fallback"), "static-v1");
  assert.equal(response.headers.get("cache-control"), "no-store, max-age=0");
  assert.equal(response.headers.get("retry-after"), "20");
  assert.match(response.headers.get("content-security-policy"), /form-action 'none'/);
  assert.deepEqual(await response.json(), {
    error: "service_temporarily_unavailable",
    edge_fallback: true,
  });
}

test("passes a healthy origin response through unchanged", async () => {
  const origin = originResponse(200, "primary");
  const response = await handleRequest(request(), async () => origin);
  assert.equal(response, origin);
  assert.equal(await response.text(), "primary");
});

for (const status of [500, 501, 502, 503, 504]) {
  test(`serves inline HTML fallback for navigation origin status ${status}`, async () => {
    const response = await handleRequest(
      request("/pricing", {
        headers: { "sec-fetch-mode": "navigate", "sec-fetch-dest": "document" },
      }),
      async () => originResponse(status),
    );
    await assertFallback(response);
  });
}

test("passes 505 through and does not broaden the fallback status range", async () => {
  const origin = originResponse(505, "version not supported");
  const response = await handleRequest(request(), async () => origin);
  assert.equal(response, origin);
  assert.equal(response.status, 505);
  assert.equal(response.headers.get("x-freio-edge-fallback"), null);
});

test("serves HTML fallback on an origin exception and timeout", async (t) => {
  await t.test("exception", async () => {
    const response = await handleRequest(request(), async () => {
      throw new Error("origin unavailable");
    });
    await assertFallback(response);
  });

  await t.test("bounded timeout", async () => {
    const response = await handleRequest(
      request(),
      (_request, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener("abort", () => reject(init.signal.reason), {
            once: true,
          });
        }),
      5,
    );
    await assertFallback(response);
  });
});

test("HEAD navigation fallback has no body", async () => {
  const response = await handleRequest(
    request("/pricing", { method: "HEAD" }),
    async () => originResponse(502),
  );
  await assertFallback(response, "HEAD");
});

test("API, Next assets and non-navigation GET requests fail closed", async (t) => {
  for (const [name, req] of [
    ["api", request("/api/users/me")],
    ["api root", request("/api")],
    ["next", request("/_next/static/app.js")],
    ["next root", request("/_next")],
    ["json accept", request("/pricing", { accept: "application/json" })],
    [
      "fetch mode",
      request("/pricing", { headers: { "sec-fetch-mode": "cors" } }),
    ],
  ]) {
    await t.test(name, async () => {
      await assertFailClosed(
        await handleRequest(req, async () => originResponse(502)),
      );
    });
  }
});

test("encoded or ambiguous paths fail closed", async (t) => {
  for (const path of [
    "/%61pi/users/me",
    "/api%2Fusers%2Fme",
    "/%5Fnext/static/app.js",
    "/%255fnext/static/app.js",
    "//api/users/me",
  ]) {
    await t.test(path, async () => {
      await assertFailClosed(
        await handleRequest(request(path), async () => originResponse(502)),
      );
    });
  }
});

test("write requests are attempted once and never replayed", async (t) => {
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    for (const failure of ["exception", "status"]) {
      await t.test(`${method} ${failure}`, async () => {
        let calls = 0;
        const response = await handleRequest(
          request("/api/checkout", {
            method,
            accept: "application/json",
            body: '{"plan":"season"}',
            headers: { "content-type": "application/json" },
          }),
          async () => {
            calls += 1;
            if (failure === "exception") throw new Error("connection reset");
            return originResponse(503);
          },
        );
        assert.equal(calls, 1);
        await assertFailClosed(response);
      });
    }
  }
});

test("successful write requests pass through after one origin attempt", async () => {
  let calls = 0;
  const origin = new Response('{"ok":true}', {
    status: 201,
    headers: { "content-type": "application/json" },
  });
  const response = await handleRequest(
    request("/api/checkout", {
      method: "POST",
      accept: "application/json",
      body: "{}",
      headers: { "content-type": "application/json" },
    }),
    async () => {
      calls += 1;
      return origin;
    },
  );
  assert.equal(calls, 1);
  assert.equal(response, origin);
});

test("redirects HTTP to HTTPS with 308 before origin fetch", async () => {
  let calls = 0;
  const response = await handleRequest(
    new Request("http://www.freio.cz/pricing?source=test"),
    async () => {
      calls += 1;
      return originResponse();
    },
  );
  assert.equal(calls, 0);
  assert.equal(response.status, 308);
  assert.equal(
    response.headers.get("location"),
    "https://www.freio.cz/pricing?source=test",
  );
  assert.equal(response.headers.get("cache-control"), "no-store, max-age=0");
});

test("health endpoint is secretless and does not call origin", async () => {
  let calls = 0;
  const response = await handleRequest(
    request("/__freio-edge-health", { accept: "application/json" }),
    async () => {
      calls += 1;
      return originResponse();
    },
  );
  assert.equal(calls, 0);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-freio-edge-fallback"), "health-v1");
  assert.deepEqual(await response.json(), {
    status: "ok",
    component: "freio-edge-fallback",
    mode: "standby",
  });
});

test("malformed URLs and unexpected hosts fail closed without origin fetch", async (t) => {
  for (const [name, req] of [
    [
      "malformed",
      {
        method: "GET",
        url: "not a URL",
        headers: new Headers({ accept: "text/html" }),
      },
    ],
    ["unexpected host", new Request("https://attacker.invalid/")],
  ]) {
    await t.test(name, async () => {
      let calls = 0;
      const response = await handleRequest(req, async () => {
        calls += 1;
        return originResponse();
      });
      assert.equal(calls, 0);
      await assertFailClosed(response);
    });
  }
});
