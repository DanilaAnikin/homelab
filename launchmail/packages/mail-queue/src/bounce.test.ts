import { describe, expect, it } from "vitest";
import { simpleParser } from "mailparser";
import { parseBounce, verpReturnPath, parseVerpToken } from "./bounce";

describe("VERP", () => {
  it("builds a bounces+<id>@domain return-path (lowercased domain)", () => {
    expect(verpReturnPath("Ripieno.XYZ", "abc-123")).toBe(
      "bounces+abc-123@ripieno.xyz",
    );
  });
  it("extracts the token from a VERP address", () => {
    expect(parseVerpToken("bounces+abc-123@ripieno.xyz")).toBe("abc-123");
    expect(parseVerpToken("Mailer <bounces+XYZ@ripieno.xyz>")).toBe("xyz");
  });
  it("returns null for non-VERP or empty addresses", () => {
    expect(parseVerpToken("hi@ripieno.xyz")).toBeNull();
    expect(parseVerpToken(null)).toBeNull();
    expect(parseVerpToken(undefined)).toBeNull();
  });
});

// A realistic RFC 3464 delivery-status notification (multipart/report).
const HARD_BOUNCE_DSN = [
  "From: Mail Delivery System <MAILER-DAEMON@mx.example.net>",
  "To: bounces+log-abc-123@ripieno.xyz",
  "Subject: Undelivered Mail Returned to Sender",
  'Content-Type: multipart/report; report-type=delivery-status; boundary="B"',
  "",
  "--B",
  "Content-Type: text/plain",
  "",
  "This is the mail system at host mx.example.net.",
  "Your message could not be delivered to nobody@gmail.com.",
  "",
  "--B",
  "Content-Type: message/delivery-status",
  "",
  "Reporting-MTA: dns; mx.example.net",
  "",
  "Final-Recipient: rfc822; nobody@gmail.com",
  "Action: failed",
  "Status: 5.1.1",
  "Diagnostic-Code: smtp; 550 5.1.1 The email account does not exist.",
  "",
  "--B--",
  "",
].join("\r\n");

const TRANSIENT_DSN = [
  "From: postmaster@mx.example.net",
  "To: bounces+log-t-9@ripieno.xyz",
  "Subject: Delivery Status Notification (Delay)",
  'Content-Type: multipart/report; report-type=delivery-status; boundary="C"',
  "",
  "--C",
  "Content-Type: message/delivery-status",
  "",
  "Final-Recipient: rfc822; busy@seznam.cz",
  "Action: delayed",
  "Status: 4.2.2",
  "Diagnostic-Code: smtp; 452 4.2.2 Mailbox full",
  "",
  "--C--",
  "",
].join("\r\n");

describe("parseBounce", () => {
  it("parses a permanent (5.x.x) bounce with recipient + VERP token", async () => {
    const parsed = await simpleParser(HARD_BOUNCE_DSN);
    const b = parseBounce(parsed);
    expect(b.isBounce).toBe(true);
    expect(b.permanent).toBe(true);
    expect(b.recipient).toBe("nobody@gmail.com");
    expect(b.status).toBe("5.1.1");
    expect(b.verpToken).toBe("log-abc-123");
    expect(b.diagnostic).toMatch(/does not exist/i);
  });

  it("treats a 4.x.x delayed report as transient (not permanent)", async () => {
    const parsed = await simpleParser(TRANSIENT_DSN);
    const b = parseBounce(parsed);
    expect(b.isBounce).toBe(true);
    expect(b.permanent).toBe(false);
    expect(b.status).toBe("4.2.2");
  });

  it("does not flag a normal reply as a bounce", async () => {
    const parsed = await simpleParser(
      [
        "From: Jana <jana@seznam.cz>",
        "To: hi@ripieno.xyz",
        "Subject: Re: your invoice",
        "Content-Type: text/plain",
        "",
        "Thanks, looks good!",
        "",
      ].join("\r\n"),
    );
    const b = parseBounce(parsed);
    expect(b.isBounce).toBe(false);
    expect(b.permanent).toBe(false);
  });
});
