import { describe, expect, it } from "vitest"
import { assertSafeWebhookUrl, isPrivateIp } from "./webhooks.service"

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
})
