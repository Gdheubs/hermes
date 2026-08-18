/**
 * PREVIEW TOOLBAR — the URL bar for the in-app browser preview.
 *
 * The live preview tile (`preview.tsx`) mounts `PreviewPane` with `embedded`.
 * After the layout-tree rewrite the tab strip owns the title, so the pane
 * itself has no address chrome — this toolbar is the navigation surface.
 *
 * Back / forward / address / submit / reload. HTTP-only validation so the
 * address bar can't open `file://` or `data:` in the `<webview>`.
 *
 * Controlled component. State lives in `PreviewPane` so the webview's event
 * listeners stay close to the webview itself; this component just renders
 * the chrome and forwards intent. Focus-aware address input: when the input
 * is focused, `onAddressChange` is the user's keystrokes (not the webview's
 * navigation events), so typing doesn't get clobbered by `pushState`.
 */

import { type FormEvent, useCallback } from 'react'

import { TooltipIconButton } from '@/components/assistant-ui/tooltip-icon-button'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import { ArrowUpRight, ChevronLeft, ChevronRight, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'

export interface PreviewToolbarProps {
  /** The address to show in the input. */
  address: string
  /** True when the address parses as `http://` or `https://`. */
  addressValid: boolean
  /** True when the webview has at least one entry to go back to. */
  canGoBack: boolean
  /** True when the webview has at least one entry to go forward to. */
  canGoForward: boolean
  /** True while a navigation is in flight (drives the reload icon spin). */
  loading: boolean
  /** Placeholder shown in the address input when empty. */
  placeholder: string

  onAddressBlur: () => void
  onAddressChange: (next: string) => void
  onAddressFocus: () => void
  onBack: () => void
  onForward: () => void
  onReload: () => void
  onSubmit: (url: string) => void
}

/**
 * Returns `true` if `raw` parses as an absolute HTTP/HTTPS URL. Rejects
 * `file://`, `data:`, `javascript:`, and other schemes — the embedded
 * `<webview>` should only render web content, never local files the
 * renderer hasn't already approved.
 */
export function isHttpUrl(raw: string): boolean {
  const trimmed = raw.trim()

  if (!trimmed) {
    return false
  }

  try {
    const url = new URL(trimmed)

    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

export function PreviewToolbar({
  address,
  addressValid,
  canGoBack,
  canGoForward,
  loading,
  placeholder,
  onAddressBlur,
  onAddressChange,
  onAddressFocus,
  onBack,
  onForward,
  onReload,
  onSubmit
}: PreviewToolbarProps) {
  const { t } = useI18n()
  const copy = t.preview.web

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault()

      if (!addressValid) {
        return
      }

      onSubmit(address.trim())
    },
    [address, addressValid, onSubmit]
  )

  return (
    <form
      aria-label={copy.navigate}
      className="flex shrink-0 items-center gap-0.5 border-b border-border/60 bg-background px-1 py-1"
      onSubmit={handleSubmit}
    >
      <TooltipIconButton disabled={!canGoBack} onClick={onBack} tooltip={copy.goBack} type="button">
        <ChevronLeft />
      </TooltipIconButton>
      <TooltipIconButton disabled={!canGoForward} onClick={onForward} tooltip={copy.goForward} type="button">
        <ChevronRight />
      </TooltipIconButton>
      <Input
        aria-invalid={address.trim().length > 0 && !addressValid ? true : undefined}
        aria-label={copy.address}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        className={cn(loading && 'text-muted-foreground')}
        inputMode="url"
        onBlur={onAddressBlur}
        onChange={event => onAddressChange(event.target.value)}
        onFocus={onAddressFocus}
        placeholder={placeholder}
        size="xs"
        spellCheck={false}
        value={address}
      />
      <TooltipIconButton disabled={!addressValid} tooltip={copy.go} type="submit">
        <ArrowUpRight />
      </TooltipIconButton>
      <TooltipIconButton onClick={onReload} tooltip={copy.reload} type="button">
        <RefreshCw className={cn(loading && 'animate-spin')} />
      </TooltipIconButton>
    </form>
  )
}
