import { describe, expect, it } from "vitest";
import { sendEmailSchema } from "./types";

const message = {
  to: [{ email: "school@example.cz" }],
  subject: "Freio",
  text: "Dobrý den",
};

describe("sendEmailSchema restricted headers", () => {
  it("accepts a UUID client reference and rejects arbitrary correlation text", () => {
    expect(
      sendEmailSchema.safeParse({
        ...message,
        clientReference: "b6f8d457-b62e-45cd-a4f4-520227af67dc",
      }).success,
    ).toBe(true);
    expect(
      sendEmailSchema.safeParse({
        ...message,
        clientReference: "recipient@example.cz",
      }).success,
    ).toBe(false);
  });

  it("accepts registered Freio client types and rejects unknown namespaces", () => {
    expect(
      sendEmailSchema.safeParse({
        ...message,
        clientType: "freio_partner_outreach",
      }).success,
    ).toBe(true);
    expect(
      sendEmailSchema.safeParse({
        ...message,
        clientType: "other_project",
      }).success,
    ).toBe(false);
  });

  it("accepts the RFC 8058 unsubscribe pair", () => {
    expect(
      sendEmailSchema.safeParse({
        ...message,
        headers: {
          "List-Unsubscribe": "<https://freio.cz/unsubscribe?id=1>",
          "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
      }).success,
    ).toBe(true);
  });

  it("rejects identity or routing header overrides", () => {
    expect(
      sendEmailSchema.safeParse({
        ...message,
        headers: { From: "attacker@example.test" },
      }).success,
    ).toBe(false);
    expect(
      sendEmailSchema.safeParse({
        ...message,
        headers: { "List-Unsubscribe-Post": "invalid" },
      }).success,
    ).toBe(false);
  });
});
