import { Skeleton } from "@workspace/ui/components/skeleton";
import {
  Card,
  CardContent,
  CardHeader,
} from "@workspace/ui/components/card";

// Layout-matched skeleton: PageHeader + members card + settings card + activity log.
export default function Loading() {
  return (
    <div className="mx-auto max-w-3xl space-y-8">
      {/* PageHeader with role badge */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-2">
          <Skeleton className="h-8 w-44" />
          <Skeleton className="h-4 w-80" />
        </div>
        <Skeleton className="h-5 w-16 rounded-badge" />
      </div>

      {/* Members & invitations */}
      <Card>
        <CardHeader className="space-y-2">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-3.5 w-full max-w-md" />
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <Skeleton className="h-9 flex-1 rounded-md" />
            <Skeleton className="h-9 w-28 rounded-md" />
          </div>
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="flex items-center gap-3">
              <Skeleton shape="avatar" />
              <div className="flex-1 space-y-1.5">
                <Skeleton className="h-3.5 w-32" />
                <Skeleton className="h-3 w-40" />
              </div>
              <Skeleton className="h-7 w-24 rounded-md" />
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Org settings */}
      <Card>
        <CardHeader className="space-y-2">
          <Skeleton className="h-5 w-36" />
          <Skeleton className="h-3.5 w-72" />
        </CardHeader>
        <CardContent className="space-y-4">
          <Skeleton className="h-9 w-full rounded-md" />
          <Skeleton className="h-9 w-full rounded-md" />
          <Skeleton className="h-9 w-28 rounded-md" />
        </CardContent>
      </Card>

      {/* Activity log */}
      <Card>
        <CardHeader className="space-y-2">
          <Skeleton className="h-5 w-28" />
          <Skeleton className="h-3.5 w-56" />
        </CardHeader>
        <CardContent className="px-3 pb-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center gap-4 border-b border-border px-3 py-2.5 last:border-b-0"
            >
              <Skeleton className="h-3.5 flex-1" />
              <Skeleton className="h-3.5 w-16" />
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
