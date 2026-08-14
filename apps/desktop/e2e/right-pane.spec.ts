import {
  type MockBackendFixture,
  setupMockBackend,
  waitForAppReady,
} from './fixtures'
import { expect, test } from './test'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('persistent terminal overlay follows the pane after split dragging', async () => {
  const page = fixture!.page

  await page.keyboard.press('Control+`')
  await page.locator('[data-terminal-slot]').waitFor({ state: 'visible', timeout: 30_000 })
  await page.locator('[data-persistent-terminal] .xterm').waitFor({ state: 'visible', timeout: 30_000 })

  const result = await page.evaluate(async () => {
    const slot = document.querySelector('[data-terminal-slot]')
    const overlay = document.querySelector('[data-persistent-terminal]')

    if (!slot || !overlay) {
      return { drift: -1, moved: 0, target: false }
    }

    const before = slot.getBoundingClientRect()
    const target = [...document.querySelectorAll<HTMLElement>('[role="separator"]')]
      .map(element => {
        const box = element.getBoundingClientRect()
        const horizontal = box.width > box.height
        const center = horizontal
          ? (box.top + box.bottom) / 2
          : (box.left + box.right) / 2
        const sides = horizontal
          ? [before.top, before.bottom]
          : [before.left, before.right]

        return {
          element,
          box,
          horizontal,
          score: Math.min(...sides.map(side => Math.abs(center - side))),
        }
      })
      .filter(item => item.box.width > 0 && item.box.height > 0)
      .sort((a, b) => a.score - b.score)[0]

    if (!target) {
      return { drift: -1, moved: 0, target: false }
    }

    const x = target.box.left + target.box.width / 2
    const y0 = target.box.top + target.box.height / 2
    const nearestSide = target.horizontal
      ? Math.abs(y0 - before.top) < Math.abs(y0 - before.bottom)
        ? 'top'
        : 'bottom'
      : Math.abs(x - before.left) < Math.abs(x - before.right)
        ? 'left'
        : 'right'
    const deltaX = nearestSide === 'left' ? -1 : nearestSide === 'right' ? 1 : 0
    const deltaY = nearestSide === 'top' ? -1 : nearestSide === 'bottom' ? 1 : 0
    let currentX = x
    let y = y0
    const pointer = {
      bubbles: true,
      cancelable: true,
      pointerId: 71,
      pointerType: 'mouse',
      isPrimary: true,
      button: 0,
      buttons: 1,
    }

    target.element.dispatchEvent(
      new PointerEvent('pointerdown', { ...pointer, clientX: x, clientY: y }),
    )

    for (let index = 0; index < 24; index += 1) {
      currentX += deltaX
      y += deltaY
      window.dispatchEvent(
        new PointerEvent('pointermove', {
          ...pointer,
          clientX: currentX,
          clientY: y,
        }),
      )
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    }

    window.dispatchEvent(
      new PointerEvent('pointerup', {
        ...pointer,
        buttons: 0,
        clientX: currentX,
        clientY: y,
      }),
    )
    await new Promise<void>(resolve => setTimeout(resolve, 350))
    await new Promise<void>(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    )

    const next = slot.getBoundingClientRect()
    const fixed = overlay.getBoundingClientRect()

    return {
      drift: Math.max(
        Math.abs(next.top - fixed.top),
        Math.abs(next.left - fixed.left),
        Math.abs(next.width - fixed.width),
        Math.abs(next.height - fixed.height),
      ),
      moved: Math.max(
        Math.abs(next.top - before.top),
        Math.abs(next.left - before.left),
        Math.abs(next.width - before.width),
        Math.abs(next.height - before.height),
      ),
      target: true,
    }
  })

  expect(result.target).toBe(true)
  expect(result.moved).toBeGreaterThan(10)
  expect(result.drift).toBeLessThanOrEqual(1)
})

