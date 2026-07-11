import * as React from "react"

import { cn } from "@workspace/ui/lib/utils"

/**
 * Inline validation message rendered beneath a form control.
 * Renders nothing when there is no message so it can be left mounted.
 * Wire the matching `id` to the control's `aria-describedby` and set
 * `aria-invalid` on the control so assistive tech announces the error.
 */
function FormMessage({
  className,
  children,
  ...props
}: React.ComponentProps<"p">) {
  if (!children) {
    return null
  }

  return (
    <p
      data-slot="form-message"
      role="alert"
      aria-live="polite"
      className={cn("text-caption text-destructive", className)}
      {...props}
    >
      {children}
    </p>
  )
}

export { FormMessage }
