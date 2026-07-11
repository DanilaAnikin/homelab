import { describe, expect, it } from "vitest";
import {
  extractEmail,
  domainOf,
  groupRecipientsByDomain,
  classifyDeliveryError,
  resolveMxHosts,
  sendDirect,
  type MxResolver,
} from "./direct-transport";
import type { SendMailInput } from "./smtp";
import type { SmtpConfig } from "./smtp-configs.service";

describe("extractEmail / domainOf", () => {
  it("pulls the address out of a display-name header", () => {
    expect(extractEmail("Jan Novák <Jan@Example.COM>")).toBe("jan@example.com");
  });
  it("passes a bare address through, lowercased", () => {
    expect(extractEmail("USER@Domain.io")).toBe("user@domain.io");
  });
  it("returns the domain part", () => {
    expect(domainOf("user@sub.domain.io")).toBe("sub.domain.io");
    expect(domainOf("garbage")).toBe("");
  });
});

describe("groupRecipientsByDomain", () => {
  it("buckets recipients by domain and drops malformed ones", () => {
    const g = groupRecipientsByDomain([
      "a@gmail.com",
      "b@gmail.com",
      "c@seznam.cz",
      "notanemail",
    ]);
    expect(g.get("gmail.com")).toEqual(["a@gmail.com", "b@gmail.com"]);
    expect(g.get("seznam.cz")).toEqual(["c@seznam.cz"]);
    expect(g.has("")).toBe(false);
    expect(g.size).toBe(2);
  });
  it("dedupes identical addresses within a domain", () => {
    const g = groupRecipientsByDomain(["x@a.io", "X@A.io"]);
    expect(g.get("a.io")).toEqual(["x@a.io"]);
  });
});

describe("classifyDeliveryError", () => {
  it("treats network errors as retryable", () => {
    expect(classifyDeliveryError({ code: "ETIMEDOUT" }).retryable).toBe(true);
    expect(classifyDeliveryError({ code: "ECONNREFUSED" }).retryable).toBe(true);
  });
  it("treats 4xx as retryable, 5xx as permanent", () => {
    expect(classifyDeliveryError({ responseCode: 451 }).retryable).toBe(true);
    expect(classifyDeliveryError({ responseCode: 550 })).toEqual({
      retryable: false,
      code: 550,
    });
  });
  it("defaults unknown shapes to retryable", () => {
    expect(classifyDeliveryError(new Error("weird")).retryable).toBe(true);
  });
});

describe("resolveMxHosts", () => {
  it("sorts MX by ascending priority", async () => {
    const resolver: MxResolver = {
      resolveMx: async () => [
        { exchange: "mx2.test", priority: 20 },
        { exchange: "mx1.test", priority: 10 },
        { exchange: "mx3.test", priority: 30 },
      ],
    };
    expect(await resolveMxHosts("test", resolver)).toEqual([
      "mx1.test",
      "mx2.test",
      "mx3.test",
    ]);
  });
  it("falls back to the domain itself when there are no MX records", async () => {
    const resolver: MxResolver = { resolveMx: async () => [] };
    expect(await resolveMxHosts("bare.io", resolver)).toEqual(["bare.io"]);
  });
  it("falls back to the domain on DNS error (ENOTFOUND)", async () => {
    const resolver: MxResolver = {
      resolveMx: async () => {
        throw Object.assign(new Error("not found"), { code: "ENOTFOUND" });
      },
    };
    expect(await resolveMxHosts("nope.io", resolver)).toEqual(["nope.io"]);
  });
});

describe("sendDirect guards", () => {
  const baseConfig = {
    id: "cfg-1",
    type: "direct",
    heloHostname: "mail.ripieno.xyz",
    fromAddress: "hi@ripieno.xyz",
  } as unknown as SmtpConfig;

  const baseInput: SendMailInput = {
    from: "hi@ripieno.xyz",
    to: [{ email: "someone@gmail.com" }],
    subject: "hello",
    smtpConfig: baseConfig,
  };

  it("refuses to send without DKIM (permanent 5xx)", async () => {
    await expect(sendDirect(baseInput)).rejects.toMatchObject({
      responseCode: 550,
    });
  });

  it("refuses to send without heloHostname (permanent 5xx)", async () => {
    const input: SendMailInput = {
      ...baseInput,
      smtpConfig: { ...baseConfig, heloHostname: null } as SmtpConfig,
      dkim: { domainName: "ripieno.xyz", keySelector: "launchmail", privateKey: "x" },
    };
    await expect(sendDirect(input)).rejects.toMatchObject({ responseCode: 550 });
  });
});
