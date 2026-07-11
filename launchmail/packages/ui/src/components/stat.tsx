"use client"

import * as React from "react"
import { Line, LineChart, ResponsiveContainer } from "recharts"
import { ArrowDownIcon, ArrowUpIcon } from "lucide-react"

import { cn } from "@workspace/ui/lib/utils"

/**
 * A horizontal strip of <Stat> cells separated by hairlines.
 * Exactly one cell per strip should carry `accent` (the primary KPI).
 */
function StatGroup({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="stat-group"
      className={cn(
        "grid grid-cols-2 divide-x divide-y divide-border overflow-hidden rounded-lg border border-border bg-card sm:grid-cols-3 lg:divide-y-0 [&>*]:-mr-px [&>*]:-mb-px lg:grid-cols-5",
        className
      )}
      {...props}
    />
  )
}

type StatDeltaDirection = "up" | "down" | "neutral"

type SparkPoint = number | { value: number }

type StatProps = Omit<React.ComponentProps<"div">, "title"> & {
  /** Uppercase eyebrow label. */
  label: React.ReactNode
  /** The metric value (already formatted). Rendered with tabular-nums. */
  value: React.ReactNode
  /** Signed trend delta, e.g. "+4.1%". Colored by `deltaDirection`. */
  delta?: React.ReactNode
  /**
   * Direction of the delta. `up` → success, `down` → destructive,
   * `neutral` → muted. Defaults to inferring from a numeric `delta`.
   */
  deltaDirection?: StatDeltaDirection
  /** Optional 40px-high sparkline series. */
  sparkline?: SparkPoint[]
  /** Mark this cell as the primary KPI (brand accent + brand spark). */
  accent?: boolean
}

function inferDirection(
  delta: React.ReactNode,
  explicit?: StatDeltaDirection
): StatDeltaDirection {
  if (explicit) return explicit
  if (typeof delta === "string") {
    if (delta.trim().startsWith("-")) return "down"
    if (delta.trim().startsWith("+")) return "up"
  }
  if (typeof delta === "number") {
    if (delta < 0) return "down"
    if (delta > 0) return "up"
  }
  return "neutral"
}

function Stat({
  className,
  label,
  value,
  delta,
  deltaDirection,
  sparkline,
  accent = false,
  ...props
}: StatProps) {
  const direction = inferDirection(delta, deltaDirection)
  const sparkData = React.useMemo(
    () =>
      (sparkline ?? []).map((point, index) =>
        typeof point === "number"
          ? { index, value: point }
          : { index, value: point.value }
      ),
    [sparkline]
  )
  const sparkStroke = accent ? "var(--brand-500)" : "var(--muted-foreground)"

  return (
    <div
      data-slot="stat"
      data-accent={accent || undefined}
      className={cn(
        "relative flex flex-col gap-1.5 bg-card p-4",
        accent &&
          "before:absolute before:inset-y-0 before:left-0 before:w-0.5 before:bg-brand-500",
        className
      )}
      {...props}
    >
      <span className="text-eyebrow">{label}</span>
      <div className="flex items-end justify-between gap-2">
        <span
          className={cn(
            "text-stat tabular-nums",
            accent ? "text-brand-600 dark:text-brand-400" : "text-foreground"
          )}
        >
          {value}
        </span>
        {sparkData.length > 1 ? (
          <div className="h-10 w-20 shrink-0" aria-hidden="true">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={sparkData}
                margin={{ top: 4, right: 0, bottom: 4, left: 0 }}
              >
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke={sparkStroke}
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : null}
      </div>
      {delta != null && delta !== "" ? (
        <span
          data-direction={direction}
          className={cn(
            "inline-flex items-center gap-0.5 text-caption font-medium tabular-nums",
            direction === "up" && "text-success-on",
            direction === "down" && "text-destructive-on",
            direction === "neutral" && "text-muted-foreground"
          )}
        >
          {direction === "up" ? (
            <ArrowUpIcon className="size-3" />
          ) : direction === "down" ? (
            <ArrowDownIcon className="size-3" />
          ) : null}
          {delta}
        </span>
      ) : null}
    </div>
  )
}

export { Stat, StatGroup }
