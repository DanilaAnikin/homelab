"use client";

import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

/* Re-keyed chart tokens (globals.css):
 *   --chart-1 = brand   (primary series → sent)
 *   --chart-2 = info    (opens)
 *   --chart-3 = success
 *   --chart-4 = warning (clicks)
 *   --chart-5 = destructive (failed)
 */
export interface VolumePoint {
  label: string;
  sent: number;
  failed: number;
  opens: number;
  clicks: number;
}

const SERIES = [
  { key: "sent", name: "Sent", color: "var(--chart-1)" },
  { key: "failed", name: "Failed", color: "var(--chart-5)" },
  { key: "opens", name: "Opens", color: "var(--chart-2)" },
  { key: "clicks", name: "Clicks", color: "var(--chart-4)" },
] as const;

export function VolumeChart({ data }: { data: VolumePoint[] }) {
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={data}
          margin={{ top: 8, right: 8, bottom: 0, left: -16 }}
        >
          <defs>
            <linearGradient id="vol-sent" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.18} />
              <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--border)"
            vertical={false}
          />
          <XAxis
            dataKey="label"
            tickLine={false}
            axisLine={false}
            stroke="var(--muted-foreground)"
            tick={{ fontSize: 11 }}
            minTickGap={16}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            stroke="var(--muted-foreground)"
            tick={{ fontSize: 11 }}
            width={44}
            allowDecimals={false}
          />
          <Tooltip
            cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
            contentStyle={{
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--border)",
              background: "var(--popover)",
              color: "var(--popover-foreground)",
              fontSize: 12,
              boxShadow: "var(--elevation-3)",
            }}
            labelStyle={{ color: "var(--muted-foreground)", marginBottom: 4 }}
            itemStyle={{ padding: 0 }}
          />
          <Legend
            iconType="plainline"
            wrapperStyle={{ fontSize: 12, paddingTop: 8 }}
          />
          <Area
            type="monotone"
            dataKey="sent"
            name="Sent"
            stroke="var(--chart-1)"
            strokeWidth={2}
            fill="url(#vol-sent)"
            isAnimationActive={false}
          />
          {SERIES.filter((s) => s.key !== "sent").map((s) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.name}
              stroke={s.color}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
