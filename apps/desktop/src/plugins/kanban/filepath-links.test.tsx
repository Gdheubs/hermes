/**
 * Focused tests for the kanban comment file-path linkifier.
 *
 * The @hermes/plugin-sdk host is mocked so revealFileInTree calls are asserted,
 * never really executed against a store.
 */

import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { isAbsoluteFilePath, LinkifiedFilePath } from './filepath-links'

const { hostMock } = vi.hoisted(() => ({
  hostMock: { revealFileInTree: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', () => ({
  host: hostMock
}))

/** Render the component to static HTML and return the markup string. */
function renderHtml(text: string): string {
  const out = LinkifiedFilePath({ text })

  return typeof out === 'string' ? out : renderToStaticMarkup(<>{out}</>)
}

/** Extract the path labels of every rendered file-path button. */
function linkLabels(text: string): string[] {
  const html = renderHtml(text)
  const labels: string[] = []
  const re = /aria-label="Reveal ([^"]*) in file tree"/g
  let match: RegExpExecArray | null

  while ((match = re.exec(html)) !== null) {
    labels.push(match[1])
  }

  return labels
}

describe('isAbsoluteFilePath', () => {
  it('accepts POSIX absolute paths', () => {
    expect(isAbsoluteFilePath('/opt/data/skills/x/SKILL.md')).toBe(true)
    expect(isAbsoluteFilePath('/opt/hermes/agent.log')).toBe(true)
    expect(isAbsoluteFilePath('/opt/data/receipt-processing/')).toBe(true)
    expect(isAbsoluteFilePath('/')).toBe(false)
  })

  it('accepts Windows absolute paths', () => {
    expect(isAbsoluteFilePath('C:/Users/me/src/a.ts')).toBe(true)
    expect(isAbsoluteFilePath('C:\\Users\\me\\src\\a.ts')).toBe(true)
    expect(isAbsoluteFilePath('\\\\server\\share\\file.md')).toBe(true)
  })

  it('rejects relative paths and bare words', () => {
    expect(isAbsoluteFilePath('src/a.ts')).toBe(false)
    expect(isAbsoluteFilePath('SKILL.md')).toBe(false)
    expect(isAbsoluteFilePath('Updated')).toBe(false)
  })
})

describe('LinkifiedFilePath', () => {
  beforeEach(() => {
    hostMock.revealFileInTree.mockClear()
  })

  it('renders plain text unchanged when there are no absolute paths', () => {
    expect(renderHtml('Updated the worker to v1.4.1')).toBe('Updated the worker to v1.4.1')
  })

  it('wraps an absolute POSIX path in a reveal link', () => {
    const labels = linkLabels('Updated `/opt/data/skills/x/SKILL.md` to v1.4.1')
    expect(labels).toContain('/opt/data/skills/x/SKILL.md')
  })

  it('does not linkify a relative path', () => {
    const labels = linkLabels('see skills/x/SKILL.md for details')
    expect(labels).not.toContain('skills/x/SKILL.md')
  })

  it('renders every file-path link as a clickable button', () => {
    const html = renderHtml('Updated `/opt/data/skills/x/SKILL.md` to v1.4.1')
    expect(html).toContain('kanban-filepath-link')
    expect(html).toContain('<button')
  })

  it('does not call revealFileInTree at render time', () => {
    renderHtml('Updated `/opt/data/skills/x/SKILL.md` to v1.4.1')
    expect(hostMock.revealFileInTree).not.toHaveBeenCalled()
  })

  it('stops the path at sentence punctuation, not mid-filename', () => {
    const labels = linkLabels('see /opt/data/file.sh! and /opt/data/file.tar.gz.')
    expect(labels).toContain('/opt/data/file.sh')
    expect(labels).toContain('/opt/data/file.tar.gz')
  })

  it('links a directory reference ending in a slash', () => {
    const labels = linkLabels('Everything in /opt/data/receipt-processing/ now')
    expect(labels).toContain('/opt/data/receipt-processing/')
  })

  it('links Windows drive and UNC paths', () => {
    const labels = linkLabels('edit C:/Users/me/src/a.ts and \\\\server\\share\\file.md')
    expect(labels).toContain('C:/Users/me/src/a.ts')
    expect(labels).toContain('\\\\server\\share\\file.md')
  })

  it('does not linkify a bare slash or a leading double-slash', () => {
    expect(linkLabels('/ alone')).toEqual([])
    expect(linkLabels('see //share/x.ts')).toEqual([])
  })
})
