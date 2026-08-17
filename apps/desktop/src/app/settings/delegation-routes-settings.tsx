import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { ModelCatalogMenu, ModelMenuCloseContext, type ModelMenuController } from '@/app/shell/model-catalog-menu'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { saveDelegationRoutes } from '@/hermes'
import { useI18n } from '@/i18n'
import { Network } from '@/lib/icons'
import { reasoningEffortLabel } from '@/lib/reasoning-effort'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import type { DelegationRouteConfig, DelegationRoutesConfig, HermesConfigRecord } from '@/types/hermes'

import { hermesConfigCacheWriter, useHermesConfigRecord } from '../hooks/use-config-record'
import { useOnProfileSwitch } from '../hooks/use-on-profile-switch'

import { ListRow, SectionHeading } from './primitives'

interface RouteChoice {
  effort: string
  model: string
  provider: string
}

interface RouteDraft extends RouteChoice {
  alias: string
  originalAlias: null | string
}

const EMPTY_CHOICE: RouteChoice = { effort: '', model: '', provider: '' }
const ALIAS_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/

export function delegationRoutesFromConfig(config: HermesConfigRecord | null | undefined): DelegationRoutesConfig {
  const delegation = config?.delegation

  if (!delegation || typeof delegation !== 'object' || Array.isArray(delegation)) {
    return {}
  }

  const routes = (delegation as Record<string, unknown>).routes

  if (!routes || typeof routes !== 'object' || Array.isArray(routes)) {
    return {}
  }

  const parsed: DelegationRoutesConfig = {}

  for (const [alias, raw] of Object.entries(routes)) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      continue
    }

    const route = raw as Record<string, unknown>
    const provider = String(route.provider ?? '').trim()
    const model = String(route.model ?? '').trim()

    if (!provider || !model) {
      continue
    }

    const effort = String(route.reasoning_effort ?? '').trim()
    const apiMode = String(route.api_mode ?? '').trim()
    const baseUrl = String(route.base_url ?? '').trim()
    const maxOutputTokens = route.max_output_tokens
    const requestOverrides = route.request_overrides

    parsed[alias] = {
      provider,
      model,
      ...(effort ? { reasoning_effort: effort } : {}),
      ...(apiMode ? { api_mode: apiMode } : {}),
      ...(baseUrl ? { base_url: baseUrl } : {}),
      ...(typeof maxOutputTokens === 'number' ? { max_output_tokens: maxOutputTokens } : {}),
      ...(requestOverrides && typeof requestOverrides === 'object' && !Array.isArray(requestOverrides)
        ? { request_overrides: requestOverrides as Record<string, unknown> }
        : {})
    }
  }

  return parsed
}

function routeChoice(route?: DelegationRouteConfig): RouteChoice {
  if (!route) {
    return EMPTY_CHOICE
  }

  return {
    effort: route.reasoning_effort ?? '',
    model: route.model,
    provider: route.provider
  }
}

function routeLabel(value: RouteChoice, empty: string): string {
  if (!value.model.trim()) {
    return empty
  }

  const base = value.provider.trim() ? `${value.provider}: ${value.model}` : value.model
  const effort = value.effort.trim() ? reasoningEffortLabel(value.effort) : ''

  return effort ? `${base} · ${effort}` : base
}

