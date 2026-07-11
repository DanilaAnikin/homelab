// ============================================================================
// Deliverability console (Phase 4) — verifies a sending domain is actually
// set up to land in the inbox, and generates the exact DNS records to publish.
//
// The checks reflect what receivers enforce:
//   • SPF authorizes the egress IP
//   • DKIM public key is published and matches ours
//   • DMARC record present
//   • PTR / forward-confirmed reverse DNS (FCrDNS) of the egress IP == HELO
//   • outbound port 25 works from the host running the check
// Pure helpers (spfAuthorizesIp, buildDeliverabilityRecords) are unit-tested;
// checkDeliverability performs live DNS/TCP and returns a structured report.
// ============================================================================
import { promises as dns } from "node:dns";
import net from "node:net";

export interface DeliverabilityCheck {
  key: "spf" | "dkim" | "dmarc" | "ptr" | "port25";
  label: string;
  ok: boolean;
  detail: string;
}

export interface DeliverabilityRecord {
  type: "TXT" | "PTR";
  name: string;
  value: string;
  purpose: string;
}

export interface DeliverabilityReport {
  domain: string;
  egressIp?: string;
  heloHostname?: string;
  checks: DeliverabilityCheck[];
  records: DeliverabilityRecord[];
  passed: number;
  total: number;
  ready: boolean;
}

/**
 * Does an SPF record explicitly authorize `ip` via an ip4 mechanism?
 * We only evaluate the ip4 term we instruct users to publish — fully resolving
 * a/mx/include would need recursive DNS and isn't needed for our own records.
 */
export function spfAuthorizesIp(spfText: string, ip: string): boolean {
  const s = spfText.toLowerCase();
  if (!s.includes("v=spf1")) return false;
  const esc = ip.replace(/\./g, "\\.");
  return new RegExp(`ip4:${esc}(?:/\\d+)?(?:\\s|$)`).test(s);
}

/** The exact DNS records to publish for direct delivery from `egressIp`. */
export function buildDeliverabilityRecords(opts: {
  domain: string;
  dkimSelector: string;
  dkimPublicKeyTxt: string;
  egressIp: string;
  heloHostname: string;
}): DeliverabilityRecord[] {
  const { domain, dkimSelector, dkimPublicKeyTxt, egressIp, heloHostname } =
    opts;
  return [
    {
      type: "TXT",
      name: domain,
      value: `v=spf1 ip4:${egressIp} ~all`,
      purpose: "SPF — authorizes the egress IP to send for this domain",
    },
    {
      type: "TXT",
      name: `${dkimSelector}._domainkey.${domain}`,
      value: dkimPublicKeyTxt,
      purpose: "DKIM — public key that verifies our signature",
    },
    {
      type: "TXT",
      name: `_dmarc.${domain}`,
      value: `v=DMARC1; p=none; rua=mailto:dmarc@${domain}`,
      purpose: "DMARC — reporting policy (tighten to quarantine after warm-up)",
    },
    {
      type: "PTR",
      name: egressIp,
      value: heloHostname,
      purpose:
        "PTR / reverse DNS — set at your VPS/host provider so the egress IP resolves to the HELO hostname",
    },
  ];
}

function tcpProbe(host: string, port: number, timeoutMs = 8000): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port });
    const finish = (ok: boolean) => {
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(timeoutMs);
    sock.once("connect", () => finish(true));
    sock.once("timeout", () => finish(false));
    sock.once("error", () => finish(false));
  });
}

async function txtFlat(name: string): Promise<string[]> {
  return (await dns.resolveTxt(name).catch(() => [])).map((chunks) =>
    chunks.join(""),
  );
}

export async function checkDeliverability(opts: {
  domain: string;
  dkimSelector: string;
  dkimPublicKeyTxt: string;
  egressIp?: string;
  heloHostname?: string;
}): Promise<DeliverabilityReport> {
  const { domain, dkimSelector, dkimPublicKeyTxt, egressIp, heloHostname } =
    opts;
  const checks: DeliverabilityCheck[] = [];

  const spf =
    (await txtFlat(domain)).find((t) => t.toLowerCase().includes("v=spf1")) ??
    "";
  checks.push({
    key: "spf",
    label: "SPF",
    ok: egressIp ? spfAuthorizesIp(spf, egressIp) : /v=spf1/i.test(spf),
    detail: spf || "no SPF record found",
  });

  const dkimTxts = (
    await txtFlat(`${dkimSelector}._domainkey.${domain}`)
  ).map((t) => t.replace(/\s+/g, ""));
  const expected = dkimPublicKeyTxt.replace(/\s+/g, "");
  checks.push({
    key: "dkim",
    label: "DKIM",
    ok: !!expected && dkimTxts.includes(expected),
    detail: dkimTxts.length ? "published" : "not published",
  });

  const dmarc =
    (await txtFlat(`_dmarc.${domain}`)).find((t) =>
      t.toLowerCase().includes("v=dmarc1"),
    ) ?? "";
  checks.push({
    key: "dmarc",
    label: "DMARC",
    ok: !!dmarc,
    detail: dmarc || "no DMARC record found",
  });

  if (egressIp && heloHostname) {
    const ptr = await dns.reverse(egressIp).catch(() => [] as string[]);
    const fwd = await dns.resolve4(heloHostname).catch(() => [] as string[]);
    const ptrMatch = ptr.some(
      (h) => h.toLowerCase() === heloHostname.toLowerCase(),
    );
    const fcrdns = fwd.includes(egressIp);
    checks.push({
      key: "ptr",
      label: "PTR / reverse DNS",
      ok: ptrMatch && fcrdns,
      detail: `PTR=${ptr.join(", ") || "none"}; ${heloHostname}→${fwd.join(", ") || "none"}`,
    });
  }

  // Outbound port 25 from the host running this check (meaningful when the
  // deliverability check runs on the egress node itself).
  const p25 = await tcpProbe("gmail-smtp-in.l.google.com", 25);
  checks.push({
    key: "port25",
    label: "Outbound port 25",
    ok: p25,
    detail: p25
      ? "open from this host"
      : "blocked/unreachable from this host (must be open on the egress node)",
  });

  const records =
    egressIp && heloHostname
      ? buildDeliverabilityRecords({
          domain,
          dkimSelector,
          dkimPublicKeyTxt,
          egressIp,
          heloHostname,
        })
      : [];

  const passed = checks.filter((c) => c.ok).length;
  return {
    domain,
    egressIp,
    heloHostname,
    checks,
    records,
    passed,
    total: checks.length,
    ready: passed === checks.length,
  };
}