test('a primary click restores focus from a minimized terminal tree rail', async () => {
  const page = fixture!.page
  const terminalSlot = page.locator('[data-terminal-slot]')

  if (!(await terminalSlot.isVisible())) {
    await page.keyboard.press('Control+`')
    await terminalSlot.waitFor({ state: 'visible', timeout: 30_000 })
  }

  // Quad places the terminal in a row split, so minimizing it produces the
  // vertical rail whose focused primary-click handoff is under test.
  await page.getByRole('button', { name: 'Layout editor' }).click()
  await page.getByRole('button', { name: 'Quad', exact: true }).click()
  await page.getByRole('button', { name: 'Done', exact: true }).click()

  const terminalTreeGroup = page.locator('[data-tree-group]').filter({
    has: page.locator('[data-tree-tab="terminal"]')
  })

  await expect(terminalTreeGroup).toHaveCount(1, { timeout: 30_000 })
  await terminalTreeGroup.getByRole('button', { name: 'Minimize' }).click()

  const minimizedTerminalTab = terminalTreeGroup.locator('[data-tree-tab="terminal"] > [role="tab"]')
  await expect(minimizedTerminalTab).toBeVisible({ timeout: 30_000 })
  await minimizedTerminalTab.focus()
  await expect(minimizedTerminalTab).toBeFocused()
  await minimizedTerminalTab.click()

  const restoredTerminalTab = terminalTreeGroup.locator('[data-tree-tab="terminal"] > [role="tab"]')
  await expect(terminalTreeGroup.getByRole('button', { name: 'Minimize' })).toBeVisible({ timeout: 30_000 })
  await expect(restoredTerminalTab).toBeFocused({ timeout: 30_000 })
})

test('terminal rail uses roving tabs with matching panels and its global close gesture', async () => {
  const page = fixture!.page
  const terminalSlot = page.locator('[data-terminal-slot]')

  if (!(await terminalSlot.isVisible())) {
    await page.keyboard.press('Control+`')
    await terminalSlot.waitFor({ state: 'visible', timeout: 30_000 })
  }

  const railTabs = page.locator('[data-terminal-rail-tab][role="tab"]')
  await expect(railTabs).toHaveCount(1, { timeout: 30_000 })
  await page.getByRole('button', { name: 'New terminal' }).click()
  await expect(railTabs).toHaveCount(2, { timeout: 30_000 })

  const railTablist = railTabs.first().locator('xpath=ancestor::*[@role="tablist"][1]')
  const initiallySelectedTab = railTablist.locator('[role="tab"][aria-selected="true"]')
  await expect(railTablist).toHaveAttribute('aria-orientation', 'vertical')
  await expect(initiallySelectedTab).toHaveCount(1)
  await expect(initiallySelectedTab).toHaveAttribute('tabindex', '0')
  await expect(railTablist.locator('[role="tab"][aria-selected="false"]').first()).toHaveAttribute('tabindex', '-1')

  await railTabs.first().focus()
  await page.keyboard.press('End')

  const selectedTab = railTablist.locator('[role="tab"][aria-selected="true"]')
  const panelId = await selectedTab.getAttribute('aria-controls')
  const selectedTabId = await selectedTab.getAttribute('id')

  if (!panelId || !selectedTabId) {
    throw new Error('Expected a selected terminal tab with linked panel identifiers')
  }

  await expect(selectedTab).toBeFocused()
  await expect(page.locator(`[role="tabpanel"][id="${panelId}"]`)).toHaveAttribute('aria-labelledby', selectedTabId)
  await expect(page.locator(`[role="tabpanel"][id="${panelId}"]`)).toHaveAttribute('aria-hidden', 'false')

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+w' : 'Control+w')
  await expect(railTabs).toHaveCount(1)
  await expect(railTablist.locator('[role="tab"][aria-selected="true"]')).toBeFocused()
})

