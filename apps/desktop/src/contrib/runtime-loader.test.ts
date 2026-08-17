import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReadDirResult } from '@/global'
import type * as HermesModule from '@/hermes'

import { $pluginRecords, setPluginEnabled } from './plugins-store'
import { discoverRuntimePlugins, loadRuntimePlugin, unloadRuntimePlugin, watchRuntimePlugins } from './runtime-loader'

// getStatus would supply the connected backend's hermes_home — a REMOTE path in
// remote mode. The disk scanner must NOT derive the plugin root from it (#66899).
const getStatus = vi.fn(async () => ({ hermes_home: '/remote/box/.hermes' }))

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getStatus: () => getStatus()
}))

const desktopPluginsRoot = vi.fn<() => Promise<string>>()
const agentPluginsRoot = vi.fn<() => Promise<string>>()
const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()
const readFileText = vi.fn<(path: string) => Promise<{ text: string }>>()
const watchDirectory = vi.fn<(path: string) => Promise<{ id: string }>>()
const watchPreviewFile = vi.fn<(path: string) => Promise<{ id: string }>>()
const stopPreviewFileWatch = vi.fn<(id: string) => Promise<void>>()
const onPreviewFileChanged = vi.fn()

function installRuntimeImportShim(): () => void {
  // The loader evaluates plugins via blob-URL import(), which vite's module
  // runner can't resolve in tests — reroute to a data: URL, which node's
  // native ESM loader handles.
  const createObjectURL = vi
    .spyOn(URL, 'createObjectURL')
    .mockImplementation(
      blob =>
        `data:text/javascript;base64,${Buffer.from((blob as unknown as { parts: string[] }).parts.join('')).toString('base64')}`
    )

  const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  const RealBlob = globalThis.Blob

  vi.stubGlobal(
    'Blob',
    class {
      parts: string[]
      constructor(parts: string[]) {
        this.parts = parts
      }
    }
  )

  return () => {
    createObjectURL.mockRestore()
    revokeObjectURL.mockRestore()
    vi.stubGlobal('Blob', RealBlob)
  }
}

beforeEach(() => {
  desktopPluginsRoot.mockReset()
  agentPluginsRoot.mockReset()
  readDir.mockReset()
  readFileText.mockReset()
  watchDirectory.mockReset()
  watchPreviewFile.mockReset()
  stopPreviewFileWatch.mockReset()
  onPreviewFileChanged.mockReset()
  getStatus.mockClear()
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    agentPluginsRoot,
    desktopPluginsRoot,
    onPreviewFileChanged,
    readDir,
    readFileText,
    stopPreviewFileWatch,
    watchDirectory,
    watchPreviewFile
  }
})

