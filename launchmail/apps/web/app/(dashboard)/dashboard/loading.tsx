import { Skeleton } from "@workspace/ui/components/skeleton";
import { Card, CardContent } from "@workspace/ui/components/card";

export default function Loading() {
  return (
    <div className="mx-auto max-w-6xl space-y-8">
      {/* Header band */}
      <div className="-mx-6 -mt-6 rounded-b-xl bg-[radial-gradient(120%_140%_at_0%_0%,var(--brand-50),transparent_60%)] px-6 pt-6 pb-8 lg:-mx-8 lg:px-8 dark:bg-[radial-gradient(120%_140%_at_0%_0%,var(--brand-subtle),transparent_55%)]">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-2">
            <Skeleton className="h-8 w-40" />
            <Skeleton className="h-3.5 w-56" />
          </div>
          <Skeleton className="h-8 w-36 rounded-md" />
        </div>
      </div>

      {/* <Stat> strip */}
      <div className="grid grid-cols-2 divide-x divide-y divide-border overflow-hidden rounded-lg border border-border bg-card sm:grid-cols-3 lg:grid-cols-5 lg:divide-y-0">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="flex flex-col gap-2.5 p-4">
            <Skeleton className="h-2.5 w-16" />
            <Skeleton className="h-7 w-20" />
            <Skeleton className="h-3 w-12" />
          </div>
        ))}
      </div>

      {/* Volume chart */}
      <Card>
        <CardContent className="space-y-4 p-5">
          <div className="space-y-1.5">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-3 w-64" />
          </div>
          <Skeleton className="h-64 w-full rounded-lg" />
        </CardContent>
      </Card>

      {/* Recent activity + SMTP servers */}
      <div className="grid gap-6 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardContent className="p-0">
            <div className="flex items-center justify-between px-5 py-4">
              <div className="space-y-1.5">
                <Skeleton className="h-4 w-28" />
                <Skeleton className="h-3 w-44" />
              </div>
              <Skeleton className="h-7 w-24 rounded-md" />
            </div>
            <div className="space-y-px border-t border-border-strong">
              {Array.from({ length: 6 }).map((_, i) => (
                <div
                  key={i}
                  className="flex items-center gap-4 px-3 py-2.5"
                >
                  <Skeleton className="h-5 w-20 rounded-badge" />
                  <Skeleton className="h-3.5 w-40" />
                  <Skeleton className="h-3.5 flex-1" />
                  <Skeleton className="h-3.5 w-24" />
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardContent className="p-0">
            <div className="flex items-center justify-between px-5 py-4">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-7 w-16 rounded-md" />
            </div>
            <div className="border-t">
              {Array.from({ length: 3 }).map((_, i) => (
                <div
                  key={i}
                  className="flex items-center gap-3 border-b px-5 py-3 last:border-b-0"
                >
                  <Skeleton className="size-8 shrink-0 rounded-lg" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-3.5 w-28" />
                    <Skeleton className="h-3 w-36" />
                  </div>
                  <Skeleton className="size-4 shrink-0" />
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
