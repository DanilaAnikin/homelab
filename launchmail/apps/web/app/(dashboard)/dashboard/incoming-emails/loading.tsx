import { Skeleton } from "@workspace/ui/components/skeleton";
import { Card, CardContent } from "@workspace/ui/components/card";

// Layout-matched skeleton: PageHeader + mailbox picker + message list.
export default function Loading() {
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-8 w-52" />
        <Skeleton className="h-4 w-96" />
      </div>

      <div className="flex items-center justify-between gap-3">
        <Skeleton className="h-9 w-[min(420px,100%)] rounded-md" />
        <Skeleton className="h-9 w-28 rounded-md" />
      </div>

      <Card>
        <CardContent className="p-0">
          {Array.from({ length: 7 }).map((_, i) => (
            <div
              key={i}
              className="flex items-start gap-3 border-b border-border px-4 py-3 last:border-b-0"
            >
              <Skeleton className="mt-1 size-2 rounded-full" />
              <div className="flex-1 space-y-1.5">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 w-64" />
                <Skeleton className="h-3 w-80" />
              </div>
              <Skeleton className="h-3 w-10" />
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