afterEach(() => {
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('scanDiskPlugins (#66899)', () => {
  it('scans the Electron-resolved local roots, never the backend hermes_home', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.hermes/plugins')
    readDir.mockResolvedValue({ entries: [] })

    await discoverRuntimePlugins()

    expect(desktopPluginsRoot).toHaveBeenCalled()
    expect(readDir).toHaveBeenCalledWith('/local/.hermes/desktop-plugins')
    expect(readDir).toHaveBeenCalledWith('/local/.hermes/plugins')
    // The remote backend's hermes_home must never feed the local plugin scan.
    expect(getStatus).not.toHaveBeenCalled()
    expect(readDir).not.toHaveBeenCalledWith('/remote/box/.hermes/desktop-plugins')
  })

  it('no-ops when the resolvers yield no local root', async () => {
    desktopPluginsRoot.mockResolvedValue('')
    agentPluginsRoot.mockResolvedValue('')

    await discoverRuntimePlugins()

    expect(readDir).not.toHaveBeenCalled()
  })

  it('probes desktop/plugin.js inside agent-plugin packages (unified packaging)', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.hermes/plugins')
    readDir.mockImplementation(async dir =>
      dir === '/local/.hermes/plugins'
        ? { entries: [{ isDirectory: true, name: 'my-feature', path: '/local/.hermes/plugins/my-feature' }] }
        : { entries: [] }
    )
    // No desktop half in this package — probe must target desktop/plugin.js.
    readFileText.mockRejectedValue(new Error('ENOENT'))

    await discoverRuntimePlugins()

    expect(readFileText).toHaveBeenCalledWith('/local/.hermes/plugins/my-feature/desktop/plugin.js')
    // The Python half's files must never be probed as a desktop entry.
    expect(readFileText).not.toHaveBeenCalledWith('/local/.hermes/plugins/my-feature/plugin.js')
  })

  it('still scans the standalone root when agentPluginsRoot is absent (older shell)', async () => {
    delete (window.hermesDesktop as unknown as { agentPluginsRoot?: unknown }).agentPluginsRoot
    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    readDir.mockResolvedValue({ entries: [] })

    await discoverRuntimePlugins()

    expect(readDir).toHaveBeenCalledWith('/local/.hermes/desktop-plugins')
    expect(readDir).toHaveBeenCalledTimes(1)
  })

  it('loads a unified desktop half OPT-IN: inventoried but not activated by default', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.hermes/plugins')
    readDir.mockImplementation(async dir =>
      dir === '/local/.hermes/plugins'
        ? { entries: [{ isDirectory: true, name: 'uni', path: '/local/.hermes/plugins/uni' }] }
        : { entries: [] }
    )

    const register = vi.fn()

    ;(globalThis as unknown as { __uniRegister: unknown }).__uniRegister = register
    readFileText.mockResolvedValue({
      text: 'export default { id: "uni", register: globalThis.__uniRegister }'
    })
    watchPreviewFile.mockResolvedValue({ id: 'w-uni' })

    const restoreRuntimeImport = installRuntimeImportShim()

    try {
      await discoverRuntimePlugins()

      // Inventoried for Settings → Plugins, but the root's opt-in posture wins:
      // ~/.hermes/plugins stays installed-but-inert until the user toggles it.
      expect($pluginRecords.get().uni).toMatchObject({ kind: 'disk', status: 'disabled' })
      expect(register).not.toHaveBeenCalled()

      // The user's explicit enable still activates it.
      await setPluginEnabled('uni', true)
      expect(register).toHaveBeenCalledTimes(1)
      expect($pluginRecords.get().uni.status).toBe('loaded')
    } finally {
      restoreRuntimeImport()
      delete (globalThis as unknown as { __uniRegister?: unknown }).__uniRegister
    }
  })

  it('keeps colliding healthy and malformed cross-root plugins independently visible', async () => {
    const name = 'inventory-collision'
    const standaloneFile = `/local/.hermes/desktop-plugins/${name}/plugin.js`
    const unifiedFile = `/local/.hermes/plugins/${name}/desktop/plugin.js`

    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.hermes/plugins')
    readDir.mockImplementation(async root => ({
      entries: [
        {
          isDirectory: true,
          name,
          path:
            root === '/local/.hermes/desktop-plugins'
              ? `/local/.hermes/desktop-plugins/${name}`
              : `/local/.hermes/plugins/${name}`
        }
      ]
    }))
    readFileText.mockImplementation(async file => ({
      text: file === standaloneFile ? `export default { id: '${name}', register() {} }` : 'export default {}'
    }))
    watchPreviewFile.mockImplementation(async file => ({ id: `watch:${file}` }))

    const restoreRuntimeImport = installRuntimeImportShim()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    try {
      await discoverRuntimePlugins()

      expect($pluginRecords.get()[name]).toMatchObject({ file: standaloneFile, status: 'loaded' })
      expect(Object.values($pluginRecords.get()).find(record => record.file === unifiedFile)).toMatchObject({
        name,
        status: 'error'
      })
    } finally {
      consoleError.mockRestore()
      restoreRuntimeImport()
    }
  })
})

describe('runtime plugin unload isolation', () => {
  it('runs every disposer and does not let one failure escape', async () => {
    const laterDispose = vi.fn()

    const register = (ctx: { onDispose: (dispose: () => void) => void }) => {
      ctx.onDispose(() => {
        throw new Error('broken disposer')
      })
      ctx.onDispose(laterDispose)
    }

    ;(globalThis as unknown as { __disposerIsolationRegister: unknown }).__disposerIsolationRegister = register
    const restoreRuntimeImport = installRuntimeImportShim()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    try {
      expect(
        await loadRuntimePlugin(
          `export default { id: 'disposer-isolation', register: globalThis.__disposerIsolationRegister }`,
          'disposer-isolation'
        )
      ).toBe('disposer-isolation')

      expect(() => unloadRuntimePlugin('disposer-isolation')).not.toThrow()
      expect(laterDispose).toHaveBeenCalledOnce()
      unloadRuntimePlugin('disposer-isolation')
      expect(laterDispose).toHaveBeenCalledOnce()
    } finally {
      consoleError.mockRestore()
      restoreRuntimeImport()
      delete (globalThis as unknown as { __disposerIsolationRegister?: unknown }).__disposerIsolationRegister
    }
  })
})

describe('watchRuntimePlugins dir watch (#66899)', () => {
  it('watches both Electron-resolved local roots, never the backend hermes_home', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.hermes/plugins')
    readDir.mockResolvedValue({ entries: [] })
    watchDirectory.mockResolvedValue({ id: 'watch-1' })

    watchRuntimePlugins()
    // Drain the async scan + startDirWatches chains.
    await vi.waitFor(() => expect(watchDirectory).toHaveBeenCalledTimes(2))

    expect(watchDirectory).toHaveBeenCalledWith('/local/.hermes/desktop-plugins')
    expect(watchDirectory).toHaveBeenCalledWith('/local/.hermes/plugins')
    expect(watchDirectory).not.toHaveBeenCalledWith('/remote/box/.hermes/desktop-plugins')
    expect(getStatus).not.toHaveBeenCalled()
  })
})
