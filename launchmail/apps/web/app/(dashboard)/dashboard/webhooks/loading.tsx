import { Skeleton } from "@workspace/ui/components/skeleton";
import { Card, CardContent } from "@workspace/ui/components/card";

// Layout-matched skeleton: PageHeader + create-endpoint card + endpoints table.
export default function Loading() {
  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* PageHeader */}
      <div className="space-y-2">
        <Skeleton className="h-8 w-36" />
        <Skeleton className="h-4 w-80" />
      </div>

      <div className="space-y-4">
        {/* Create-endpoint card */}
        <Card>
          <CardContent className="space-y-4 p-5">
            <div className="space-y-2">
              <Skeleton className="h-3.5 w-24" />
              <Skeleton className="h-9 w-full rounded-md" />
            </div>
            <div className="flex flex-wrap gap-2">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-6 w-28 rounded-badge" />
              ))}
            </div>
            <Skeleton className="h-9 w-32 rounded-md" />
          </CardContent>
        </Card>

        {/* Endpoints table */}
        <Card>
          <CardContent className="p-0">
            <div className="flex items-center gap-4 border-b border-border-strong px-3 py-2">
              <Skeleton className="h-3 w-24" />
              <Skeleton className="ml-auto h-3 w-16" />
            </div>
            {Array.from({ length: 3 }).map((_, i) => (
              <div
                key={i}
                className="flex items-center gap-4 border-b border-border px-3 py-3 last:border-b-0"
              >
                <Skeleton className="h-4 w-56" />
                <Skeleton className="ml-auto h-5 w-20 rounded-badge" />
                <Skeleton className="size-7 rounded-md" />
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