function DetachedRouteModelField({ onChange, value }: { onChange: (next: RouteChoice) => void; value: RouteChoice }) {
  const { t } = useI18n()
  const copy = t.settings.model.delegationRoutes
  const [open, setOpen] = useState(false)

  const controller: ModelMenuController = {
    applyPreset: (preset, row) =>
      onChange({
        effort: preset.effort ?? '',
        model: row.model,
        provider: row.provider
      }),
    current: { effort: value.effort, fast: false, model: value.model, provider: value.provider },
    presetFor: () => ({}),
    select: (model, provider) => onChange({ ...value, model, provider }),
    setOptions: (patch, row) => {
      if (patch.effort !== undefined) {
        onChange({ effort: patch.effort, model: row.model, provider: row.provider })
      }
    }
  }

  return (
    <DropdownMenu onOpenChange={setOpen} open={open}>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={routeLabel(value, copy.selectModel)}
          className={cn('w-full justify-between gap-2 font-normal', !value.model && 'text-(--ui-text-tertiary)')}
          size="sm"
          type="button"
          variant="outline"
        >
          <span className="min-w-0 truncate">{routeLabel(value, copy.selectModel)}</span>
          <Codicon className="shrink-0 opacity-50" name="chevron-down" size="0.7rem" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72 p-0">
        <ModelMenuCloseContext.Provider value={() => setOpen(false)}>
          <ModelCatalogMenu allowFast={false} controller={controller} />
        </ModelMenuCloseContext.Provider>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function DelegationRoutesSettings() {
  const { t } = useI18n()
  const copy = t.settings.model.delegationRoutes
  const profile = normalizeProfileKey(useStore($activeGatewayProfile))
  const { data: config, isPending } = useHermesConfigRecord(profile)
  const setConfigCache = hermesConfigCacheWriter(profile)
  const configuredRoutes = useMemo(() => delegationRoutesFromConfig(config), [config])
  const [routes, setRoutes] = useState<DelegationRoutesConfig>(configuredRoutes)
  const [draft, setDraft] = useState<null | RouteDraft>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const profileEpoch = useRef(0)

  useEffect(() => setRoutes(configuredRoutes), [configuredRoutes])
  useOnProfileSwitch(() => {
    profileEpoch.current += 1
    setRoutes({})
    setDraft(null)
    setError('')
    setSaving(false)
  })

  const persist = async (nextRoutes: DelegationRoutesConfig) => {
    const epoch = profileEpoch.current
    const targetProfile = profile
    const previous = routes
    setRoutes(nextRoutes)
    setError('')
    setSaving(true)

    const delegation = config?.delegation

    const previousDelegation =
      delegation && typeof delegation === 'object' && !Array.isArray(delegation)
        ? (delegation as Record<string, unknown>)
        : {}

    const nextConfig = { ...config, delegation: { ...previousDelegation, routes: nextRoutes } }

    setConfigCache(nextConfig)

    try {
      await saveDelegationRoutes(nextRoutes, targetProfile)
    } catch (err) {
      if (profileEpoch.current === epoch) {
        setRoutes(previous)
        setConfigCache({ ...config, delegation: { ...previousDelegation, routes: previous } })
        notifyError(err, copy.saveFailed)
      }

      throw err
    } finally {
      if (profileEpoch.current === epoch) {
        setSaving(false)
      }
    }
  }

  const beginAdd = () => {
    setError('')
    setDraft({ alias: '', originalAlias: null, ...EMPTY_CHOICE })
  }

  const beginEdit = (alias: string) => {
    setError('')
    setDraft({ alias, originalAlias: alias, ...routeChoice(routes[alias]) })
  }

  const saveDraft = async () => {
    if (!draft) {
      return
    }

    const alias = draft.alias.trim()

    if (!ALIAS_PATTERN.test(alias)) {
      setError(copy.aliasError)

      return
    }

    if (!draft.provider.trim() || !draft.model.trim()) {
      setError(copy.modelError)

      return
    }

    if (alias !== draft.originalAlias && routes[alias]) {
      setError(copy.duplicateError)

      return
    }

    const next = { ...routes }

    if (draft.originalAlias && draft.originalAlias !== alias) {
      delete next[draft.originalAlias]
    }

    const previousRoute = draft.originalAlias ? routes[draft.originalAlias] : undefined
    const preserved = { ...(previousRoute ?? {}) }

    delete preserved.reasoning_effort
    next[alias] = {
      ...preserved,
      provider: draft.provider.trim(),
      model: draft.model.trim(),
      ...(draft.effort.trim() ? { reasoning_effort: draft.effort.trim() } : {})
    }

    try {
      await persist(next)
      setDraft(null)
    } catch {
      // persist owns user-visible notification + rollback.
    }
  }

  const remove = async (alias: string) => {
    const next = { ...routes }

    delete next[alias]

    try {
      await persist(next)

      if (draft?.originalAlias === alias) {
        setDraft(null)
      }
    } catch {
      // persist owns user-visible notification + rollback.
    }
  }

  return (
    <section>
      <div className="mb-2.5 flex items-center justify-between">
        <SectionHeading icon={Network} title={copy.title} />
        <Button disabled={isPending || !config || saving || draft !== null} onClick={beginAdd} size="sm" variant="textStrong">
          {copy.add}
        </Button>
      </div>
      <p className="mb-2 text-xs text-muted-foreground">{copy.description}</p>

      <div className="grid gap-1">
        {Object.entries(routes).map(([alias, route]) => (
          <ListRow
            action={
              <div className="flex shrink-0 items-center gap-1.5">
                <Button disabled={isPending || !config || saving} onClick={() => beginEdit(alias)} size="sm" variant="textStrong">
                  {copy.change}
                </Button>
                <Button
                  aria-label={copy.removeAria(alias)}
                  disabled={isPending || !config || saving}
                  onClick={() => void remove(alias)}
                  size="sm"
                  variant="text"
                >
                  {copy.remove}
                </Button>
              </div>
            }
            description={routeLabel(routeChoice(route), copy.selectModel)}
            key={alias}
            title={alias}
          />
        ))}
      </div>

      {Object.keys(routes).length === 0 && !draft ? (
        <div className="py-3 text-xs text-(--ui-text-tertiary)">{copy.empty}</div>
      ) : null}

      {draft ? (
        <div className="grid gap-2 py-3 @2xl:grid-cols-[minmax(10rem,0.6fr)_minmax(15rem,1.4fr)_auto] @2xl:items-center">
          <Input
            aria-label={copy.aliasLabel}
            onChange={event => setDraft(current => (current ? { ...current, alias: event.target.value } : current))}
            placeholder={copy.aliasPlaceholder}
            value={draft.alias}
          />
          <DetachedRouteModelField
            onChange={choice => setDraft(current => (current ? { ...current, ...choice } : current))}
            value={draft}
          />
          <div className="flex items-center gap-1.5">
            <Button disabled={saving} onClick={() => void saveDraft()} size="sm">
              {copy.save}
            </Button>
            <Button disabled={saving} onClick={() => setDraft(null)} size="sm" variant="text">
              {t.common.cancel}
            </Button>
          </div>
        </div>
      ) : null}

      {error ? <div className="mt-1 text-xs text-destructive">{error}</div> : null}
    </section>
  )
}
