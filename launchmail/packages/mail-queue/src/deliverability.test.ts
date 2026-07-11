import { describe, expect, it } from "vitest";
import {
  spfAuthorizesIp,
  buildDeliverabilityRecords,
} from "./deliverability";

describe("spfAuthorizesIp", () => {
  it("accepts an SPF that lists the ip4 mechanism", () => {
    expect(spfAuthorizesIp("v=spf1 ip4:185.1.2.3 ~all", "185.1.2.3")).toBe(true);
    expect(spfAuthorizesIp("v=spf1 ip4:185.1.2.0/24 ~all", "185.1.2.0")).toBe(
      true,
    );
  });
  it("rejects when the IP is not authorized", () => {
    expect(spfAuthorizesIp("v=spf1 ip4:10.0.0.1 ~all", "185.1.2.3")).toBe(false);
    expect(spfAuthorizesIp("v=spf1 a mx ~all", "185.1.2.3")).toBe(false);
  });
  it("rejects a non-SPF string", () => {
    expect(spfAuthorizesIp("some txt record", "185.1.2.3")).toBe(false);
  });
  it("does not match a different IP that shares a prefix", () => {
    expect(spfAuthorizesIp("v=spf1 ip4:185.1.2.3 ~all", "185.1.2.30")).toBe(
      false,
    );
  });
});

describe("buildDeliverabilityRecords", () => {
  const records = buildDeliverabilityRecords({
    domain: "ripieno.xyz",
    dkimSelector: "launchmail",
    dkimPublicKeyTxt: "v=DKIM1; k=rsa; p=ABC",
    egressIp: "185.1.2.3",
    heloHostname: "mail.ripieno.xyz",
  });
  it("emits SPF with the egress IP", () => {
    const spf = records.find((r) => r.purpose.startsWith("SPF"));
    expect(spf?.value).toBe("v=spf1 ip4:185.1.2.3 ~all");
    expect(spf?.name).toBe("ripieno.xyz");
  });
  it("emits the DKIM record at the selector subdomain", () => {
    const dkim = records.find((r) => r.purpose.startsWith("DKIM"));
    expect(dkim?.name).toBe("launchmail._domainkey.ripieno.xyz");
    expect(dkim?.value).toBe("v=DKIM1; k=rsa; p=ABC");
  });
  it("emits DMARC and a PTR advisory for the egress IP", () => {
    expect(records.find((r) => r.purpose.startsWith("DMARC"))?.name).toBe(
      "_dmarc.ripieno.xyz",
    );
    const ptr = records.find((r) => r.type === "PTR");
    expect(ptr?.name).toBe("185.1.2.3");
    expect(ptr?.value).toBe("mail.ripieno.xyz");
  });
});
