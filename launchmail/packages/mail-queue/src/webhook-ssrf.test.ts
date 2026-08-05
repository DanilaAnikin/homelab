import type { LookupAddress, LookupOptions } from "node:dns"
import type { LookupFunction } from "node:net"
import { describe, expect, it, vi } from "vitest"
import {
  assertSafeWebhookUrl,
  createPinnedLookup,
  isPrivateIp,
  SsrfError,
  type PinnedLookupResolver,
} from "./webhooks.service"

function invokeLookup(lookup: LookupFunction, options: LookupOptions) {
  return new Promise<{
    error: NodeJS.ErrnoException | null
    address: string | LookupAddress[]
    family: number | undefined
  }>((resolve) => {
    lookup("hooks.example.test", options, (error, address, family) => {
      resolve({ error, address, family })
    })
  })
}

describe("webhook SSRF IP classification", () => {
  it.each([
    "127.0.0.1",
    "10.0.0.1",
    "169.254.169.254",
    "100.64.0.1",
    "::1",
    "febf::1",
    "fd00::1",
    "::ffff:127.0.0.1",
    "::ffff:7f00:1",
    "64:ff9b::7f00:1",
  ])("blocks non-public address %s", (address) => {
    expect(isPrivateIp(address)).toBe(true)
  })

  it.each(["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])(
    "allows public unicast address %s",
    (address) => {
      expect(isPrivateIp(address)).toBe(false)
    }
  )

  it("handles bracketed IPv6 URL literals without bypass or false rejection", async () => {
    await expect(assertSafeWebhookUrl("http://[febf::1]/hook")).rejects.toThrow(
      "blocked address"
    )
    await expect(
      assertSafeWebhookUrl("https://[2606:4700:4700::1111]/hook")
    ).resolves.toBeUndefined()
  })

  it("returns an address array for Node 20 all:true lookups", async () => {
    const resolveAll = vi.fn<PinnedLookupResolver>().mockResolvedValue([
      { address: "1.1.1.1", family: 4 },
      { address: "2606:4700:4700::1111", family: 6 },
      { address: "8.8.8.8", family: 4 },
    ])
    const lookup = createPinnedLookup(
      [
        { address: "8.8.8.8", family: 4 },
        { address: "2606:4700:4700::1111", family: 6 },
      ],
      resolveAll
    )

    const result = await invokeLookup(lookup, { all: true })

    expect(result).toEqual({
      error: null,
      address: [
        { address: "2606:4700:4700::1111", family: 6 },
        { address: "8.8.8.8", family: 4 },
      ],
      family: undefined,
    })
    expect(resolveAll).toHaveBeenCalledWith(
      "hooks.example.test",
      expect.objectContaining({ all: true })
    )
  })

  it("returns one address and family for single-address lookups", async () => {
    const resolveAll = vi.fn<PinnedLookupResolver>().mockResolvedValue([
      { address: "2606:4700:4700::1111", family: 6 },
      { address: "8.8.8.8", family: 4 },
    ])
    const lookup = createPinnedLookup(
      [
        { address: "8.8.8.8", family: 4 },
        { address: "2606:4700:4700::1111", family: 6 },
      ],
      resolveAll
    )

    const result = await invokeLookup(lookup, { all: false, family: 4 })

    expect(result).toEqual({ error: null, address: "8.8.8.8", family: 4 })
    expect(resolveAll).toHaveBeenCalledWith(
      "hooks.example.test",
      expect.objectContaining({ all: true, family: 4 })
    )
  })

  it.each([
    {
      name: "blocked private answer",
      addresses: [{ address: "127.0.0.1", family: 4 }],
    },
    {
      name: "public answer outside the pinned set",
      addresses: [{ address: "1.1.1.1", family: 4 }],
    },
  ])("fails closed for $name", async ({ addresses }) => {
    const resolveAll = vi
      .fn<PinnedLookupResolver>()
      .mockResolvedValue(addresses)
    const lookup = createPinnedLookup(
      [{ address: "8.8.8.8", family: 4 }],
      resolveAll
    )

    const allResult = await invokeLookup(lookup, { all: true })
    const singleResult = await invokeLookup(lookup, { all: false })

    expect(allResult.error).toBeInstanceOf(SsrfError)
    expect(allResult.address).toEqual([])
    expect(singleResult.error).toBeInstanceOf(SsrfError)
    expect(singleResult.address).toBe("")
  })
})
