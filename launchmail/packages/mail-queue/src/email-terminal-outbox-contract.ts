/**
 * Validate a caller's optional exact webhook-outbox contract. Callers that
 * need atomic fail-closed behavior must invoke this before their surrounding
 * database transaction returns.
 */
export function assertExpectedEmailTerminalOutboxRows(
  expected: number | undefined,
  actual: number
): void {
  if (expected === undefined) return
  if (!Number.isSafeInteger(expected) || expected < 0) {
    throw new Error(
      "Expected terminal webhook outbox count must be a non-negative integer"
    )
  }
  if (actual !== expected) {
    throw new Error(
      `Terminal webhook outbox count mismatch: expected ${expected}, created ${actual}`
    )
  }
}
