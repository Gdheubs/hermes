import { afterEach, describe, expect, it } from 'vitest'

import { shouldYieldShellContextMenu } from './shell-context-menu'

afterEach(() => {
  window.getSelection()?.removeAllRanges()
})

function mount(html: string): HTMLElement {
  document.body.innerHTML = html
  return document.body.firstElementChild as HTMLElement
}

describe('shouldYieldShellContextMenu', () => {
  it('stands down on a chat / markdown link so Electron can offer Copy Link', () => {
    const link = mount('<a href="https://docs.google.com/spreadsheets/d/abc">docs.google.com/spreadsheets</a>')
    expect(shouldYieldShellContextMenu(link)).toBe(true)
  })

  it('stands down when the click lands on a child of the anchor (brand icon, pretty label)', () => {
    mount('<a href="https://example.com/path"><span class="label">example.com/path</span></a>')
    expect(shouldYieldShellContextMenu(document.querySelector('.label'))).toBe(true)
  })

  it('does not treat a non-link chrome click as owned', () => {
    const pane = mount('<div data-slot="thread">empty pane</div>')
    expect(shouldYieldShellContextMenu(pane)).toBe(false)
  })

  it('ignores an anchor with no href', () => {
    const fake = mount('<a>not a link</a>')
    expect(shouldYieldShellContextMenu(fake)).toBe(false)
  })

  it('stands down for a nested Radix menu that is not the shell fallback', () => {
    mount('<div data-slot="context-menu-trigger"><span class="row">session</span></div>')
    expect(shouldYieldShellContextMenu(document.querySelector('.row'))).toBe(true)
  })

  it('does not treat the shell fallback trigger as an owner', () => {
    mount('<div data-slot="context-menu-trigger" data-shell-context-menu=""><span class="chrome">titlebar</span></div>')
    expect(shouldYieldShellContextMenu(document.querySelector('.chrome'))).toBe(false)
  })

  it('stands down for an editable', () => {
    const input = mount('<textarea></textarea>')
    expect(shouldYieldShellContextMenu(input)).toBe(true)
  })

  it('stands down when the user has a live text selection', () => {
    const pane = mount('<p id="copy-me">selectable prose</p>')
    const range = document.createRange()
    range.selectNodeContents(pane)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    expect(shouldYieldShellContextMenu(pane)).toBe(true)
  })
})
