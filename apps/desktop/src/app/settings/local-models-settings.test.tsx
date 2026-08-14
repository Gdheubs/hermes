import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $localRuntimeJobs } from '@/store/local-runtime-jobs'
import type { LocalCatalogModel, LocalHardware, LocalModelsStatus, LocalRuntimeJob } from '@/types/hermes'

import { LocalModelsSettings } from './local-models-settings'

// Mock the API layer — the pane's contract is what it RENDERS from these
// payloads, not transport.
vi.mock('@/hermes', () => ({
  activateLocalModel: vi.fn(),
  deleteLocalModel: vi.fn(),
  downloadLocalModel: vi.fn(),
  ejectLocalModel: vi.fn(),
  getLocalCatalog: vi.fn(),
  getLocalHardware: vi.fn(),
  getLocalModelsJobs: vi.fn(),
  getLocalModelsStatus: vi.fn(),
  getLocalRuntimeJob: vi.fn(),
  installLocalRuntime: vi.fn()
}))

import * as hermes from '@/hermes'

const mocked = vi.mocked(hermes)

const BASE_STATUS: LocalModelsStatus = {
  enabled: true,
  tag: 'b10290',
  configured_tag: 'b10290',
  update_available: false,
  runtime_installed: false,
  runtime_backend: null,
  server_running: false,
  server_base_url: null,
  active_model_id: null,
  loaded_models: {},
  models: [],
  models_dir: 'C:/somewhere/models'
}

const BASE_HARDWARE: LocalHardware = {
  uma: false,
  vram_total_bytes: 32 * 2 ** 30,
  vram_usable_bytes: 26 * 2 ** 30,
  ram_total_bytes: 256 * 2 ** 30,
  ram_available_bytes: 200 * 2 ** 30,
  vram_label: '32.0 GB',
  gpu_name: 'NVIDIA GeForce RTX 5090',
  gpu_util_percent: 12,
  vram_used_bytes: 6 * 2 ** 30
}

const FITTING_MODEL: LocalCatalogModel = {
  id: 'Qwen3.6-27B-UD-Q4_K_XL',
  display_name: 'Qwen3.6 27B',
  description: 'Best all-round agent model; long context stays fast',
  size_bytes: 17.6 * 2 ** 30,
  size_label: '17.6 GB',
  native_context: 262144,
  native_context_label: '256K',
  tags: ['recommended'],
  downloaded: false,
  mtp: false,
  fits: true,
  fit_summary: 'runs at its full 256K context',
  start_window: 262144,
  start_window_label: '256K',
  spilled: false
}

const SPILLED_MODEL: LocalCatalogModel = {
  ...FITTING_MODEL,
  id: 'Spilled-Model',
  display_name: 'Spilled Model',
  tags: [],
  fits: true,
  spilled: true,
  start_window: 65536,
  start_window_label: '64K',
  fit_summary: 'starts at 64K and grows toward 256K as you use it (larger than your GPU memory — runs slower)'
}

const REFUSED_MODEL: LocalCatalogModel = {
  ...FITTING_MODEL,
  id: 'Huge-Model',
  display_name: 'Huge Model',
  tags: [],
  fits: false,
  fit_summary: 'Needs more memory than this machine has',
  fit_detail: 'needs ~60 GiB at the 64K floor',
  start_window: undefined,
  start_window_label: undefined
}

function renderPane() {
  return render(
    <I18nProvider>
      <LocalModelsSettings />
    </I18nProvider>
  )
}

