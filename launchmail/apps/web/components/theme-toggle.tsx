"use client"

import { useSyncExternalStore } from "react"
import { Moon, Sun } from "lucide-react"
import { useTheme } from "next-themes"

import { Button } from "@workspace/ui/components/button"

// True once mounted on the client, without a setState-in-effect: the server
// snapshot is always false, the client snapshot always true, so React flips it
// during hydration without us writing state.
const subscribeNoop = () => () => {}
const getMountedClient = () => true
const getMountedServer = () => false

export function ThemeToggle() {
  const { setTheme, resolvedTheme } = useTheme()
  const mounted = useSyncExternalStore(
    subscribeNoop,
    getMountedClient,
    getMountedServer,
  )

  const isDark = resolvedTheme === "dark"
  const label = mounted
    ? isDark
      ? "Switch to light mode"
      : "Switch to dark mode"
    : "Toggle theme"

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      className="size-8 text-muted-foreground hover:text-foreground"
      aria-label={label}
      title={label}
    >
      <Sun className="size-4 dark:hidden" />
      <Moon className="hidden size-4 dark:block" />
    </Button>
  )
}
