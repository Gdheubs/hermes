import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import {
  activateLocalModel,
  deleteLocalModel,
  downloadLocalModel,
  ejectLocalModel,
  getLocalCatalog,
  getLocalHardware,
  getLocalModelsStatus,
  installLocalRuntime,
  setLocalServer
} from '@/hermes'
import { useI18n } from '@/i18n'
import { Check, CheckCircle2, Cpu, Download, Eject, Loader2, Monitor, Package, StopFilled, Trash2, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import {
  $localRuntimeJobs,
  runningDownloadFor,
  runningRuntimeInstall,
  watchLocalRuntimeJobs
} from '@/store/local-runtime-jobs'
import { notify, notifyError } from '@/store/notifications'
import type { LocalCatalogModel, LocalHardware, LocalModelsStatus } from '@/types/hermes'

import { ListRow, Pill, SettingsContent, SettingsSection, SettingsSkeleton } from './primitives'

function ProgressBar({ percent }: { percent: number | undefined }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-(--ui-bg-tertiary)">
      <div
        className="h-full rounded-full bg-primary transition-[width] duration-300"
        style={{ width: `${Math.max(2, Math.min(100, percent ?? 2))}%` }}
      />
    </div>
  )
}

function gbLabel(bytes: number | null | undefined): string {
  if (!bytes) {
    return '—'
  }

  return `${(bytes / (1 << 30)).toFixed(1)} GB`
}

