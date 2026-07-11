import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { Slot } from "radix-ui"

import { cn } from "@workspace/ui/lib/utils"

const badgeVariants = cva(
  "group/badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-4xl border border-transparent px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&>svg]:pointer-events-none [&>svg]:size-3!",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground [a]:hover:bg-primary/80",
        secondary:
          "bg-secondary text-secondary-foreground [a]:hover:bg-secondary/80",
        destructive:
          "bg-destructive/10 text-destructive focus-visible:ring-destructive/20 dark:bg-destructive/20 dark:focus-visible:ring-destructive/40 [a]:hover:bg-destructive/20",
        outline:
          "border-border text-foreground [a]:hover:bg-muted [a]:hover:text-muted-foreground",
        ghost:
          "hover:bg-muted hover:text-muted-foreground dark:hover:bg-muted/50",
        link: "text-primary underline-offset-4 hover:underline",
        // Tonal status pills — AA-compliant *-tint background + *-on text,
        // with a leading status dot. The single pill system StatusBadge maps to.
        success:
          "rounded-badge bg-success-tint text-success-on before:size-1.5 before:shrink-0 before:rounded-full before:bg-success-on",
        warning:
          "rounded-badge bg-warning-tint text-warning-on before:size-1.5 before:shrink-0 before:rounded-full before:bg-warning-on",
        info: "rounded-badge bg-info-tint text-info-on before:size-1.5 before:shrink-0 before:rounded-full before:bg-info-on",
        "destructive-tonal":
          "rounded-badge bg-destructive-tint text-destructive-on before:size-1.5 before:shrink-0 before:rounded-full before:bg-destructive-on",
        pending:
          "rounded-badge bg-pending-tint text-pending-on before:size-1.5 before:shrink-0 before:rounded-full before:bg-pending-on",
        brand: "rounded-badge bg-brand-100 text-brand-800",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Badge({
  className,
  variant = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "span"

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants }
