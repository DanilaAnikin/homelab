import { cn } from "@workspace/ui/lib/utils"

const SHIMMER_KEYFRAMES = `@keyframes cn-skeleton-shimmer{100%{transform:translateX(100%)}}`

const shapePresets: Record<"line" | "avatar" | "card", string> = {
  line: "h-4 w-full rounded-md",
  avatar: "size-9 rounded-full",
  card: "h-32 w-full rounded-lg",
}

function Skeleton({
  className,
  shape,
  ...props
}: React.ComponentProps<"div"> & {
  shape?: "line" | "avatar" | "card"
}) {
  return (
    <div
      data-slot="skeleton"
      data-shape={shape}
      className={cn(
        // tinted base + clipped moving shimmer (no layout/opacity pulse)
        "relative isolate overflow-hidden rounded-md bg-muted",
        "after:absolute after:inset-0 after:-translate-x-full after:bg-gradient-to-r after:from-transparent after:via-foreground/[0.06] after:to-transparent after:[animation:cn-skeleton-shimmer_1.6s_var(--ease-standard)_infinite]",
        shape && shapePresets[shape],
        className
      )}
      {...props}
    >
      <style>{SHIMMER_KEYFRAMES}</style>
    </div>
  )
}

export { Skeleton }
