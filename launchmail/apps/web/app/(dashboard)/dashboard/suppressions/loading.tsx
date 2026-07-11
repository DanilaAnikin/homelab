import { Skeleton } from "@workspace/ui/components/skeleton";
import { Card, CardContent } from "@workspace/ui/components/card";

// Layout-matched skeleton: PageHeader + add-row + DataTable card.
export default function Loading() {
  return (
    <div className="mx-auto max-w-6xl space-y-8">
      {/* PageHeader */}
      <div className="space-y-2">
        <Skeleton className="h-8 w-44" />
        <Skeleton className="h-4 w-96" />
      </div>

      <div className="space-y-4">
        {/* Add row */}
        <div className="flex gap-2">
          <Skeleton className="h-9 flex-1 rounded-md" />
          <Skeleton className="h-9 w-24 rounded-md" />
        </div>

        {/* Table card */}
        <Card>
          <CardContent className="p-0">
            <div className="flex items-center gap-4 border-b border-border-strong px-3 py-2">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-3 w-16" />
              <Skeleton className="ml-auto h-3 w-12" />
            </div>
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                key={i}
                className="flex items-center gap-4 border-b border-border px-3 py-3 last:border-b-0"
              >
                <Skeleton className="h-4 w-48" />
                <Skeleton className="h-5 w-20 rounded-badge" />
                <Skeleton className="ml-auto h-4 w-16" />
                <Skeleton className="size-7 rounded-md" />
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
