"use client"

import { useSyncExternalStore } from "react"
import { useRouter } from "next/navigation"
import { useTheme } from "next-themes"
import { authClient, clearToken } from "@/lib/auth-client"
import { Button } from "@workspace/ui/components/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@workspace/ui/components/dropdown-menu"
import { Avatar, AvatarFallback } from "@workspace/ui/components/avatar"
import { ChevronsUpDownIcon, LogOutIcon, MoonIcon, SunIcon } from "lucide-react"

// Client-mounted flag without a setState-in-effect (server snapshot false,
// client snapshot true → React flips it during hydration).
const subscribeNoop = () => () => {}
const getMountedClient = () => true
const getMountedServer = () => false

export function AccountDropdown() {
  const router = useRouter()
  const { data: session } = authClient.useSession()
  const { setTheme, resolvedTheme } = useTheme()
  const mounted = useSyncExternalStore(
    subscribeNoop,
    getMountedClient,
    getMountedServer,
  )
  const isDark = resolvedTheme === "dark"

  async function handleSignOut() {
    await authClient.signOut({
      fetchOptions: {
        onSuccess: () => {
          clearToken()
          router.push("/login")
        },
      },
    })
  }

  if (!session) return null

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-2 px-1.5 font-normal text-muted-foreground hover:text-foreground hover:bg-muted"
        >
          <Avatar className="size-6">
            <AvatarFallback className="bg-brand/10 text-brand text-xs font-medium">
              {session.user.name.charAt(0).toUpperCase()}
            </AvatarFallback>
          </Avatar>
          <span className="hidden max-w-[10rem] truncate text-body-strong text-foreground sm:inline">
            {session.user.name}
          </span>
          <ChevronsUpDownIcon className="size-3.5 shrink-0 opacity-60" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel>
          <div className="flex flex-col gap-0.5">
            <span className="truncate font-medium">{session.user.name}</span>
            <span className="truncate text-xs font-normal text-muted-foreground">
              {session.user.email}
            </span>
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={(e) => {
            e.preventDefault()
            setTheme(isDark ? "light" : "dark")
          }}
        >
          <SunIcon className="mr-2 size-4 dark:hidden" />
          <MoonIcon className="mr-2 hidden size-4 dark:block" />
          {mounted ? (isDark ? "Light mode" : "Dark mode") : "Toggle theme"}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={handleSignOut}
          className="text-destructive focus:text-destructive"
        >
          <LogOutIcon className="mr-2 size-4" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
