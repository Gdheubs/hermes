/**
 * E2E coverage for directly closing stacked session tabs.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expect, test } from './test'

let fixture: MockBackendFixture | null = null

const REPLY = 'Hello from the mock inference server! The full boot chain is working.'

async function sendMessage(page: MockBackendFixture['page'], text: string): Promise<void> {
  const activeTranscript = page.locator('[data-slot="aui_thread-viewport"]:visible').last()

  // A new draft briefly coexists with the previous session while the renderer
  // switches context. Wait for the new empty transcript instead of sleeping.
  await expect(activeTranscript).not.toContainText(REPLY, { timeout: 10_000 })

  const composer = page.locator('[contenteditable="true"]:visible').last()
  await composer.waitFor({ state: 'visible', timeout: 10_000 })
  await expect.poll(() => composer.textContent(), { timeout: 10_000 }).toBe('')
  await composer.click()
  await expect(composer).toBeFocused()
  await composer.type(text, { delay: 20 })
  await page.keyboard.press('Enter')

  await expect(activeTranscript).toContainText(text, { timeout: 15_000 })
  await expect(activeTranscript).toContainText(REPLY, { timeout: 60_000 })
}

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('reveals a close control and closes an identified inactive tab without changing selection', async ({
  playwright: _playwright
}, testInfo) => {
  const page = fixture!.page

  await sendMessage(page, 'first close-button session')
  await page.locator('[data-slot="sidebar"] button[aria-label="New session"]').first().click()
  await sendMessage(page, 'second close-button session')

  const sessionRows = page.locator('[data-slot="sidebar"] button:has([data-reorder-handle])')
  await expect(sessionRows).toHaveCount(2)

  // Dispatch the ctrl-modified click directly so macOS does not translate the
  // gesture into a native context click before React receives it.
  await sessionRows.last().dispatchEvent('click', { ctrlKey: true })

  const closeButtons = page.locator('[data-tree-tab] > button[aria-label="Close tab"]')
  await expect.poll(() => closeButtons.count()).toBeGreaterThan(1)

  const tabList = closeButtons.first().locator('xpath=ancestor::*[@role="tablist"][1]')
  const tabItems = tabList.locator('[data-tree-tab]')
  const initialTabCount = await tabItems.count()
  const selectedTab = tabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')

  const inactiveTab = tabList
    .locator('[data-tree-tab]:has(> button[aria-label="Close tab"]) > [role="tab"][aria-selected="false"]')
    .first()

  await expect(selectedTab).toHaveCount(1)
  await expect(inactiveTab).toHaveCount(1)
  await expect(selectedTab).toHaveAttribute('tabindex', '0')
  await expect(inactiveTab).toHaveAttribute('tabindex', '-1')

  const selectedTabItem = selectedTab.locator('xpath=..')
  const inactiveTabItem = inactiveTab.locator('xpath=..')
  const selectedTabId = await selectedTabItem.getAttribute('data-tree-tab')
  const inactiveTabId = await inactiveTabItem.getAttribute('data-tree-tab')

  if (!selectedTabId || !inactiveTabId) {
    throw new Error('Expected stacked pane tabs to expose stable data-tree-tab identities')
  }

  expect(inactiveTabId).not.toBe(selectedTabId)

  const activeTranscript = page.locator('[data-slot="aui_thread-viewport"]:visible').last()
  await expect(activeTranscript).toContainText('first close-button session')

  const closeButton = inactiveTabItem.locator('button[aria-label="Close tab"]')
  const closeIcon = closeButton.locator('[data-slot="pane-tab-close-icon"]')
  await expect(closeIcon).toHaveCSS('opacity', '0')

  // The close glyph is a sibling of the tab control. Its right-click must
  // still reach the session trigger that wraps the whole visual tab, rather
  // than falling into the strip menu's dead zone.
  await closeButton.click({ button: 'right' })
  const sessionMenu = page.getByRole('menu', { name: 'Session actions' })
  await expect(sessionMenu).toBeVisible()
  await expect(sessionMenu.getByRole('menuitem', { name: 'Close', exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(sessionMenu).toBeHidden()

  await inactiveTabItem.hover()
  await expect(closeIcon).toHaveCSS('opacity', '1')
  await page.screenshot({ path: testInfo.outputPath('tab-close-hover.png') })

  // Preserve the direct pointer path: a pointer close removes only the
  // identified inactive tab and recovers focus to the surviving selection.
  const closeBox = await closeButton.boundingBox()

  if (!closeBox) {
    throw new Error('Expected the inactive tab close control to be visible')
  }

  await page.mouse.move(closeBox.x + closeBox.width / 2, closeBox.y + closeBox.height / 2)
  await page.mouse.down()
  await expect(closeButton).toBeFocused()
  await page.mouse.up()
  await expect
    .poll(() => tabItems.evaluateAll(items => items.map(item => item.getAttribute('data-tree-tab'))))
    .not.toContain(inactiveTabId)
  await expect(tabItems).toHaveCount(initialTabCount - 1)

  const pointerRemainingSelectedTab = tabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')
  await expect(pointerRemainingSelectedTab).toHaveCount(1)
  await expect(pointerRemainingSelectedTab.locator('xpath=..')).toHaveAttribute('data-tree-tab', selectedTabId)
  await expect(pointerRemainingSelectedTab).toBeFocused()
  await expect(activeTranscript).toContainText('first close-button session')

  // Recreate a stacked inactive session so this test also follows the real
  // keyboard route (native Tab into the close button, then Enter).
  await page.locator('[data-slot="sidebar"] button[aria-label="New session"]').first().click()
  await sendMessage(page, 'third close-button session')
  await sessionRows.last().dispatchEvent('click', { ctrlKey: true })

  const keyboardCloseButtons = page.locator('[data-tree-tab] > button[aria-label="Close tab"]')
  await expect.poll(() => keyboardCloseButtons.count()).toBeGreaterThan(1)

  const keyboardTabList = keyboardCloseButtons.first().locator('xpath=ancestor::*[@role="tablist"][1]')
  const keyboardTabItems = keyboardTabList.locator('[data-tree-tab]')
  const keyboardInitialTabCount = await keyboardTabItems.count()
  const keyboardSelectedTab = keyboardTabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')
  const keyboardInactiveTab = keyboardTabList
    .locator('[data-tree-tab]:has(> button[aria-label="Close tab"]) > [role="tab"][aria-selected="false"]')
    .first()

  await expect(keyboardSelectedTab).toHaveCount(1)
  await expect(keyboardInactiveTab).toHaveCount(1)

  const keyboardSelectedTabItem = keyboardSelectedTab.locator('xpath=..')
  const keyboardInactiveTabItem = keyboardInactiveTab.locator('xpath=..')
  const keyboardSelectedTabId = await keyboardSelectedTabItem.getAttribute('data-tree-tab')
  const keyboardInactiveTabId = await keyboardInactiveTabItem.getAttribute('data-tree-tab')

  if (!keyboardSelectedTabId || !keyboardInactiveTabId) {
    throw new Error('Expected a recreated stacked tab pair for keyboard close coverage')
  }

  const keyboardCloseButton = keyboardInactiveTabItem.locator('button[aria-label="Close tab"]')
  const keyboardCloseIcon = keyboardCloseButton.locator('[data-slot="pane-tab-close-icon"]')
  await keyboardInactiveTab.focus()
  await page.keyboard.press('Tab')
  await expect(keyboardCloseButton).toBeFocused()
  await expect(keyboardCloseIcon).toHaveCSS('opacity', '1')
  await page.keyboard.press('Enter')
  await expect
    .poll(() => keyboardTabItems.evaluateAll(items => items.map(item => item.getAttribute('data-tree-tab'))))
    .not.toContain(keyboardInactiveTabId)
  await expect(keyboardTabItems).toHaveCount(keyboardInitialTabCount - 1)

  const remainingSelectedTab = keyboardTabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')
  await expect(remainingSelectedTab).toHaveCount(1)
  await expect(remainingSelectedTab.locator('xpath=..')).toHaveAttribute('data-tree-tab', keyboardSelectedTabId)
  await expect(remainingSelectedTab).toBeFocused()
  await expect(activeTranscript).toContainText('first close-button session')

  const remainingTabs = keyboardTabList.locator('[data-tree-tab] > [role="tab"]')
  await remainingSelectedTab.focus()
  await page.keyboard.press('Home')
  await expect(remainingTabs.first()).toBeFocused()
  await page.keyboard.press('End')
  await expect(remainingTabs.last()).toBeFocused()

  // The platform close shortcut is a separate route from the tab's close
  // control. Keep a sibling in the stack, focus the selected tab, then prove
  // the root-level recovery moves focus to the surviving tab instead of body.
  await page.locator('[data-slot="sidebar"] button[aria-label="New session"]').first().click()
  await sendMessage(page, 'fourth close-button session')
  await sessionRows.last().dispatchEvent('click', { ctrlKey: true })

  const commandTabItems = keyboardTabList.locator('[data-tree-tab]')
  const commandCloseableTab = keyboardTabList.locator('[data-tree-tab^="session-tile:"] > [role="tab"]')

  await expect(commandCloseableTab).toHaveCount(1)
  await commandCloseableTab.click()
  await expect(commandCloseableTab).toHaveAttribute('aria-selected', 'true')

  const commandSelectedTab = keyboardTabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')
  const commandSelectedItem = commandSelectedTab.locator('xpath=..')
  const commandInactiveTab = keyboardTabList.locator('[data-tree-tab] > [role="tab"][aria-selected="false"]')
  const commandSelectedId = await commandSelectedItem.getAttribute('data-tree-tab')

  if (!commandSelectedId) {
    throw new Error('Expected ⌘W target tab to expose a stable data-tree-tab identity')
  }

  expect(commandSelectedId).toMatch(/^session-tile:/)
  await expect(commandSelectedTab).toHaveCount(1)
  await expect(commandInactiveTab).toHaveCount(1)
  await commandSelectedTab.focus()
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+w' : 'Control+w')

  await expect
    .poll(() => commandTabItems.evaluateAll(items => items.map(item => item.getAttribute('data-tree-tab'))))
    .not.toContain(commandSelectedId)
  await expect(commandTabItems).toHaveCount(1)

  const commandRemainingTab = keyboardTabList.locator('[data-tree-tab] > [role="tab"][aria-selected="true"]')
  await expect(commandRemainingTab).toHaveCount(1)
  await expect(commandRemainingTab).toBeFocused()
})
