import { Card, CardContent } from "@workspace/ui/components/card";
import { Skeleton } from "@workspace/ui/components/skeleton";

// Layout-matched skeleton for the logs cockpit: page header + stat strip +
// filter chips + table rows at the real row height. The envelope loader is
// reserved for auth / first paint and is never used inside the shell.
export default function Loading() {
  return (
    <div className="mx-auto max-w-6xl space-y-8">
      {/* PageHeader */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-2">
          <Skeleton className="h-8 w-44" />
          <Skeleton className="h-4 w-72" />
        </div>
        <Skeleton className="h-7 w-28 rounded-md" />
      </div>

      {/* Stat strip */}
      <div className="grid grid-cols-3 divide-x divide-border overflow-hidden rounded-lg border border-border bg-card">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="flex flex-col gap-2 p-4">
            <Skeleton className="h-3 w-16" />
            <Skeleton className="h-7 w-20" />
            <Skeleton className="h-3 w-12" />
          </div>
        ))}
      </div>

      {/* Table card */}
      <Card>
        <CardContent className="p-0">
          {/* Filter chips */}
          <div className="flex flex-wrap items-center gap-1.5 px-3 py-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-6 w-16 rounded-badge" />
            ))}
          </div>

          {/* Header rule */}
          <div className="flex items-center gap-4 border-b border-border-strong px-3 py-2">
            <Skeleton className="h-3 w-14" />
            <Skeleton className="h-3 w-20" />
            <Skeleton className="ml-auto h-3 w-24" />
          </div>

          {/* Rows at the real row height */}
          {Array.from({ length: 8 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center gap-4 border-b border-border px-3 py-3 last:border-b-0"
            >
              <Skeleton className="h-5 w-20 rounded-badge" />
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-4 w-40" />
              <Skeleton className="ml-auto h-4 w-16" />
              <Skeleton className="h-4 w-24" />
            </div>
          ))}

          {/* Pagination */}
          <div className="flex items-center justify-between gap-3 px-3 py-3">
            <Skeleton className="h-3 w-40" />
            <Skeleton className="h-7 w-44 rounded-md" />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
