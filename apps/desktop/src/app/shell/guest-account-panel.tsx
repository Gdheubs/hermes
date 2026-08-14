import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { StatusDot } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $instantAccount, claimInstantAccount } from '@/store/instant-account'

/**
 * The Guest chip's popover — the ENTIRE account surface until the user seeks
 * one. One sentence on what guest means, the honest facts (temporary, when it
 * expires, history is local and stays theirs), and one button. A popover, not
 * a page: claiming is a link, not a signup.
 */
export function GuestAccountPanel({ onClose }: { onClose: () => void }) {
  const { t } = useI18n()
  const copy = t.shell.guestAccount
  const account = useStore($instantAccount)
  const [claiming, setClaiming] = useState(false)

  const claim = async () => {
    setClaiming(true)

    try {
      await claimInstantAccount()
      onClose()
    } catch {
      // Store already toasted; keep the popover open for a retry.
      setClaiming(false)
    }
  }

  const credits = account.creditsUsd

  return (
    <div className="grid gap-0 text-sm">
      <div className="flex items-center gap-1.5 px-3 pb-1 pt-2.5 text-[0.7rem] font-medium leading-none">
        <StatusDot tone="good" />
        {copy.title}
      </div>

      <p className="px-3 py-1 text-xs leading-relaxed text-muted-foreground">{copy.description}</p>

      <div className="grid gap-1 px-3 py-1.5 text-[0.7rem] leading-none text-muted-foreground">
        {account.model ? (
          <div className="flex items-center justify-between gap-3">
            <span>{copy.modelLabel}</span>
            <span className="font-medium text-foreground/85">{account.model}</span>
          </div>
        ) : null}
        {credits !== null ? (
          <div className="flex items-center justify-between gap-3">
            <span>{copy.creditsLabel}</span>
            <span className={cn('font-medium', credits < 1 ? 'text-destructive' : 'text-foreground/85')}>
              {formatCredits(credits)}
            </span>
          </div>
        ) : null}
      </div>

      <p className="px-3 py-1 text-xs leading-relaxed text-muted-foreground">{copy.historyNote}</p>

      <div className="px-3 pb-2.5 pt-1.5">
        <Button className="w-full" disabled={claiming} onClick={() => void claim()} size="sm">
          {claiming ? copy.claiming : copy.claimAction}
        </Button>
      </div>
    </div>
  )
}

function formatCredits(usd: number): string {
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: usd === Math.trunc(usd) ? 0 : 2
  }).format(usd)
}
