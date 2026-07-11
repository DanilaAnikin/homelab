import { Skeleton } from "@workspace/ui/components/skeleton";
import { Card, CardContent } from "@workspace/ui/components/card";

// Layout-matched skeleton: back-link + PageHeader + toolbar + contacts table.
export default function Loading() {
  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <div className="space-y-3">
        <Skeleton className="h-4 w-24" />
        <div className="space-y-2">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-56" />
        </div>
      </div>

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <Skeleton className="h-9 flex-1 rounded-md" />
        <Skeleton className="h-9 w-28 rounded-md" />
        <Skeleton className="h-9 w-32 rounded-md" />
      </div>

      {/* Contacts table */}
      <Card>
        <CardContent className="p-0">
          <div className="flex items-center gap-4 border-b border-border-strong px-3 py-2">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="ml-auto h-3 w-16" />
          </div>
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center gap-4 border-b border-border px-3 py-3 last:border-b-0"
            >
              <Skeleton className="h-4 w-48" />
              <Skeleton className="ml-auto h-5 w-20 rounded-badge" />
              <Skeleton className="size-7 rounded-md" />
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
