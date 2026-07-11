"use client"

import { useTheme } from "next-themes"
import { Toaster as Sonner, type ToasterProps } from "sonner"
import { CircleCheckIcon, InfoIcon, TriangleAlertIcon, OctagonXIcon, Loader2Icon } from "lucide-react"

// Spring enter that matches the badge/dialog motion language. Scoped to our
// `cn-toast` class so it only retargets the easing — Sonner still owns timing.
const TOAST_MOTION = `.cn-toast[data-sonner-toast]{transition-timing-function:var(--ease-spring)!important}`

const Toaster = ({ ...props }: ToasterProps) => {
  const { theme = "system" } = useTheme()

  return (
    <>
      <style>{TOAST_MOTION}</style>
      <Sonner
        theme={theme as ToasterProps["theme"]}
        className="toaster group"
        richColors
        icons={{
          success: (
            <CircleCheckIcon className="size-4" />
          ),
          info: (
            <InfoIcon className="size-4" />
          ),
          warning: (
            <TriangleAlertIcon className="size-4" />
          ),
          error: (
            <OctagonXIcon className="size-4" />
          ),
          loading: (
            <Loader2Icon className="size-4 animate-spin" />
          ),
        }}
        style={
          {
            "--normal-bg": "var(--popover)",
            "--normal-text": "var(--popover-foreground)",
            "--normal-border": "var(--border)",
            "--border-radius": "var(--radius)",
            // Semantic toasts share the badge palette: *-tint surface, *-on text.
            "--success-bg": "var(--success-tint)",
            "--success-text": "var(--success-on)",
            "--success-border": "var(--success-tint)",
            "--error-bg": "var(--destructive-tint)",
            "--error-text": "var(--destructive-on)",
            "--error-border": "var(--destructive-tint)",
            "--warning-bg": "var(--warning-tint)",
            "--warning-text": "var(--warning-on)",
            "--warning-border": "var(--warning-tint)",
            "--info-bg": "var(--info-tint)",
            "--info-text": "var(--info-on)",
            "--info-border": "var(--info-tint)",
          } as React.CSSProperties
        }
        toastOptions={{
          classNames: {
            toast: "cn-toast",
          },
        }}
        {...props}
      />
    </>
  )
}

export { Toaster }
export { toast } from "sonner"
