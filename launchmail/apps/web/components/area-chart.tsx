"use client";

import * as React from "react";
import {
  Area,
  AreaChart as RechartsAreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { cn } from "@workspace/ui/lib/utils";

/**
 * A point on the chart. `label` is the (already formatted) x-axis tick,
 * e.g. "Jun 13". Every numeric key beyond `label` is treated as a series.
 */
export type AreaChartPoint = { label: string } & Record<string, number | string>;

export interface AreaChartSeries {
  /** Object key on each point that holds this series' value. */
  key: string;
  /** Human label shown in the legend + tooltip. */
  name: string;
  /** Chart color token (defaults to --chart-1 / brand). */
  color?: string;
}

/**
 * Legacy single-series shape: `{ label, value }[]`. Still supported so existing
 * callers keep working — rendered as one "Sent" brand series.
 */
type LegacyPoint = { label: string; value: number };

function isLegacy(
  data: AreaChartPoint[] | LegacyPoint[]
): data is LegacyPoint[] {
  return data.length === 0 || "value" in data[0]!;
}

const DEFAULT_SERIES: AreaChartSeries[] = [
  { key: "value", name: "Sent", color: "var(--chart-1)" },
];

function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { name?: string; value?: number; color?: string; dataKey?: string }[];
  label?: string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="min-w-32 rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-[var(--elevation-3)]">
      <p className="text-eyebrow text-muted-foreground">{label}</p>
      <div className="mt-2 space-y-1">
        {payload.map((entry) => (
          <div
            key={entry.dataKey ?? entry.name}
            className="flex items-center justify-between gap-4"
          >
            <span className="flex items-center gap-1.5 text-caption text-muted-foreground">
              <span
                aria-hidden="true"
                className="size-2 shrink-0 rounded-full"
                style={{ backgroundColor: entry.color }}
              />
              {entry.name}
            </span>
            <span className="text-mono tabular-nums text-foreground">
              {typeof entry.value === "number"
                ? entry.value.toLocaleString()
                : entry.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function AreaChart({
  data,
  series,
  className,
}: {
  data: AreaChartPoint[] | LegacyPoint[];
  series?: AreaChartSeries[];
  className?: string;
}) {
  const resolvedSeries = series ?? (isLegacy(data) ? DEFAULT_SERIES : []);

  // Track which series are visible; all on by default. Toggled via the legend.
  const [hidden, setHidden] = React.useState<Record<string, boolean>>({});

  function toggle(key: string) {
    setHidden((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      // Never let the user hide the final visible series.
      const anyVisible = resolvedSeries.some((s) => !next[s.key]);
      return anyVisible ? next : prev;
    });
  }

  const gridStroke = "var(--border)";
  const axisStroke = "var(--muted-foreground)";

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {resolvedSeries.length > 1 ? (
        <div
          role="group"
          aria-label="Toggle chart series"
          className="flex flex-wrap items-center gap-1.5"
        >
          {resolvedSeries.map((s) => {
            const color = s.color ?? "var(--chart-1)";
            const isHidden = !!hidden[s.key];
            return (
              <button
                key={s.key}
                type="button"
                onClick={() => toggle(s.key)}
                aria-pressed={!isHidden}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-badge border border-border px-2 py-1 text-caption font-medium transition-[color,background-color,border-color] duration-[var(--duration-fast)] ease-[var(--ease-standard)] hover:bg-muted focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
                  isHidden
                    ? "text-muted-foreground"
                    : "text-foreground"
                )}
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    "size-2 shrink-0 rounded-full transition-opacity",
                    isHidden && "opacity-30"
                  )}
                  style={{ backgroundColor: color }}
                />
                {s.name}
              </button>
            );
          })}
        </div>
      ) : null}

      <div className="h-56 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <RechartsAreaChart
            data={data}
            margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
          >
            <defs>
              {resolvedSeries.map((s) => {
                const color = s.color ?? "var(--chart-1)";
                return (
                  <linearGradient
                    key={s.key}
                    id={`area-fill-${s.key}`}
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="1"
                  >
                    <stop offset="0%" stopColor={color} stopOpacity={0.2} />
                    <stop offset="100%" stopColor={color} stopOpacity={0} />
                  </linearGradient>
                );
              })}
            </defs>
            <CartesianGrid
              vertical={false}
              stroke={gridStroke}
              strokeDasharray="3 3"
            />
            <XAxis
              dataKey="label"
              tickLine={false}
              axisLine={false}
              tick={{ fill: axisStroke, fontSize: 11 }}
              tickMargin={8}
              minTickGap={24}
            />
            <YAxis
              tickLine={false}
              axisLine={false}
              tick={{ fill: axisStroke, fontSize: 11 }}
              width={40}
              allowDecimals={false}
            />
            <Tooltip
              content={<ChartTooltip />}
              cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
            />
            {resolvedSeries.map((s) => {
              const color = s.color ?? "var(--chart-1)";
              return (
                <Area
                  key={s.key}
                  type="monotone"
                  dataKey={s.key}
                  name={s.name}
                  stroke={color}
                  strokeWidth={2}
                  fill={`url(#area-fill-${s.key})`}
                  hide={!!hidden[s.key]}
                  isAnimationActive={false}
                  activeDot={{ r: 3, strokeWidth: 0 }}
                />
              );
            })}
          </RechartsAreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