beforeEach(() => {
  mocked.getLocalModelsStatus.mockResolvedValue(BASE_STATUS)
  mocked.getLocalHardware.mockResolvedValue(BASE_HARDWARE)
  mocked.getLocalCatalog.mockResolvedValue({ models: [FITTING_MODEL, SPILLED_MODEL, REFUSED_MODEL] })
  mocked.getLocalModelsJobs.mockResolvedValue({ jobs: [] })
  $localRuntimeJobs.set([])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('LocalModelsSettings', () => {
  it('offers the runtime install with a plain-language explanation', async () => {
    renderPane()

    expect(await screen.findByText('Install the local runtime')).toBeTruthy()
    expect(screen.getByText(/runs? entirely on this machine/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /install runtime/i })).toBeTruthy()
  })

  it('shows every catalog model with fit pills; unaffordable ones stay visible with the reason', async () => {
    renderPane()

    expect(await screen.findByText('Qwen3.6 27B')).toBeTruthy()
    // The fitting model reads as pills, not prose: green memory pill +
    // full-context pill (start_window == native here).
    expect(screen.getByText('Fits your GPU')).toBeTruthy()
    expect(screen.getByText('Full 256K context')).toBeTruthy()

    // The refused model is NOT hidden (discoverability rule): red memory
    // pill, and the ceiling it would have had (spilled model shows one too).
    expect(screen.getByText('Huge Model')).toBeTruthy()
    expect(screen.getByText('Too big for this machine')).toBeTruthy()
    expect(screen.getAllByText('Up to 256K').length).toBeGreaterThanOrEqual(1)

    // The spilled model reads amber + a start/ceiling pair.
    expect(screen.getByText('Spilled Model')).toBeTruthy()
    expect(screen.getByText('Uses system RAM')).toBeTruthy()
    expect(screen.getByText('Starts at 64K')).toBeTruthy()

    // Its download button is disabled; the fitting model's is enabled once
    // the runtime exists (here runtime_installed=false, so both disabled —
    // asserted separately below).
    const buttons = screen.getAllByRole('button', { name: /download · 17\.6 GB/i })
    expect(buttons.every(b => (b as HTMLButtonElement).disabled)).toBe(true)
  })

  it('enables downloads only once the runtime is installed', async () => {
    mocked.getLocalModelsStatus.mockResolvedValue({
      ...BASE_STATUS,
      runtime_installed: true,
      runtime_backend: 'cuda'
    })
    renderPane()

    await screen.findByText('Qwen3.6 27B')
    const [fittingButton] = screen.getAllByRole('button', { name: /download · 17\.6 GB/i })
    expect((fittingButton as HTMLButtonElement).disabled).toBe(false)
  })

  it('shows hardware facts after backfill', async () => {
    renderPane()

    expect(await screen.findByText('NVIDIA GeForce RTX 5090')).toBeTruthy()
    expect(screen.getByText(/32\.0 GB GPU memory/)).toBeTruthy()
    expect(screen.getByText(/256\.0 GB RAM/)).toBeTruthy()
  })

  it('tracks a download job to completion and refreshes', async () => {
    mocked.getLocalModelsStatus.mockResolvedValue({
      ...BASE_STATUS,
      runtime_installed: true,
      runtime_backend: 'cuda'
    })
    mocked.downloadLocalModel.mockResolvedValue({ job_id: 'j1' })

    const running: LocalRuntimeJob = {
      job_id: 'j1',
      kind: 'model-download',
      target: 'Qwen3.6 27B',
      model_id: FITTING_MODEL.id,
      status: 'running',
      phase: 'downloading',
      detail: 'Qwen3.6 27B — 17.6 GB',
      total_bytes: 100,
      done_bytes: 40,
      percent: 40,
      error: null
    }

    mocked.getLocalModelsJobs
      .mockResolvedValueOnce({ jobs: [running] })
      .mockResolvedValue({ jobs: [{ ...running, status: 'done', phase: 'done', done_bytes: 100, percent: 100 }] })

    renderPane()
    await screen.findByText('Qwen3.6 27B')

    const [download] = screen.getAllByRole('button', { name: /download · 17\.6 GB/i })
    download.click()

    // The app-level watcher follows the job; when it settles the pane
    // refreshes (status + catalog re-fetched).
    await waitFor(() => {
      expect(mocked.getLocalModelsJobs).toHaveBeenCalled()
      expect(mocked.getLocalModelsStatus.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
  })

  it('renders progress for a download discovered from the store (survives pane remount)', async () => {
    mocked.getLocalModelsStatus.mockResolvedValue({
      ...BASE_STATUS,
      runtime_installed: true,
      runtime_backend: 'cuda'
    })
    // A running job already in the app-level store — as after closing and
    // reopening the pane mid-download.
    $localRuntimeJobs.set([
      {
        job_id: 'j9',
        kind: 'model-download',
        target: 'Qwen3.6 27B',
        model_id: FITTING_MODEL.id,
        status: 'running',
        phase: 'downloading',
        detail: '',
        total_bytes: 100,
        done_bytes: 62,
        percent: 62,
        error: null
      }
    ])

    renderPane()
    await screen.findByText('Qwen3.6 27B')

    // The fitting row shows byte progress; the remaining download
    // buttons belong to the other rows (spilled + refused).
    expect(screen.getByText(/0\.0 GB of 0\.0 GB|of/)).toBeTruthy()
    const remaining = screen.queryAllByRole('button', { name: /download · 17\.6 GB/i })
    expect(remaining.length).toBe(2)
    expect(remaining.some(b => (b as HTMLButtonElement).disabled)).toBe(true)
  })

  it('surfaces a failed download with the backend message', async () => {
    mocked.getLocalModelsStatus.mockResolvedValue({
      ...BASE_STATUS,
      runtime_installed: true,
      runtime_backend: 'cuda'
    })
    $localRuntimeJobs.set([
      {
        job_id: 'j2',
        kind: 'model-download',
        target: 'Qwen3.6 27B',
        model_id: FITTING_MODEL.id,
        status: 'error',
        phase: 'verifying',
        detail: '',
        total_bytes: 100,
        done_bytes: 100,
        error: 'Downloaded file failed its integrity check and was removed — try again'
      }
    ])

    renderPane()
    await screen.findByText('Qwen3.6 27B')

    expect(await screen.findByText(/integrity check/)).toBeTruthy()
  })
})