export function LocalModelsSettings() {
  const { t } = useI18n()
  const copy = t.settings.localModels
  const [status, setStatus] = useState<LocalModelsStatus | null>(null)
  const [hardware, setHardware] = useState<LocalHardware | null>(null)
  const [catalog, setCatalog] = useState<LocalCatalogModel[] | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [serverBusy, setServerBusy] = useState(false)
  // Jobs live in the app-level store (they must survive this pane
  // unmounting); the pane just renders the slice it cares about.
  const jobs = useStore($localRuntimeJobs)

  const refresh = useCallback(() => {
    void getLocalModelsStatus()
      .then(setStatus)
      .catch(() => setStatus(null))
    void getLocalCatalog()
      .then(data => setCatalog(data.models))
      .catch(() => setCatalog([]))
  }, [])

  // Snappy first paint: status + catalog immediately; hardware (may shell out
  // to nvidia-smi) backfills and pops in-place. The job watcher also kicks
  // here so reopening the pane rediscovers work started before.
  useEffect(() => {
    refresh()
    watchLocalRuntimeJobs()
    void getLocalHardware()
      .then(setHardware)
      .catch(() => setHardware(null))
  }, [refresh])

  // The pane is LIVE while visible: residency changes without user action
  // (boot warm finishing, idle sweep unloading, another surface ejecting),
  // and a stale snapshot here reads as a broken feature — 'VRAM full but
  // the pane says Not in memory'. The status route is built cheap for
  // polling; setTimeout chain, never overlapping.
  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const next = await getLocalModelsStatus()

        if (!cancelled) {
          setStatus(next)
        }
      } catch {
        // Backend briefly unreachable — keep the last snapshot.
      }

      if (!cancelled) {
        timer = window.setTimeout(() => void tick(), 4_000)
      }
    }

    timer = window.setTimeout(() => void tick(), 4_000)

    return () => {
      cancelled = true

      if (timer !== undefined) {
        window.clearTimeout(timer)
      }
    }
  }, [])

  // A job finishing (download done, install done) changes what status/catalog
  // should show — refresh whenever the running set shrinks.
  const runningCount = jobs.filter(j => j.status === 'running').length
  useEffect(() => {
    refresh()
  }, [refresh, runningCount])

  async function handleInstallRuntime() {
    try {
      await installLocalRuntime()
      watchLocalRuntimeJobs()
    } catch (err) {
      notifyError(err, copy.installFailed)
    }
  }

  async function handleDownload(model: LocalCatalogModel) {
    try {
      const res = await downloadLocalModel(model.id)

      if (res.already_downloaded || !res.job_id) {
        refresh()

        return
      }

      watchLocalRuntimeJobs()
    } catch (err) {
      notifyError(err, copy.downloadFailed(model.display_name))
    }
  }

  async function handleActivate(model: LocalCatalogModel) {
    const target = model.downloaded_model_id ?? model.model_id

    if (!target) {
      return
    }

    try {
      await activateLocalModel(target)
      watchLocalRuntimeJobs()
    } catch (err) {
      notifyError(err, copy.activateFailed(model.display_name))
    }
  }

  async function handleEject(modelId: string) {
    try {
      await ejectLocalModel(modelId)
      notify({ durationMs: 3_000, kind: 'success', message: copy.ejected, title: copy.title })
      refresh()
    } catch (err) {
      notifyError(err, copy.ejectFailed)
    }
  }

  async function handleServer(action: 'start' | 'stop') {
    setServerBusy(true)

    try {
      await setLocalServer(action)
      notify({
        durationMs: 3_500,
        kind: 'success',
        message: action === 'stop' ? copy.serverStopped : copy.serverStarted,
        title: copy.title
      })
      refresh()
    } catch (err) {
      notifyError(err, action === 'stop' ? copy.serverStopFailed : copy.serverStartFailed)
    } finally {
      setServerBusy(false)
    }
  }

  async function handleDelete(model: LocalCatalogModel) {
    const target = model.downloaded_model_id ?? model.id

    if (!window.confirm(copy.deleteConfirm(target))) {
      return
    }

    setDeleting(model.id)

    try {
      await deleteLocalModel(target)
      notify({ durationMs: 2_500, kind: 'success', message: copy.deleted(target), title: copy.title })
      refresh()
    } catch (err) {
      notifyError(err, copy.deleteFailed)
    } finally {
      setDeleting(null)
    }
  }

  if (!status || catalog === null) {
    return <SettingsSkeleton sections={[{ rows: 2 }, { rows: 4 }]} />
  }

  const rJob = runningRuntimeInstall(jobs)
  const lastError = jobs.find(j => j.status === 'error')

  // Up to date = the authority (status) says the configured tag is what's
  // serving. Shown whenever true — not only right after an update.
  const updateApplied =
    status.runtime_installed && !status.update_available && status.tag === status.configured_tag

  return (
    <SettingsContent>
      {/* ── Runtime ── */}
      <SettingsSection
        aside={
          status.runtime_installed ? (
            <Pill tone="primary">
              {status.server_running ? copy.serverRunning : copy.runtimeReady(status.runtime_backend ?? '')}
            </Pill>
          ) : undefined
        }
        icon={Zap}
        meta={status.tag}
        title={copy.runtimeTitle}
      >
        {status.runtime_installed ? (
          <ListRow
            action={
              status.server_running ? (
                <Button
                  className={cn(serverBusy && '[&_svg]:animate-spin')}
                  disabled={serverBusy}
                  onClick={() => void handleServer('stop')}
                  size="sm"
                  variant="outline"
                >
                  {serverBusy ? <Loader2 /> : <StopFilled />}
                  {copy.stopServer}
                </Button>
              ) : (
                <Button
                  className={cn(serverBusy && '[&_svg]:animate-spin')}
                  disabled={serverBusy}
                  onClick={() => void handleServer('start')}
                  size="sm"
                  variant="outline"
                >
                  {serverBusy ? <Loader2 /> : <Zap />}
                  {copy.startServer}
                </Button>
              )
            }
            description={
              status.server_running
                ? copy.runtimeRunningDetail
                : copy.runtimeInstalledDetail(status.tag, status.runtime_backend ?? 'cpu')
            }
            title={copy.runtimeInstalled}
          />
        ) : rJob ? (
          <ListRow
            below={<ProgressBar percent={rJob.percent} />}
            description={rJob.detail || copy.installing}
            title={
              <span className="inline-flex items-center gap-2">
                <Loader2 className="size-3.5 animate-spin" />
                {copy.installing}
              </span>
            }
          />
        ) : (
          <ListRow
            action={
              <Button onClick={() => void handleInstallRuntime()} size="sm">
                <Download />
                {copy.installAction}
              </Button>
            }
            description={copy.installDetail}
            title={copy.installTitle}
          />
        )}

        {status.update_available && !rJob && (
          <ListRow
            action={
              <Button onClick={() => void handleInstallRuntime()} size="sm">
                <Download />
                {copy.updateAction}
              </Button>
            }
            description={copy.updateDetail(status.configured_tag, status.tag)}
            title={copy.updateTitle}
          />
        )}

        {rJob && status.runtime_installed && (
          <ListRow
            below={<ProgressBar percent={rJob.percent} />}
            description={rJob.detail || copy.updating}
            title={
              <span className="inline-flex items-center gap-2">
                <Loader2 className="size-3.5 animate-spin" />
                {copy.updating}
              </span>
            }
          />
        )}

        {updateApplied && (
          <ListRow
            description={copy.upToDateDetail(status.tag, status.runtime_backend ?? 'cpu')}
            title={
              <span className="inline-flex items-center gap-2">
                <CheckCircle2 className="size-4 text-emerald-600 dark:text-emerald-400" />
                {copy.upToDateTitle}
              </span>
            }
          />
        )}

        {lastError?.kind === 'runtime-install' && (
          <p className="text-[0.75rem] text-destructive">{lastError.error}</p>
        )}
      </SettingsSection>

      {/* ── This machine ── */}
      <SettingsSection icon={Monitor} title={copy.hardwareTitle}>
        {hardware ? (
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1 py-1 text-[length:var(--conversation-caption-font-size)] text-muted-foreground">
            {hardware.gpu_name && (
              <span className="inline-flex items-center gap-1.5">
                <Zap className="size-3.5" />
                {hardware.gpu_name}
              </span>
            )}

            <span className="inline-flex items-center gap-1.5">
              <Cpu className="size-3.5" />
              {copy.vram(gbLabel(hardware.vram_total_bytes))}
            </span>

            <span className="inline-flex items-center gap-1.5">
              <Package className="size-3.5" />
              {copy.ram(gbLabel(hardware.ram_total_bytes))}
            </span>

            {hardware.uma && <Pill>{copy.unifiedMemory}</Pill>}
          </div>
        ) : (
          <p className="py-1 text-[length:var(--conversation-caption-font-size)] text-muted-foreground">
            {copy.hardwareLoading}
          </p>
        )}
      </SettingsSection>

      {/* ── Models ── */}
      <SettingsSection icon={Download} meta={`${catalog.length}`} title={copy.modelsTitle}>
        <div className="grid gap-1">
          {catalog.map(model => {
            const dJob = runningDownloadFor(jobs, model.id)
            const anyDownloadRunning = jobs.some(j => j.kind === 'model-download' && j.status === 'running')
            const activateTarget = model.downloaded_model_id ?? model.model_id
            const isActive = Boolean(activateTarget && status.active_model_id === activateTarget)
            const residency = activateTarget ? status.loaded_models[activateTarget] : undefined
            const isLoaded = residency === 'loaded' || residency === 'ready'
            const isLoadingNow = residency === 'loading'
            const livePlacement = activateTarget ? status.placement?.[activateTarget] : undefined

            const aJob = jobs.find(
              j => j.kind === 'model-activate' && j.status === 'running' && j.model_id === activateTarget
            )

            const anyActivateRunning = jobs.some(j => j.kind === 'model-activate' && j.status === 'running')

            return (
              <ListRow
                action={
                  model.downloaded ? (
                    <div className="flex items-center justify-end gap-2">
                      {isLoaded && livePlacement && (
                        <Tip
                          label={
                            livePlacement.spilled
                              ? copy.placementSpilledTip
                              : copy.placementResidentTip
                          }
                        >
                          <Pill tone={livePlacement.spilled ? 'warn' : 'success'}>
                            <Cpu className="mr-1 size-3" />
                            {livePlacement.granted_window_label ?? livePlacement.window_label ?? ''}
                            {' · '}
                            {livePlacement.spilled ? copy.placementSpilled : copy.placementResident}
                          </Pill>
                        </Tip>
                      )}
                      {isLoaded && !livePlacement && <Pill>{copy.loadedPill}</Pill>}

                      {isLoadingNow && (
                        <Pill>
                          <Loader2 className="mr-1 size-3 animate-spin" />
                          {copy.loadingPill}
                        </Pill>
                      )}

                      {isActive ? (
                        <Tip label={copy.activeDetail}>
                          <Pill tone="primary">
                            <Check className="mr-1 size-3" />
                            {copy.activePill}
                          </Pill>
                        </Tip>
                      ) : (
                        <Button
                          className={cn(aJob && '[&_svg]:animate-spin')}
                          disabled={anyActivateRunning}
                          onClick={() => void handleActivate(model)}
                          size="sm"
                        >
                          {aJob ? <Loader2 /> : <Check />}
                          {aJob ? copy.activating : copy.useAction}
                        </Button>
                      )}

                      {isLoaded && (
                        <Tip label={copy.ejectTip}>
                          <Button
                            onClick={() => void handleEject(activateTarget ?? model.id)}
                            size="icon"
                            variant="ghost"
                          >
                            <Eject />
                          </Button>
                        </Tip>
                      )}

                      <Tip label={copy.deleteAction}>
                        <Button
                          className={cn(deleting === model.id && '[&_svg]:animate-spin')}
                          onClick={() => void handleDelete(model)}
                          size="icon"
                          variant="ghost"
                        >
                          {deleting === model.id ? <Loader2 /> : <Trash2 />}
                        </Button>
                      </Tip>
                    </div>
                  ) : dJob ? undefined : (
                    <Button
                      disabled={!model.fits || anyDownloadRunning || !status.runtime_installed}
                      onClick={() => void handleDownload(model)}
                      size="sm"
                      variant="outline"
                    >
                      <Download />
                      {copy.downloadAction(model.size_label)}
                    </Button>
                  )
                }
                below={
                  dJob ? (
                    <div className="mt-2 grid gap-1">
                      <ProgressBar percent={dJob.percent} />

                      <p className="text-[0.68rem] text-muted-foreground">
                        {dJob.phase === 'verifying'
                          ? copy.verifying
                          : copy.downloadProgress(gbLabel(dJob.done_bytes), gbLabel(dJob.total_bytes))}
                      </p>
                    </div>
                  ) : undefined
                }
                description={
                  <>
                    {model.description}

                    <span className="mt-1.5 flex flex-wrap items-center gap-1.5">
                      {/* Memory: the traffic light. Green = runs fully on
                          the GPU; amber = spills to system RAM (works,
                          slower); red = doesn't fit this machine at all.
                          Detail prose lives in the tooltip. */}
                      {!model.fits ? (
                        <Tip label={model.fit_detail ?? model.fit_summary}>
                          <Pill tone="destructive">
                            <Cpu className="mr-1 size-3" />
                            {copy.pillTooBig}
                          </Pill>
                        </Tip>
                      ) : model.spilled ? (
                        <Tip label={model.quant_reason ?? model.fit_summary}>
                          <Pill tone="warn">
                            <Cpu className="mr-1 size-3" />
                            {copy.pillUsesRam}
                          </Pill>
                        </Tip>
                      ) : (
                        <Tip label={model.quant_reason ?? model.fit_summary}>
                          <Pill tone="success">
                            <Cpu className="mr-1 size-3" />
                            {copy.pillFitsGpu}
                          </Pill>
                        </Tip>
                      )}

                      {/* Context: where it starts, and how far it can grow.
                          A model granted its full native window collapses
                          both into one green pill. */}
                      {model.fits && model.start_window_label && (
                        model.start_window && model.start_window >= model.native_context ? (
                          <Tip label={copy.pillFullContextTip}>
                            <Pill tone="success">{copy.pillFullContext(model.native_context_label)}</Pill>
                          </Tip>
                        ) : (
                          <>
                            <Tip label={copy.pillStartsTip}>
                              <Pill>{copy.pillStarts(model.start_window_label)}</Pill>
                            </Tip>
                            <Tip label={copy.pillGrowsTip}>
                              <Pill>{copy.pillUpTo(model.native_context_label)}</Pill>
                            </Tip>
                          </>
                        )
                      )}

                      {!model.fits && (
                        <Pill>{copy.pillUpTo(model.native_context_label)}</Pill>
                      )}

                      {model.vision && <Pill>{copy.pillVision}</Pill>}
                    </span>

                    {isActive && !isLoaded && !isLoadingNow && status.server_running && (
                      <span className="mt-0.5 block text-(--ui-text-tertiary)">{copy.activeNotLoaded}</span>
                    )}
                  </>
                }
                key={model.id}
                title={
                  <span className="inline-flex items-center gap-2">
                    {model.display_name}

                    {model.tags.includes('recommended') && <Pill tone="primary">{copy.recommended}</Pill>}

                    <span className="text-[0.68rem] font-normal text-muted-foreground">
                      {model.downloaded_quant ?? model.quant ?? ''}
                      {(model.downloaded_quant ?? model.quant) ? ' · ' : ''}
                      {model.size_label}
                    </span>
                  </span>
                }
              />
            )
          })}
        </div>

        {lastError?.kind === 'model-download' && (
          <p className="text-[0.75rem] text-destructive">{lastError.error}</p>
        )}
      </SettingsSection>
    </SettingsContent>
  )
}
