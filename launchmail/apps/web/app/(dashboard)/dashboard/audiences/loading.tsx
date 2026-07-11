import { Skeleton } from "@workspace/ui/components/skeleton";
import { Card, CardContent } from "@workspace/ui/components/card";

// Layout-matched skeleton: PageHeader + linked-list card (max-w-4xl).
export default function Loading() {
  return (
    <div className="mx-auto max-w-4xl space-y-8">
      {/* PageHeader */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-2">
          <Skeleton className="h-8 w-36" />
          <Skeleton className="h-4 w-80" />
        </div>
        <Skeleton className="h-8 w-32 rounded-md" />
      </div>

      {/* Audience list */}
      <Card>
        <CardContent className="p-0">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center justify-between gap-3 border-b px-5 py-4 last:border-b-0"
            >
              <div className="flex items-center gap-3">
                <Skeleton className="size-9 rounded-lg" />
                <div className="space-y-1.5">
                  <Skeleton className="h-4 w-32" />
                  <Skeleton className="h-3 w-20" />
                </div>
              </div>
              <Skeleton className="size-4" />
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