test('global close identifies the focused real xterm panel', async () => {
  const page = fixture!.page
  const terminalSlot = page.locator('[data-terminal-slot]')

  if (!(await terminalSlot.isVisible())) {
    await page.keyboard.press('Control+`')
    await terminalSlot.waitFor({ state: 'visible', timeout: 30_000 })
  }

  const railTabs = page.locator('[data-terminal-rail-tab][role="tab"]')
  await expect(railTabs).toHaveCount(1, { timeout: 30_000 })
  await page.getByRole('button', { name: 'New terminal' }).click()
  await expect(railTabs).toHaveCount(2, { timeout: 30_000 })

  const focusedTab = page.locator('[data-terminal-rail-tab][role="tab"][aria-selected="true"]')
  const terminalId = await focusedTab.getAttribute('data-terminal-rail-tab')
  const panelId = await focusedTab.getAttribute('aria-controls')

  if (!terminalId || !panelId) {
    throw new Error('Expected the selected terminal tab to identify its xterm panel')
  }

  const panel = page.locator(`[role="tabpanel"][id="${panelId}"]`)
  const xtermInput = panel.locator('.xterm textarea').first()
  await expect(panel).toHaveAttribute('data-terminal-id', terminalId)
  await xtermInput.focus()
  await expect(xtermInput).toBeFocused()

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+w' : 'Control+w')
  await expect(railTabs).toHaveCount(1)
  await expect(railTabs).not.toHaveAttribute('data-terminal-rail-tab', terminalId)

  const survivor = railTabs.first()
  await expect(survivor).toBeFocused()

  // The first close must leave ownership in the nested terminal stack. A
  // second global close should therefore remove the surviving terminal, not
  // dismiss the containing layout pane while leaving its xterm alive.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+w' : 'Control+w')
  await expect(railTabs).toHaveCount(0)
})

test('terminal rail keeps focus after its selected survivor finishes initializing', async () => {
  const page = fixture!.page
  const terminalSlot = page.locator('[data-terminal-slot]')

  if (!(await terminalSlot.isVisible())) {
    await page.keyboard.press('Control+`')
    await terminalSlot.waitFor({ state: 'visible', timeout: 30_000 })
  }

  const railTabs = page.locator('[data-terminal-rail-tab][role="tab"]')
  await expect(railTabs).toHaveCount(1, { timeout: 30_000 })

  await page.evaluate(() => {
    const fontSet = document.fonts
    const original = Object.getOwnPropertyDescriptor(fontSet, 'load')
    const hadOwnLoad = Boolean(original)
    let resolve: (faces: FontFace[]) => void
    const pending = new Promise<FontFace[]>(done => {
      resolve = done
    })
    const state = window as Window & {
      __releaseTerminalFontLoad?: () => void
      __terminalFontLoadCalls?: number
    }

    state.__terminalFontLoadCalls = 0
    Object.defineProperty(fontSet, 'load', {
      configurable: true,
      value: () => {
        state.__terminalFontLoadCalls = (state.__terminalFontLoadCalls ?? 0) + 1

        return pending
      }
    })
    state.__releaseTerminalFontLoad = () => {
      resolve([])

      if (hadOwnLoad && original) {
        Object.defineProperty(fontSet, 'load', original)
      } else {
        Reflect.deleteProperty(fontSet, 'load')
      }
    }
  })

  await page.getByRole('button', { name: 'New terminal' }).click()
  await expect(railTabs).toHaveCount(2, { timeout: 30_000 })
  await expect
    .poll(() => page.evaluate(() => (window as Window & { __terminalFontLoadCalls?: number }).__terminalFontLoadCalls ?? 0))
    .toBeGreaterThanOrEqual(3)

  const pendingTab = railTabs.nth(1)
  const pendingPanelId = await pendingTab.getAttribute('aria-controls')

  if (!pendingPanelId) {
    throw new Error('Expected delayed terminal tab to control a panel')
  }

  const pendingTerminal = page.locator(`[role="tabpanel"][id="${pendingPanelId}"] .xterm`)
  await expect(pendingTerminal).toHaveCount(0)

  const firstTab = railTabs.first()
  await firstTab.click()
  await expect(firstTab).toHaveAttribute('aria-selected', 'true')
  await firstTab.click({ modifiers: ['Meta'] })
  await expect(railTabs).toHaveCount(1)

  const selectedTab = page.locator('[data-terminal-rail-tab][role="tab"][aria-selected="true"]')
  await expect(selectedTab).toBeFocused()
  await page.evaluate(() => {
    ;(window as Window & { __releaseTerminalFontLoad?: () => void }).__releaseTerminalFontLoad?.()
  })
  await expect(pendingTerminal).toHaveCount(1, { timeout: 30_000 })
  await expect(selectedTab).toBeFocused({ timeout: 30_000 })
})
